"""授权签发、范围校验与消费（V5 6.3、6.4、AUTH-04）。

两类授权：
- 精确授权 exact：绑定用户、计划修订、操作、账户、对象、金额、费用上限、时间窗、参数哈希，单次使用。
- 范围授权 scoped：额外列出允许的商户／产品集合、每笔与总额上限、最多次数、截止时间与可恢复动作；
  后续修订只有经确定性检查属于已批准范围的子集才能沿用，且共享额度与次数（不能靠新 operation_id 重置）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..ids import new_id, scope_hash as compute_scope_hash
from ..models import Grant, GrantUse, Operation, PlanStep
from ..timeutil import utcnow
from .identity import AuthContext

EXACT = "exact"
SCOPED = "scoped"


def _expired(grant: Grant, now: datetime | None = None) -> bool:
    return grant.expires_at <= (now or utcnow())


def _clean_scope(scope: dict) -> dict:
    """范围哈希只对约束字段计算，避免把上一次计算的 scope_hash 混入导致摘要漂移。"""
    return {key: value for key, value in scope.items() if key != "scope_hash"}


def create_exact_grant(
    session: Session,
    ctx: AuthContext,
    *,
    plan_id: str | None,
    plan_revision: int,
    scope: dict,
    required_level: str,
    ttl_seconds: int | None = None,
    verification_level: str = "NONE",
) -> Grant:
    """签发精确授权；YELLOW 在用户明确确认后即 active，RED 需先完成强验证。"""
    ctx.require_customer()
    settings = get_settings()
    now = utcnow()
    scope = dict(scope)
    scope.setdefault("kind", EXACT)
    digest = compute_scope_hash(_clean_scope(scope))
    status = "active" if required_level == "YELLOW" else "pending"
    grant = Grant(
        grant_id=new_id("grant"),
        owner_user_id=ctx.user_id,
        task_id=scope.get("task_id"),
        plan_id=plan_id,
        plan_revision=plan_revision,
        scope_kind=EXACT,
        scope_hash=digest,
        scope=scope,
        policy_version=settings.app_mode and scope.get("policy_version", "policy-2026.10"),
        required_level=required_level,
        verification_level=verification_level,
        status=status,
        max_uses=1,
        used_count=0,
        max_total_minor=scope.get("amount_minor"),
        used_total_minor=0,
        approved_revision=plan_revision,
        lineage=[],
        expires_at=now + timedelta(seconds=ttl_seconds or settings.grant_exact_ttl_seconds),
        created_at=now,
    )
    session.add(grant)
    session.flush()
    return grant


def create_scoped_grant(
    session: Session,
    ctx: AuthContext,
    *,
    plan_id: str | None,
    plan_revision: int,
    scope: dict,
    required_level: str,
    max_uses: int,
    max_total_minor: int,
    expires_at: datetime,
    verification_level: str = "NONE",
) -> Grant:
    ctx.require_customer()
    if max_uses <= 0 or max_total_minor <= 0:
        raise FinPilotError("PARAM_INVALID", "范围授权必须给出正的次数与总额上限。")
    scope = dict(scope)
    scope.setdefault("kind", SCOPED)
    scope["max_uses"] = max_uses
    scope["max_total_minor"] = max_total_minor
    scope["deadline"] = expires_at.isoformat()
    digest = compute_scope_hash(_clean_scope(scope))
    grant = Grant(
        grant_id=new_id("grant"),
        owner_user_id=ctx.user_id,
        task_id=scope.get("task_id"),
        plan_id=plan_id,
        plan_revision=plan_revision,
        scope_kind=SCOPED,
        scope_hash=digest,
        scope=scope,
        policy_version=scope.get("policy_version", "policy-2026.10"),
        required_level=required_level,
        verification_level=verification_level,
        status="pending" if required_level == "RED" else "active",
        max_uses=max_uses,
        used_count=0,
        max_total_minor=max_total_minor,
        used_total_minor=0,
        approved_revision=plan_revision,
        lineage=[plan_revision],
        expires_at=expires_at,
        created_at=utcnow(),
    )
    session.add(grant)
    session.flush()
    return grant


def activate_grant(session: Session, ctx: AuthContext, *, grant_id: str, verification_level: str) -> Grant:
    grant = session.execute(select(Grant).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
    if grant is None or grant.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该授权。")
    if grant.status == "active":
        return grant
    if grant.status != "pending":
        raise FinPilotError("GRANT_INVALID", "授权状态不允许激活。")
    if _expired(grant):
        grant.status = "expired"
        session.flush()
        raise FinPilotError("GRANT_INVALID", "授权已过期，请重新确认。")
    from .state_machine import assert_transition

    assert_transition("grant", grant.status, "active")
    grant.status = "active"
    grant.verification_level = verification_level
    grant.version += 1
    session.flush()
    return grant


def _lock_grant(session: Session, grant_id: str) -> Grant:
    grant = session.execute(select(Grant).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
    if grant is None:
        raise FinPilotError("GRANT_INVALID", "授权不存在或已失效。")
    return grant


def scope_is_subset(approved: dict, candidate: dict) -> bool:
    """确定性范围子集检查：候选只能比批准范围更窄。"""
    for key, cand in candidate.items():
        if key in ("kind", "task_id", "plan_id", "scope_hash"):
            continue
        if key not in approved:
            return False
        appr = approved[key]
        if isinstance(appr, (int, float)) and isinstance(cand, (int, float)) and not isinstance(cand, bool):
            # 金额、次数、时长等上限字段：候选必须不大于批准值
            if key.startswith(("max_", "amount", "fee", "quantity", "limit")):
                if cand > appr:
                    return False
            elif key.startswith(("min_",)):
                if cand < appr:
                    return False
            elif cand != appr:
                return False
        elif isinstance(appr, dict) and isinstance(cand, dict):
            if not scope_is_subset(appr, cand):
                return False
        elif isinstance(appr, list) and isinstance(cand, list):
            if not all(item in appr for item in cand):
                return False
        else:
            if json.dumps(appr, sort_keys=True, ensure_ascii=False) != json.dumps(cand, sort_keys=True, ensure_ascii=False):
                return False
    return True


def grant_covers(
    grant: Grant,
    *,
    plan_revision: int,
    candidate_scope: dict,
    amount_minor: int,
    parameter_hash: str | None = None,
) -> tuple[bool, str]:
    """判断授权是否覆盖某次候选操作。返回 (是否覆盖, 原因码)。"""
    now = utcnow()
    if grant.status in ("revoked", "expired"):
        return False, "grant_not_active"
    if grant.status == "consumed":
        return False, "grant_consumed"
    if _expired(grant, now):
        return False, "grant_expired"
    if grant.status == "pending":
        return False, "grant_pending_verification"

    if grant.scope_kind == EXACT:
        if plan_revision != grant.approved_revision:
            return False, "exact_grant_revision_mismatch"
        bound = grant.scope.get("parameters_hash")
        if bound and parameter_hash and bound != parameter_hash:
            return False, "parameter_hash_mismatch"
        bound_op = grant.scope.get("operation_id")
        if bound_op and candidate_scope.get("operation_id") and bound_op != candidate_scope.get("operation_id"):
            return False, "operation_mismatch"
        if grant.used_count >= grant.max_uses:
            return False, "grant_already_used"
        return True, "exact_match"

    # scoped：按批准范围逐项检查（工具、账户、金额、商户／产品、步骤绑定、期限）
    allowed_actions = grant.scope.get("allowed_tools") or grant.scope.get("allowed_actions") or []
    tool = candidate_scope.get("tool")
    if allowed_actions and tool not in allowed_actions:
        return False, "tool_out_of_scope"

    deadline_raw = grant.scope.get("deadline")
    if deadline_raw:
        try:
            from datetime import datetime as _dt

            if now > _dt.fromisoformat(str(deadline_raw).replace("Z", "+00:00")):
                return False, "scope_deadline_passed"
        except ValueError:
            pass

    accounts = grant.scope.get("allowed_account_ids") or []
    candidate_account = candidate_scope.get("account_id")
    if accounts and candidate_account and candidate_account not in accounts:
        return False, "account_out_of_scope"

    per_op_cap = grant.scope.get("max_amount_per_op_minor")
    if per_op_cap is not None and amount_minor > int(per_op_cap):
        return False, "per_operation_cap_exceeded"
    if grant.max_total_minor is not None and grant.used_total_minor + amount_minor > grant.max_total_minor:
        return False, "grant_total_cap_exceeded"
    if grant.used_count >= grant.max_uses:
        return False, "grant_uses_exhausted"

    merchants = grant.scope.get("merchants") or []
    if merchants and candidate_scope.get("merchant_id"):
        approved_ids = {m.get("merchant_id") if isinstance(m, dict) else m for m in merchants}
        if candidate_scope["merchant_id"] not in approved_ids:
            return False, "merchant_out_of_scope"

    products = grant.scope.get("products") or []
    if products and candidate_scope.get("product_id") and candidate_scope["product_id"] not in products:
        return False, "product_out_of_scope"

    steps = grant.scope.get("steps") or []
    if steps:
        matched = False
        for step in steps:
            if step.get("tool") != tool:
                continue
            if step.get("merchant_id") and candidate_scope.get("merchant_id") and step["merchant_id"] != candidate_scope["merchant_id"]:
                continue
            if step.get("product_id") and candidate_scope.get("product_id") and step["product_id"] != candidate_scope["product_id"]:
                continue
            if step.get("payee_id") and candidate_scope.get("payee_id") and step["payee_id"] != candidate_scope["payee_id"]:
                continue
            if step.get("card_id") and candidate_scope.get("card_id") and step["card_id"] != candidate_scope["card_id"]:
                continue
            if step.get("subscription_id") and candidate_scope.get("subscription_id") and step["subscription_id"] != candidate_scope["subscription_id"]:
                continue
            step_amount = int(step.get("amount_minor") or 0)
            if step_amount and amount_minor > step_amount:
                continue
            matched = True
            break
        if not matched:
            return False, "step_out_of_scope"
    return True, "scoped_subset"


def consume_grant(
    session: Session,
    ctx: AuthContext,
    *,
    grant_id: str,
    operation_id: str,
    parameter_hash: str,
    amount_minor: int,
) -> GrantUse:
    """在提交事务内消费授权：同一授权不能被两个操作重复使用。"""
    grant = _lock_grant(session, grant_id)
    if grant.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该授权。")

    existing = session.execute(
        select(GrantUse).where(GrantUse.grant_id == grant_id, GrantUse.operation_id == operation_id)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.authorized_parameter_hash != parameter_hash:
            raise FinPilotError("IDEMPOTENCY_CONFLICT", "同一授权被用于不同参数的操作。")
        return existing

    other = session.execute(select(GrantUse).where(GrantUse.operation_id == operation_id)).scalar_one_or_none()
    if other is not None and other.grant_id != grant_id:
        raise FinPilotError("GRANT_INVALID", "该操作已绑定其他授权。")

    if grant.status != "active":
        raise FinPilotError("GRANT_INVALID", f"授权状态为 {grant.status}，不能使用。")
    if _expired(grant):
        grant.status = "expired"
        session.flush()
        raise FinPilotError("GRANT_INVALID", "授权已过期，请重新确认。")
    if grant.used_count >= grant.max_uses:
        raise FinPilotError("GRANT_INVALID", "授权可用次数已用尽。")
    if grant.scope_kind == EXACT:
        bound = grant.scope.get("parameters_hash")
        if bound and bound != parameter_hash:
            raise FinPilotError("GRANT_INVALID", "授权参数与实际操作不一致。")
    else:
        if grant.max_total_minor is not None and grant.used_total_minor + amount_minor > grant.max_total_minor:
            raise FinPilotError("GRANT_SCOPE_VIOLATION", "超过范围授权累计额度。")

    now = utcnow()
    use = GrantUse(
        grant_id=grant_id,
        operation_id=operation_id,
        authorized_parameter_hash=parameter_hash,
        amount_minor=amount_minor,
        use_status="consumed",
        created_at=now,
    )
    session.add(use)
    grant.used_count += 1
    grant.used_total_minor += amount_minor
    if grant.used_count >= grant.max_uses or (
        grant.max_total_minor is not None and grant.used_total_minor >= grant.max_total_minor
    ):
        grant.status = "consumed"
    grant.version += 1
    session.flush()
    return use


def release_grant_use(session: Session, *, grant_id: str, operation_id: str) -> None:
    """操作确认未产生效果时，允许释放该次使用计数（不重置总额已经使用的部分）。"""
    use = session.execute(
        select(GrantUse).where(GrantUse.grant_id == grant_id, GrantUse.operation_id == operation_id)
    ).scalar_one_or_none()
    if use is None or use.use_status == "released":
        return
    grant = _lock_grant(session, grant_id)
    use.use_status = "released"
    grant.used_count = max(0, grant.used_count - 1)
    grant.used_total_minor = max(0, grant.used_total_minor - use.amount_minor)
    if grant.status == "consumed" and grant.used_count < grant.max_uses:
        grant.status = "active"
    grant.version += 1
    session.flush()


def revoke_grant(session: Session, ctx: AuthContext, *, grant_id: str) -> dict:
    """撤销授权：只停止尚未提交的动作；已提交与未知结果继续核验。"""
    grant = _lock_grant(session, grant_id)
    if grant.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该授权。")
    if grant.status not in ("active", "pending"):
        return {"grant": _grant_view(grant), "stopped_steps": [], "inflight_operations": []}
    grant.status = "revoked"
    grant.revoked_at = utcnow()
    grant.version += 1

    stopped: list[str] = []
    if grant.plan_id:
        steps = session.execute(
            select(PlanStep).where(PlanStep.plan_id == grant.plan_id, PlanStep.status.in_(("pending", "ready")))
        ).scalars().all()
        for step in steps:
            step.status = "stopped"
            step.last_error = "grant_revoked"
            stopped.append(step.node_id)

    inflight = session.execute(
        select(Operation.operation_id).where(
            Operation.owner_user_id == ctx.user_id,
            Operation.grant_id == grant_id,
            Operation.status.in_(("dispatching", "pending", "unknown")),
        )
    ).scalars().all()
    session.flush()
    return {"grant": _grant_view(grant), "stopped_steps": stopped, "inflight_operations": list(inflight)}


def expire_stale_grants(session: Session, *, now: datetime | None = None) -> int:
    moment = now or utcnow()
    rows = session.execute(
        select(Grant).where(Grant.status.in_(("active", "pending")), Grant.expires_at <= moment).with_for_update(skip_locked=True)
    ).scalars().all()
    for grant in rows:
        grant.status = "expired"
        grant.version += 1
    session.flush()
    return len(rows)


def _grant_view(grant: Grant) -> dict:
    return {
        "grant_id": grant.grant_id,
        "status": grant.status,
        "scope_kind": grant.scope_kind,
        "scope_hash": grant.scope_hash,
        "required_level": grant.required_level,
        "verification_level": grant.verification_level,
        "plan_revision": grant.plan_revision,
        "used_count": grant.used_count,
        "max_uses": grant.max_uses,
        "used_total_minor": grant.used_total_minor,
        "max_total_minor": grant.max_total_minor,
        "expires_at": grant.expires_at.isoformat(),
    }


def grant_view(grant: Grant) -> dict:
    return _grant_view(grant)