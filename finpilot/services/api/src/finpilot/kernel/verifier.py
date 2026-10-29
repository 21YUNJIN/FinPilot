"""强验证挑战：真实 TOTP 校验、范围绑定、过期、次数限制与重放防护（V5 6.2、AUTH-05）。

默认：挑战有效期 120 秒、最多 5 次尝试、失败锁定 15 分钟。
成功证明绑定 user_id + scope_hash + challenge_id + expires_at，只能满足指定操作范围。
验证码不进入对话、模型上下文、日志或审计。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..ids import new_id, scope_hash as compute_scope_hash
from ..models import AuthChallenge
from ..timeutil import utcnow
from .grants import create_exact_grant, create_scoped_grant
from .identity import AuthContext, load_totp_secret, user_has_strong_factor, verify_totp

_CHALLENGE_LOOKBACK = timedelta(minutes=10)


def issue_challenge(
    session: Session,
    ctx: AuthContext,
    *,
    scope: dict,
    plan_id: str | None,
    plan_revision: int,
    factor_type: str = "totp",
) -> AuthChallenge:
    ctx.require_customer()
    if not user_has_strong_factor(session, ctx.user_id):
        raise FinPilotError(
            "FACTOR_NOT_ENROLLED",
            "尚未配置强验证因子，无法完成该操作。",
            details={"reason_codes": ["factor_not_enrolled"]},
        )
    settings = get_settings()
    now = utcnow()

    # 同一用户未完成的旧挑战立即失效，避免并行挑战放大尝试次数
    stale = session.execute(
        select(AuthChallenge).where(
            AuthChallenge.user_id == ctx.user_id,
            AuthChallenge.status == "pending",
        )
    ).scalars().all()
    for old in stale:
        old.status = "expired"

    challenge = AuthChallenge(
        challenge_id=new_id("chal"),
        user_id=ctx.user_id,
        operation_scope_hash=compute_scope_hash({k: v for k, v in scope.items() if k != "scope_hash"}),
        scope=scope,
        plan_id=plan_id,
        plan_revision=plan_revision,
        factor_type=factor_type,
        status="pending",
        expires_at=now + timedelta(seconds=settings.challenge_ttl_seconds),
        attempts=0,
        max_attempts=settings.challenge_max_attempts,
        created_at=now,
    )
    session.add(challenge)
    session.flush()
    return challenge


def _used_counters(session: Session, user_id: str, now: datetime) -> set[int]:
    rows = session.execute(
        select(AuthChallenge.last_counter).where(
            AuthChallenge.user_id == user_id,
            AuthChallenge.last_counter.is_not(None),
            AuthChallenge.consumed_at.is_not(None),
            AuthChallenge.consumed_at >= now - _CHALLENGE_LOOKBACK,
        )
    ).scalars().all()
    return {int(c) for c in rows if c is not None}


def verify_challenge(session: Session, ctx: AuthContext, *, challenge_id: str, factor_response: str) -> dict:
    ctx.require_customer()
    settings = get_settings()
    now = utcnow()

    challenge = session.execute(
        select(AuthChallenge).where(AuthChallenge.challenge_id == challenge_id).with_for_update()
    ).scalar_one_or_none()
    if challenge is None or challenge.user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该验证。")

    if challenge.status == "verified" and challenge.consumed_at is not None:
        raise FinPilotError("GRANT_INVALID", "该验证已完成并已签发授权，不能重复使用。")
    if challenge.status == "locked" and challenge.locked_until and challenge.locked_until > now:
        raise FinPilotError(
            "CHALLENGE_LOCKED",
            "验证尝试次数过多，请稍后再试。",
            details={"reason_codes": ["locked"], "locked_until": challenge.locked_until.isoformat()},
        )
    if challenge.expires_at <= now:
        challenge.status = "expired"
        session.flush()
        raise FinPilotError("CHALLENGE_EXPIRED", "验证已过期，请重新发起验证。")

    secret = load_totp_secret(session, ctx.user_id)
    used = _used_counters(session, ctx.user_id, now)
    ok, counter = verify_totp(secret, factor_response.strip(), now, used_counters=used)

    if not ok:
        challenge.attempts += 1
        if challenge.attempts >= challenge.max_attempts:
            challenge.status = "locked"
            challenge.locked_until = now + timedelta(seconds=settings.challenge_lock_seconds)
            session.flush()
            raise FinPilotError(
                "CHALLENGE_LOCKED",
                "验证尝试次数过多，请稍后再试。",
                details={"reason_codes": ["max_attempts"], "attempts": challenge.attempts},
            )
        session.flush()
        raise FinPilotError(
            "CHALLENGE_FAILED",
            "验证码不正确。",
            details={"reason_codes": ["bad_code"], "attempts_left": challenge.max_attempts - challenge.attempts},
        )

    # 一次性消费挑战，并记录计数器防重放
    challenge.status = "verified"
    challenge.verified_at = now
    challenge.consumed_at = now
    challenge.last_counter = counter

    scope = dict(challenge.scope)
    required_level = scope.get("required_level", "RED")
    scope_kind = scope.get("kind", "exact")
    max_uses = int(scope.get("max_uses", 1))
    max_total = int(scope.get("max_total_minor", 0))

    if scope_kind == "scoped" and max_uses > 1 and max_total > 0:
        grant = create_scoped_grant(
            session,
            ctx,
            plan_id=challenge.plan_id,
            plan_revision=challenge.plan_revision,
            scope=scope,
            required_level=required_level,
            max_uses=max_uses,
            max_total_minor=max_total,
            expires_at=min(challenge.expires_at + timedelta(minutes=5), now + timedelta(minutes=15)),
            verification_level="TOTP",
        )
    else:
        grant = create_exact_grant(
            session,
            ctx,
            plan_id=challenge.plan_id,
            plan_revision=challenge.plan_revision,
            scope=scope,
            required_level=required_level,
            verification_level="TOTP",
        )
    grant.challenge_id = challenge.challenge_id
    grant.status = "active"
    session.flush()

    return {
        "challenge_id": challenge.challenge_id,
        "status": "verified",
        "grant": {
            "grant_id": grant.grant_id,
            "status": grant.status,
            "scope_hash": grant.scope_hash,
            "required_level": grant.required_level,
            "verification_level": grant.verification_level,
            "expires_at": grant.expires_at.isoformat(),
        },
    }


def lock_out(session: Session, ctx: AuthContext, *, reason: str = "suspicious_behavior") -> None:
    """安全锁：可疑行为触发后，高风险操作需等待冷却与人工解除。"""
    now = utcnow()
    rows = session.execute(
        select(AuthChallenge).where(AuthChallenge.user_id == ctx.user_id, AuthChallenge.status == "pending")
    ).scalars().all()
    for row in rows:
        row.status = "locked"
        row.locked_until = now + timedelta(seconds=get_settings().challenge_lock_seconds)
        row.scope = {**(row.scope or {}), "lock_reason": reason}
    session.flush()