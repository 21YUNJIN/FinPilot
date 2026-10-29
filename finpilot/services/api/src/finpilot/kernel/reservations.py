"""资金预留：创建、捕获、部分释放与过期清理（V5 8.1、8.3、MONEY-02/05）。

不变量：
- 预留创建不改变 B，只减少可支配金额。
- 捕获 x：B 与 H 同减 x（禁止先扣余额再额外减少可用资金造成双减）。
- 释放只释放确认未被占用的部分；dispatching/pending/unknown 关联金额不得因 TTL 释放。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import lock_accounts
from ..errors import FinPilotError
from ..ids import new_id
from ..models import Account, Operation, Reservation
from ..timeutil import utcnow
from .identity import AuthContext
from .ledger import post_transaction, reserved_minor
from .snapshots import protection_policy

INFLIGHT_STATUSES = ("dispatching", "pending", "unknown")


def _lock_reservation(session: Session, reservation_id: str) -> Reservation:
    reservation = session.execute(
        select(Reservation).where(Reservation.reservation_id == reservation_id).with_for_update()
    ).scalar_one_or_none()
    if reservation is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该预留。")
    return reservation


def create_reservation(
    session: Session,
    ctx: AuthContext,
    *,
    account_id: str,
    amount_minor: int,
    task_id: str | None,
    purpose: str = "task_budget",
    long_lived: bool = False,
    expires_at: datetime | None = None,
    enforce_floor: bool = True,
) -> Reservation:
    ctx.require_customer()
    if amount_minor <= 0:
        raise FinPilotError("PARAM_INVALID", "预留金额必须大于 0。")
    account = session.get(Account, account_id)
    if account is None or account.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
    lock_accounts(session, [account_id])

    policy = protection_policy(session, ctx.user_id)
    floor = policy.protected_floor_minor if (policy and enforce_floor and account_id in (policy.account_scope or [account_id])) else 0
    available = account.posted_balance_minor - reserved_minor(session, account_id) - account.frozen_other_minor
    if available < amount_minor:
        raise FinPilotError(
            "RESERVATION_EXCEEDED",
            "预留金额超过该账户可用余额。",
            details={"reason_codes": ["insufficient_available"], "available_minor": available},
        )
    if enforce_floor and available - amount_minor < floor:
        raise FinPilotError(
            "PROTECTED_FLOOR_VIOLATION",
            "该预留会穿透已确认的应急金底线。",
            details={"reason_codes": ["protected_floor"], "protected_floor_minor": floor},
        )

    settings = get_settings()
    now = utcnow()
    due = expires_at or (None if long_lived else now + timedelta(seconds=settings.reservation_idle_ttl_seconds))
    reservation = Reservation(
        reservation_id=new_id("rsv"),
        owner_user_id=ctx.user_id,
        account_id=account_id,
        task_id=task_id,
        purpose=purpose,
        original_minor=amount_minor,
        remaining_minor=amount_minor,
        captured_minor=0,
        released_minor=0,
        inflight_minor=0,
        status="active",
        long_lived=long_lived,
        expires_at=due,
        created_at=now,
        updated_at=now,
    )
    session.add(reservation)
    session.flush()
    return reservation


def capture_reservation(
    session: Session,
    ctx: AuthContext,
    *,
    reservation_id: str,
    amount_minor: int,
    operation_id: str | None = None,
    counterparty_account: str | None = None,
    memo: str = "",
    kind: str = "reservation_capture",
) -> Reservation:
    """从预留中捕获支付 x：减少 B 与 H 各 x，并记一笔守恒分录。"""
    if amount_minor <= 0:
        raise FinPilotError("PARAM_INVALID", "捕获金额必须大于 0。")
    reservation = _lock_reservation(session, reservation_id)
    if reservation.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该预留。")
    if reservation.status not in ("active", "partially_captured"):
        raise FinPilotError("POLICY_DENIED", "该预留已结束，不能继续捕获。")
    if amount_minor > reservation.remaining_minor:
        raise FinPilotError(
            "RESERVATION_EXCEEDED",
            "捕获金额超过预留剩余金额。",
            details={"reason_codes": ["exceeds_remaining"], "remaining_minor": reservation.remaining_minor},
        )

    lock_accounts(session, [reservation.account_id])
    from .ledger import CLEARING_ACCOUNT_ID, ensure_clearing_account

    target = counterparty_account or CLEARING_ACCOUNT_ID
    if target == CLEARING_ACCOUNT_ID:
        ensure_clearing_account(session)
    post_transaction(
        session,
        kind=kind,
        entries=[(reservation.account_id, -amount_minor), (target, amount_minor)],
        operation_id=operation_id,
        task_id=reservation.task_id,
        memo=memo or "预留捕获",
    )

    reservation.remaining_minor -= amount_minor
    reservation.captured_minor += amount_minor
    reservation.status = "captured" if reservation.remaining_minor == 0 else "partially_captured"
    reservation.version += 1
    reservation.updated_at = utcnow()
    session.flush()
    return reservation


def release_reservation(
    session: Session,
    ctx: AuthContext,
    *,
    reservation_id: str,
    amount_minor: int | None = None,
    reason: str = "",
) -> Reservation:
    """释放确认未占用的部分；不改变 B。"""
    reservation = _lock_reservation(session, reservation_id)
    if reservation.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该预留。")
    if reservation.status not in ("active", "partially_captured"):
        raise FinPilotError("POLICY_DENIED", "该预留已结束，不能重复释放。")
    if reservation.inflight_minor > 0:
        raise FinPilotError(
            "POLICY_DENIED",
            "存在在途未知金额，不能释放该预留。",
            details={"reason_codes": ["inflight_unknown"], "inflight_minor": reservation.inflight_minor},
        )
    release_amount = reservation.remaining_minor if amount_minor is None else amount_minor
    if release_amount <= 0 or release_amount > reservation.remaining_minor:
        raise FinPilotError("PARAM_INVALID", "释放金额不合法。")
    reservation.remaining_minor -= release_amount
    reservation.released_minor += release_amount
    reservation.status = "released" if reservation.remaining_minor == 0 else "partially_captured"
    reservation.version += 1
    reservation.updated_at = utcnow()
    session.flush()
    return reservation


def mark_inflight(session: Session, *, reservation_id: str, amount_minor: int) -> Reservation:
    """登记在途未知金额：既不能当可用资金，也不能被 TTL 释放。"""
    reservation = _lock_reservation(session, reservation_id)
    reservation.inflight_minor += amount_minor
    reservation.version += 1
    reservation.updated_at = utcnow()
    session.flush()
    return reservation


def clear_inflight(session: Session, *, reservation_id: str, amount_minor: int | None = None) -> Reservation:
    reservation = _lock_reservation(session, reservation_id)
    reservation.inflight_minor = 0 if amount_minor is None else max(0, reservation.inflight_minor - amount_minor)
    reservation.version += 1
    reservation.updated_at = utcnow()
    session.flush()
    return reservation


def expire_idle_reservations(session: Session, *, now: datetime | None = None) -> list[str]:
    """清理闲置预留：长期预留与在途金额不动。"""
    moment = now or utcnow()
    rows = session.execute(
        select(Reservation)
        .where(
            Reservation.status.in_(("active", "partially_captured")),
            Reservation.long_lived.is_(False),
            Reservation.expires_at.is_not(None),
            Reservation.expires_at <= moment,
        )
        .with_for_update(skip_locked=True)
    ).scalars().all()

    expired: list[str] = []
    for reservation in rows:
        if reservation.inflight_minor > 0:
            continue
        if reservation.operation_id:
            operation = session.get(Operation, reservation.operation_id)
            if operation is not None and operation.status in INFLIGHT_STATUSES:
                continue
        reservation.status = "expired"
        reservation.released_minor += reservation.remaining_minor
        reservation.remaining_minor = 0
        reservation.version += 1
        reservation.updated_at = moment
        expired.append(reservation.reservation_id)
    session.flush()
    return expired


def reservations_for_task(session: Session, task_id: str) -> list[Reservation]:
    return list(
        session.execute(select(Reservation).where(Reservation.task_id == task_id).order_by(Reservation.created_at)).scalars()
    )


def reservation_view(reservation: Reservation) -> dict:
    return {
        "reservation_id": reservation.reservation_id,
        "account_id": reservation.account_id,
        "task_id": reservation.task_id,
        "original_minor": reservation.original_minor,
        "remaining_minor": reservation.remaining_minor,
        "captured_minor": reservation.captured_minor,
        "released_minor": reservation.released_minor,
        "inflight_minor": reservation.inflight_minor,
        "status": reservation.status,
        "long_lived": reservation.long_lived,
        "expires_at": reservation.expires_at.isoformat() if reservation.expires_at else None,
    }