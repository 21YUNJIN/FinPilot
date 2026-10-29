"""转账日额度与分级（V5 6.1、AUTH-03、8.2）。

口径：
- 100000 分（1000 元）是**验证等级边界**，不是转账最高限额。
- 日累计 = 已成功转账本金 + 已占用的待执行本金 + 本次本金。
- 恰好 100000 分为 YELLOW，超过为 RED；手续费不计入本金累计。
- 并发分拆不能绕过升级：占用在提交事务内串行登记到同一额度桶。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..models import DailyLimit, DailyQuotaHold
from ..money import CURRENCY
from ..timeutil import current_business_day, utcnow
from .identity import AuthContext

YELLOW = "YELLOW"
RED = "RED"
GREEN = "GREEN"


def _bucket(session: Session, ctx: AuthContext, day: date) -> DailyLimit:
    """取（或创建）当日额度桶，并在事务内加行锁。"""
    stmt = pg_insert(DailyLimit).values(
        id=f"{ctx.user_id}:{day.isoformat()}:{CURRENCY}",
        user_id=ctx.user_id,
        business_day=day,
        currency=CURRENCY,
        settled_transfer_minor=0,
        held_pending_minor=0,
        version=1,
    ).on_conflict_do_nothing(index_elements=["user_id", "business_day", "currency"])
    session.execute(stmt)
    bucket = session.execute(
        select(DailyLimit)
        .where(
            DailyLimit.user_id == ctx.user_id,
            DailyLimit.business_day == day,
            DailyLimit.currency == CURRENCY,
        )
        .with_for_update()
    ).scalar_one()
    return bucket


def daily_usage(session: Session, ctx: AuthContext, business_day: date | None = None) -> dict:
    day = business_day or current_business_day()
    bucket = session.execute(
        select(DailyLimit).where(
            DailyLimit.user_id == ctx.user_id,
            DailyLimit.business_day == day,
            DailyLimit.currency == CURRENCY,
        )
    ).scalar_one_or_none()
    settled = bucket.settled_transfer_minor if bucket else 0
    held = bucket.held_pending_minor if bucket else 0
    limit = get_settings().transfer_daily_yellow_limit_minor
    return {
        "business_day": day.isoformat(),
        "settled_transfer_minor": settled,
        "held_pending_minor": held,
        "used_minor": settled + held,
        "yellow_limit_minor": limit,
        "next_level": YELLOW if settled + held <= limit else RED,
    }


def grade_transfer(session: Session, ctx: AuthContext, *, principal_minor: int, business_day: date | None = None) -> str:
    if principal_minor <= 0:
        raise FinPilotError("PARAM_INVALID", "转账本金必须大于 0。")
    day = business_day or current_business_day()
    bucket = session.execute(
        select(DailyLimit).where(
            DailyLimit.user_id == ctx.user_id,
            DailyLimit.business_day == day,
            DailyLimit.currency == CURRENCY,
        )
    ).scalar_one_or_none()
    used = (bucket.settled_transfer_minor + bucket.held_pending_minor) if bucket else 0
    return YELLOW if used + principal_minor <= get_settings().transfer_daily_yellow_limit_minor else RED


def hold_daily_quota(
    session: Session,
    ctx: AuthContext,
    *,
    operation_id: str,
    principal_minor: int,
    business_day: date | None = None,
) -> None:
    """占用待执行本金；同一 operation 重复调用幂等，异参冲突。"""
    if principal_minor <= 0:
        return
    day = business_day or current_business_day()
    bucket = _bucket(session, ctx, day)
    existing = session.get(DailyQuotaHold, operation_id)
    if existing is not None:
        if existing.principal_minor != principal_minor or existing.business_day != day:
            raise FinPilotError("IDEMPOTENCY_CONFLICT", "同一操作编号被用于不同参数的额度占用。")
        if existing.state == "held":
            return
        if existing.state == "settled":
            return
        raise FinPilotError("IDEMPOTENCY_CONFLICT", "该操作额度已释放，不能再次占用。")

    now = utcnow()
    session.add(
        DailyQuotaHold(
            operation_id=operation_id,
            user_id=ctx.user_id,
            business_day=day,
            currency=CURRENCY,
            principal_minor=principal_minor,
            state="held",
            created_at=now,
            updated_at=now,
        )
    )
    bucket.held_pending_minor += principal_minor
    bucket.version += 1
    session.flush()


def settle_daily_quota(
    session: Session,
    ctx: AuthContext,
    *,
    operation_id: str,
    principal_minor: int,
    business_day: date | None = None,
) -> None:
    """成功转账：占用转为累计（退款不自动减少已成功累计）。"""
    hold = session.get(DailyQuotaHold, operation_id)
    if hold is None:
        hold_daily_quota(session, ctx, operation_id=operation_id, principal_minor=principal_minor, business_day=business_day)
        hold = session.get(DailyQuotaHold, operation_id)
        assert hold is not None
    if hold.state == "settled":
        return
    day = hold.business_day
    bucket = _bucket(session, ctx, day)
    if hold.state == "held":
        bucket.held_pending_minor = max(0, bucket.held_pending_minor - hold.principal_minor)
        bucket.settled_transfer_minor += hold.principal_minor
        bucket.version += 1
    elif hold.state == "released":
        # 已释放后仍然确认成功：补记累计，避免漏计
        bucket.settled_transfer_minor += hold.principal_minor
        bucket.version += 1
    hold.state = "settled"
    hold.updated_at = utcnow()
    session.flush()


def release_daily_quota(
    session: Session,
    ctx: AuthContext,
    *,
    operation_id: str,
    principal_minor: int,
    business_day: date | None = None,
) -> None:
    """确认无转账效果（失败/取消）时释放占用；未知结果不得释放。"""
    hold = session.get(DailyQuotaHold, operation_id)
    if hold is None:
        return
    if hold.state != "held":
        return
    bucket = _bucket(session, ctx, hold.business_day)
    bucket.held_pending_minor = max(0, bucket.held_pending_minor - hold.principal_minor)
    bucket.version += 1
    hold.state = "released"
    hold.updated_at = utcnow()
    session.flush()


def quota_holds(session: Session, ctx: AuthContext, day: date | None = None) -> list[dict]:
    stmt = select(DailyQuotaHold).where(DailyQuotaHold.user_id == ctx.user_id)
    if day is not None:
        stmt = stmt.where(DailyQuotaHold.business_day == day)
    rows = session.execute(stmt.order_by(DailyQuotaHold.created_at)).scalars().all()
    return [
        {
            "operation_id": row.operation_id,
            "business_day": row.business_day.isoformat(),
            "principal_minor": row.principal_minor,
            "state": row.state,
        }
        for row in rows
    ]


def total_used(session: Session, ctx: AuthContext, day: date | None = None) -> int:
    day = day or current_business_day()
    value = session.execute(
        select(func.coalesce(func.sum(DailyQuotaHold.principal_minor), 0)).where(
            DailyQuotaHold.user_id == ctx.user_id,
            DailyQuotaHold.business_day == day,
            DailyQuotaHold.state.in_(("held", "settled")),
        )
    ).scalar_one()
    return int(value)