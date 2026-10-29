"""转账执行与结算：唯一记账入口（V5 8.1、9、S1-02、STATE-02）。

- 成功回执才记账：一次且仅一次分录；分录守恒。
- 失败且确认无转账效果时释放额度占用，预留保留供任务重试。
- 结果未知保留占用与预留，先用原操作编号查询，不把未知写成失败。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..models import Operation, Receipt, Reservation
from ..timeutil import utcnow
from .identity import AuthContext
from .ledger import CLEARING_ACCOUNT_ID, ensure_clearing_account, post_transaction
from .limits import release_daily_quota, settle_daily_quota
from .reservations import capture_reservation, clear_inflight, mark_inflight
from .state_machine import assert_transition
from .submission import transition


def _reservation_for_operation(session: Session, operation: Operation) -> Reservation | None:
    if operation.task_id:
        rows = session.execute(
            select(Reservation).where(
                Reservation.task_id == operation.task_id,
                Reservation.account_id == operation.source_account_id,
                Reservation.status.in_(("active", "partially_captured")),
            )
        ).scalars().all()
        if rows:
            return rows[0]
    if operation.source_account_id:
        rows = session.execute(
            select(Reservation).where(
                Reservation.account_id == operation.source_account_id,
                Reservation.status.in_(("active", "partially_captured")),
                Reservation.purpose == "task_budget",
            )
        ).scalars().all()
        return rows[0] if rows else None
    return None


def settle_transfer(
    session: Session,
    *,
    operation: Operation,
    status: str,
    provider_ref: str | None,
    raw: dict | None = None,
    source: str = "provider_callback",
) -> Operation:
    """按权威回执结算转账；同一操作只允许一次金融效果。"""
    from .provider_adapter import normalize_status

    status = normalize_status(status)
    if status == "cancel_rejected":
        # 撤单被拒说明原业务仍然有效，继续保持等待/在途，不改账
        status = "pending"
    ctx = AuthContext(user_id=operation.owner_user_id, roles=("customer",), channel="system", session_id="system")

    if status == "succeeded":
        if operation.status == "succeeded":
            return operation  # 重复回执：不再记账
        if operation.effect_confirmed:
            return operation

        total = operation.amount_minor + operation.fee_minor
        reservation = _reservation_for_operation(session, operation)
        if reservation is not None and reservation.remaining_minor >= total and total > 0:
            capture_reservation(
                session,
                ctx,
                reservation_id=reservation.reservation_id,
                amount_minor=total,
                operation_id=operation.operation_id,
                memo=f"{operation.tool} 预留捕获",
                kind="transfer_capture",
            )
        elif total > 0:
            ensure_clearing_account(session)
            post_transaction(
                session,
                kind="transfer",
                entries=[(operation.source_account_id, -total), (CLEARING_ACCOUNT_ID, total)],
                operation_id=operation.operation_id,
                task_id=operation.task_id,
                memo=f"{operation.tool} 转账支出",
            )

        if operation.tool in ("transfer.submit", "schedule.create") and operation.amount_minor > 0:
            settle_daily_quota(
                session,
                ctx,
                operation_id=operation.operation_id,
                principal_minor=operation.amount_minor,
            )

        operation.provider_order_ref = provider_ref or operation.provider_order_ref
        from .provider_adapter import default_provider_id
        operation.provider_id = operation.provider_id or default_provider_id("transfer.submit")
        transition(session, operation, "succeeded", reason="provider_succeeded", detail={"provider_ref": provider_ref})
        return operation

    if status == "failed":
        if operation.status == "succeeded":
            # 冲突终态：隔离并交人工核验，不依到达顺序随意改账
            raise FinPilotError(
                "POLICY_DENIED",
                "已成功的操作收到失败回执，已进入隔离核验。",
                details={"reason_codes": ["conflicting_terminal_state"]},
            )
        if operation.tool in ("transfer.submit", "schedule.create") and operation.amount_minor > 0:
            release_daily_quota(
                session,
                ctx,
                operation_id=operation.operation_id,
                principal_minor=operation.amount_minor,
            )
        reservation = _reservation_for_operation(session, operation)
        if reservation is not None:
            clear_inflight(session, reservation_id=reservation.reservation_id)
        operation.error_code = (raw or {}).get("failure_code") or "PROVIDER_REJECTED"
        transition(session, operation, "failed", reason="provider_failed", detail={"raw": raw or {}})
        return operation

    if status in ("pending", "accepted"):
        reservation = _reservation_for_operation(session, operation)
        if reservation is not None:
            mark_inflight(session, reservation_id=reservation.reservation_id, amount_minor=0)
        if operation.status not in ("pending", "dispatching", "authorized"):
            transition(session, operation, "pending", reason="provider_pending", detail={})
        else:
            assert_transition("operation", operation.status, "pending")
            operation.status = "pending"
            operation.version += 1
            operation.updated_at = utcnow()
        operation.provider_order_ref = provider_ref or operation.provider_order_ref
        session.flush()
        return operation

    if status in ("unknown", "timeout"):
        reservation = _reservation_for_operation(session, operation)
        if reservation is not None:
            mark_inflight(session, reservation_id=reservation.reservation_id, amount_minor=operation.amount_minor)
        transition(session, operation, "unknown", reason="provider_unknown", detail={"source": source})
        return operation

    raise FinPilotError("SYSTEM_ERROR", f"未知的提供方状态: {status}")


def record_receipt(
    session: Session,
    *,
    operation: Operation,
    provider_id: str,
    provider_ref: str,
    payload: dict,
    source: str,
    verified_status: str = "verified",
    event_version: int = 1,
) -> Receipt:
    from ..ids import canonical_hash

    payload_hash = canonical_hash(payload or {})
    existing = session.execute(
        select(Receipt).where(Receipt.operation_id == operation.operation_id, Receipt.payload_hash == payload_hash)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    receipt = Receipt(
        operation_id=operation.operation_id,
        provider_id=provider_id,
        provider_ref=provider_ref,
        source=source,
        event_version=event_version,
        received_at=utcnow(),
        verified_status=verified_status,
        payload_hash=payload_hash,
        payload=payload or {},
        state="accepted",
    )
    session.add(receipt)
    session.flush()
    return receipt


def transfer_operation_ids(session: Session, task_id: str) -> list[str]:
    return list(
        session.execute(
            select(Operation.operation_id).where(Operation.task_id == task_id, Operation.tool == "transfer.submit")
        ).scalars()
    )
