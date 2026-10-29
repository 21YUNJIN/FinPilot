"""卡片领域服务（V5 12 节 S4-01/02/04；验收族 T12/T13/T17/T18）。

核心口径（与 SRS 12 节、INTERFACES.md 第 3 节策略表一致）：

1. **申请与卡生命周期分离**：``card.apply`` 只产生 ``CardApplication(status='submitted')``，
   申请成功不代表有卡；只有运营审批 ``approved`` 时才创建 ``Card(lifecycle_status='active')``。
   生命周期仅 ``inactive/active/lost/closed``，交易限制是**叠加集合**（用户锁、渠道锁、
   风控冻结、限额可同时存在、互不清除）。
2. **审批是独立运营角色动作**：``approve_application`` 要求 ``ctx.is_operator``，
   客户与 Agent 都不能自行批准；模拟审批规则公开可读且标注"模拟银行决策"。
3. **额度调整区分申请与批准**：客户侧 ``card.set_limit`` 只写 ``requested_limit_minor``，
   永远不直接改 ``Card.credit_limit_minor``；``approved_limit_minor`` 只由运营审批写入。
4. **挂失不自动清除风控冻结**；补卡走独立申请并建立新旧卡关系，旧卡按条款 ``closed``，
   新旧卡不得同时为无限制有效。
5. 卡号只使用测试号段且只保存脱敏结果（``**** **** **** 1234``），禁止真实卡号与安全码。

返回结构：``TOOL_HANDLERS`` 的处理器返回 DomainResult
``{"type", "data", "source", "version", "warnings"}``，其中 ``data`` 是
``contracts/schemas/{card,card_application,card_control}.json`` 描述的**合同对象**
（``additionalProperties: false``，因此展示型聚合字段一律放在 DomainResult 顶层，不塞进合同对象）。

本领域不产生任何资金效果；操作状态迁移只经 ``kernel.submission.transition``。
"""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...kernel.identity import AuthContext
from ...kernel.submission import transition
from ...models import Card, CardApplication, CardControl, Operation
from ...timeutil import format_iso, utcnow
from . import repository as repo

SOURCE = "cards"
DOMAIN = "cards"
DOMAIN_VERSION = "2026.10"
TERMS_VERSION = "2026.10"

# --- 卡号与敏感字段 -------------------------------------------------------

MASKED_PAN_TEMPLATE = "**** **** **** {last4}"
# 仅测试号段：本实现只持久化脱敏尾部四位（masked_pan），完整卡号与安全码一律不落库，也不进日志。
TEST_PAN_BIN = "625900"
SENSITIVE_ARG_FIELDS = frozenset(
    {"pan", "card_number", "card_no", "cardnumber", "cvv", "cvv2", "cvn", "security_code", "expiry", "expire", "track2", "pin"}
)
CARD_BRANDS = ("VIRTUAL", "TEST")
CONTROL_TYPES = ("user_lock", "channel_lock", "risk_freeze", "limit")
CONTROL_TYPE_ALIASES = {"user_limit": "limit", "risk_limit": "limit", "bank_freeze": "risk_freeze"}
CONTROL_CHANNELS = ("atm", "pos", "online", "contactless", "all")

# 模拟审批规则（公开可读，标注为模拟银行决策）
SIMULATED_DECISION_LABEL = "模拟银行决策"
SIMULATED_DECISION_VERSION = "sim-bank-2026.10"
SIMULATED_MAX_APPROVED_LIMIT_MINOR = 500_000  # 5000 元


def simulated_approval_rule() -> dict:
    """公开可读的模拟审批规则（不声称任何真实银行授信政策）。"""
    return {
        "label": SIMULATED_DECISION_LABEL,
        "version": SIMULATED_DECISION_VERSION,
        "statement": (
            "申请额度（requested_limit_minor）≤ 500000 分（5000 元），"
            "且该卡不存在未结清的被拒记录时批准；否则拒绝。"
        ),
        "max_approved_limit_minor": SIMULATED_MAX_APPROVED_LIMIT_MINOR,
        "approve_conditions": [
            "requested_limit_minor <= 500000",
            "同卡不存在 rejected 且参数未变更的 limit_change 申请",
        ],
        "reject_conditions": [
            "requested_limit_minor > 500000",
            "同卡存在未结清的被拒记录",
        ],
        "disclosure": "本规则是参考实现的模拟银行决策，不是任何真实银行的授信政策，也不产生真实授信。",
    }


def simulated_decision(session: Session, application: CardApplication) -> tuple[str, str]:
    """按公开模拟规则给出（'approve'|'reject', 原因码）。"""
    if int(application.requested_limit_minor or 0) > SIMULATED_MAX_APPROVED_LIMIT_MINOR:
        return "reject", "requested_limit_over_simulated_cap"
    if application.card_id:
        sibling = repo.rejected_limit_change(
            session, application.card_id, exclude_application_id=application.application_id
        )
        if sibling is not None:
            return "reject", "previously_rejected_unsettled"
    return "approve", "within_simulated_rule"


# --- 状态迁移（卡片领域自有表；kernel 状态机未登记卡片类型） ----------------

APPLICATION_TRANSITIONS: dict[str, set[str]] = {
    "submitted": {"under_review", "approved", "rejected", "withdrawn"},
    "under_review": {"approved", "rejected", "withdrawn"},
    "approved": set(),
    "rejected": set(),
    "withdrawn": set(),
}

CARD_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    "inactive": {"active", "closed"},
    "active": {"lost", "closed"},
    "lost": {"active", "closed"},
    "closed": set(),
}


def assert_application_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in APPLICATION_TRANSITIONS.get(current, set()):
        raise FinPilotError(
            "POLICY_DENIED",
            f"非法申请状态转移：{current} → {target}",
            details={"reason_codes": ["illegal_transition"], "from": current, "to": target},
        )


def assert_card_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in CARD_LIFECYCLE_TRANSITIONS.get(current, set()):
        raise FinPilotError(
            "POLICY_DENIED",
            f"非法卡片生命周期转移：{current} → {target}",
            details={"reason_codes": ["illegal_transition"], "from": current, "to": target},
        )


# --- 卡控叠加视图 ---------------------------------------------------------


def can_release(control: CardControl, actor_role: str) -> bool:
    """控制是否可由该角色解除；风控冻结只能由独立运营／风控角色解除。"""
    role = (actor_role or "").lower()
    is_operator = role in ("operator", "risk_officer", "risk", "bank_operator", "system")
    if not control.active:
        return False
    if control.control_type in ("risk_freeze", "bank_freeze"):
        return is_operator
    if control.control_type in ("user_lock", "channel_lock", "limit", "user_limit", "risk_limit"):
        return True
    return is_operator


def effective_controls(controls: list[CardControl]) -> dict:
    """叠加后的限制视图：多条控制同时生效，互不清除。"""
    active = [c for c in controls if c.active]
    always_blocking = sorted(c.control_type for c in active if c.control_type in ("user_lock", "risk_freeze", "bank_freeze"))
    channel_locks = sorted(c.channel for c in active if c.control_type == "channel_lock" and c.channel)
    limits = [int(c.limit_minor) for c in active if c.control_type == "limit" and c.limit_minor is not None]
    return {
        "count": len(active),
        "blocking": bool(always_blocking or channel_locks or limits),
        "always_blocking_types": always_blocking,
        "user_locked": "user_lock" in always_blocking,
        "risk_frozen": any(t in ("risk_freeze", "bank_freeze") for t in always_blocking),
        "freeze_types": sorted({t for t in always_blocking if t in ("risk_freeze", "bank_freeze")}),
        "channel_locks": channel_locks,
        "limits_minor": limits,
        "min_limit_minor": min(limits) if limits else None,
        "control_types": sorted({c.control_type for c in active}),
        "control_ids": [c.control_id for c in active],
        "created_by_roles": sorted({c.created_by_role for c in active if c.created_by_role}),
        "releasable_by_customer": [c.control_id for c in active if can_release(c, "customer")],
        "operator_only": [c.control_id for c in active if not can_release(c, "customer")],
    }


def control_block_reasons(
    view: dict,
    *,
    card_status: str | None = None,
    channel: str = "online",
    amount_minor: int = 0,
) -> list[str]:
    """与 ``security.policy.card_control_block_reason`` 同规则的原因码推导。

    多重集必须与策略层一致：同一卡存在多条同类控制时原因码可重复。
    """
    reasons: list[str] = []
    if card_status is not None and card_status != "active":
        reasons.append(f"card_{card_status}")
    reasons.extend(view.get("always_blocking_types", []))
    for locked_channel in view.get("channel_locks", []):
        if locked_channel == channel:
            reasons.append("channel_lock")
    for limit in view.get("limits_minor", []):
        if amount_minor > limit:
            reasons.append("over_limit")
    return reasons


# --- 合同对象视图（严格按 contracts/schemas/*.json，additionalProperties=false） ---


def masked_pan_for_test_card() -> str:
    """生成脱敏卡号（``**** **** **** 1234``）；只保存该脱敏结果，不生成也不保存完整卡号。"""
    last4 = f"{secrets.randbelow(10_000):04d}"
    return MASKED_PAN_TEMPLATE.format(last4=last4)


def control_view(control: CardControl) -> dict:
    """CardControl 合同对象（contracts/schemas/card_control.json）。"""
    return {
        "control_id": control.control_id,
        "card_id": control.card_id,
        "control_type": control.control_type,
        "channel": control.channel,
        "limit_minor": control.limit_minor,
        "reason": control.reason,
        "created_by": control.created_by,
        "created_by_role": control.created_by_role,
        "active": bool(control.active),
        "released_by": control.released_by,
        "released_at": format_iso(control.released_at) if control.released_at else None,
        "created_at": format_iso(control.created_at),
    }


def card_view(session: Session, card: Card) -> dict:
    """Card 合同对象（contracts/schemas/card.json）：仅脱敏卡号，controls 为叠加集合。"""
    controls = repo.controls_for_card(session, card.card_id)
    return {
        "card_id": card.card_id,
        "owner_user_id": card.owner_user_id,
        "account_id": card.account_id,
        "masked_pan": card.masked_pan,
        "brand": card.brand,
        "lifecycle_status": card.lifecycle_status,
        "credit_limit_minor": card.credit_limit_minor,
        "replaces_card_id": card.replaces_card_id,
        "replaced_by_card_id": card.replaced_by_card_id,
        "controls": [control_view(c) for c in controls],
        "effective_controls": effective_controls(controls),
        "version": card.version,
        "created_at": format_iso(card.created_at),
        "updated_at": format_iso(card.updated_at),
    }


def card_blocked_reasons(session: Session, card: Card, *, channel: str = "online", amount_minor: int = 0) -> list[str]:
    """阻止交易的原因码：与 ``security.policy.card_control_block_reason`` 一致。"""
    view = effective_controls(repo.controls_for_card(session, card.card_id))
    return control_block_reasons(view, card_status=card.lifecycle_status, channel=channel, amount_minor=amount_minor)


def application_view(session: Session, application: CardApplication) -> dict:
    """CardApplication 合同对象（contracts/schemas/card_application.json）。"""
    return {
        "application_id": application.application_id,
        "owner_user_id": application.owner_user_id,
        "account_id": application.account_id,
        "kind": application.kind,
        "card_id": application.card_id,
        "requested_limit_minor": application.requested_limit_minor,
        "approved_limit_minor": application.approved_limit_minor,
        "application_status": application.application_status,
        "decision_reason": application.decision_reason,
        "decided_by": application.decided_by,
        "decided_at": format_iso(application.decided_at) if application.decided_at else None,
        "operation_id": application.operation_id,
        "version": application.version,
        "created_at": format_iso(application.created_at),
    }


# --- 处理器公共部分 -------------------------------------------------------


def _reject_sensitive_args(typed_args: dict) -> None:
    offending = sorted(SENSITIVE_ARG_FIELDS.intersection({str(k).lower() for k in typed_args}))
    if offending:
        raise FinPilotError(
            "PARAM_INVALID",
            "卡片请求不得包含卡号或安全码等敏感字段。",
            details={"reason_codes": ["sensitive_field_rejected"], "fields": offending},
        )


def _cached_result(session: Session, operation: Operation) -> dict | None:
    """幂等：同一 operation_id 已产生结果时直接返回，不再新建业务记录。"""
    if operation.domain_result:
        return dict(operation.domain_result)
    row = session.get(Operation, operation.operation_id)
    if row is not None and row.domain_result:
        return dict(row.domain_result)
    return None


def _result(operation: Operation, kind: str, data: dict, warnings: list[str] | None = None, **extra) -> dict:
    """DomainResult：顶层带领域聚合信息，``data`` 保持为合同对象。"""
    result = {
        "type": kind,
        "data": data,
        "source": SOURCE,
        "version": DOMAIN_VERSION,
        "warnings": list(warnings or []),
        # 与 contracts/schemas/domain_result.json 对齐的别名（便于网关与契约消费者读取）
        "domain": DOMAIN,
        "result_type": kind,
        "terms_version": TERMS_VERSION,
        "observed_at": format_iso(utcnow()),
        "operation_id": operation.operation_id if operation is not None else None,
        "task_id": operation.task_id if operation is not None else None,
    }
    result.update(extra)
    return result


def _remember(session: Session, operation: Operation, result: dict) -> dict:
    """在同一事务内登记领域结果并结束操作（纯领域写入无提供方回调）。"""
    operation.domain_result = result
    operation.updated_at = utcnow()
    if operation.status in ("dispatching", "pending"):
        transition(
            session,
            operation,
            "succeeded",
            reason="domain_effect_confirmed",
            detail={"source": SOURCE, "type": result["type"]},
        )
    session.flush()
    return result


def _int_arg(args: dict, key: str, default: int | None = None) -> int | None:
    value = args.get(key, default)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FinPilotError("PARAM_INVALID", f"{key} 必须是整数分。")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FinPilotError("PARAM_INVALID", f"{key} 必须是整数分。") from exc


def _new_application(
    session: Session,
    *,
    ctx: AuthContext,
    kind: str,
    account_id: str,
    card_id: str | None,
    requested_limit_minor: int,
    operation_id: str,
) -> CardApplication:
    now = utcnow()
    application = CardApplication(
        owner_user_id=ctx.user_id,
        account_id=account_id,
        kind=kind,
        card_id=card_id,
        requested_limit_minor=int(requested_limit_minor),
        approved_limit_minor=None,
        application_status="submitted",
        decision_reason=None,
        decided_by=None,
        decided_at=None,
        operation_id=operation_id,
        version=1,
        created_at=now,
    )
    session.add(application)
    session.flush()
    return application


# --- 写处理器：card.apply --------------------------------------------------


def apply_card(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """虚拟卡申请：只登记申请，不创建卡、不产生额度。"""
    ctx.require_customer()
    cached = _cached_result(session, operation)
    if cached:
        return cached
    existing = repo.application_by_operation(session, operation.operation_id)
    if existing is not None:
        return _remember(session, operation, _result(operation, "card_application", application_view(session, existing)))

    args = dict(operation.typed_args or {})
    _reject_sensitive_args(args)
    if args.get("card_id"):
        raise FinPilotError(
            "PARAM_INVALID",
            "补卡请使用 card.replace，额度调整请使用 card.set_limit。",
            details={"reason_codes": ["use_dedicated_tool"]},
        )
    kind = str(args.get("kind") or "new_card")
    if kind != "new_card":
        raise FinPilotError(
            "PARAM_INVALID",
            "card.apply 只受理新卡申请。",
            details={"reason_codes": ["unsupported_application_kind"], "kind": kind},
        )
    account = repo.require_owned_account(session, ctx, args.get("account_id") or operation.source_account_id)
    requested = _int_arg(args, "requested_limit_minor", 0) or 0
    if requested < 0:
        raise FinPilotError("PARAM_INVALID", "申请额度不能为负。", details={"reason_codes": ["negative_limit"]})

    application = _new_application(
        session,
        ctx=ctx,
        kind="new_card",
        account_id=account.account_id,
        card_id=None,
        requested_limit_minor=requested,
        operation_id=operation.operation_id,
    )
    result = _result(
        operation,
        "card_application",
        application_view(session, application),
        warnings=[SIMULATED_DECISION_LABEL],
        produces_card=False,
        simulated_decision_rule=simulated_approval_rule(),
        note="申请成功仅表示已提交，批准后才会创建有效卡。",
    )
    return _remember(session, operation, result)


# --- 写处理器：card.set_limit ---------------------------------------------


def request_limit_change(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """额度调整申请：客户侧只写 requested_limit_minor，绝不直接改卡额度。"""
    ctx.require_customer()
    cached = _cached_result(session, operation)
    if cached:
        return cached
    existing = repo.application_by_operation(session, operation.operation_id)
    if existing is not None:
        return _remember(session, operation, _result(operation, "card_application", application_view(session, existing)))

    args = dict(operation.typed_args or {})
    _reject_sensitive_args(args)
    card = repo.require_owned_card(session, ctx, args.get("card_id"))

    expected_version = _int_arg(args, "expected_card_version")
    if expected_version is not None and expected_version != card.version:
        raise FinPilotError(
            "VERSION_CONFLICT",
            "卡片已被其他操作更新，请刷新后重试。",
            details={"reason_codes": ["version_mismatch"], "current_version": card.version},
        )

    direction = str(args.get("direction") or "increase").lower()
    if direction in ("increase", "up", "raise", "higher"):
        direction = "increase"
    elif direction in ("decrease", "down", "lower", "reduce"):
        direction = "decrease"
    else:
        raise FinPilotError(
            "PARAM_INVALID",
            "direction 只能是 increase 或 decrease。",
            details={"reason_codes": ["unsupported_direction"], "direction": direction},
        )

    requested = _int_arg(args, "requested_limit_minor")
    if requested is None or requested <= 0:
        raise FinPilotError(
            "PARAM_INVALID",
            "必须给出正的目标额度 requested_limit_minor。",
            details={"reason_codes": ["requested_limit_required"]},
        )
    if direction == "increase" and requested <= card.credit_limit_minor:
        raise FinPilotError(
            "PARAM_INVALID",
            "提额申请的目标额度必须高于当前额度。",
            details={"reason_codes": ["not_an_increase"], "current_limit_minor": card.credit_limit_minor},
        )
    if direction == "decrease" and requested >= card.credit_limit_minor:
        raise FinPilotError(
            "PARAM_INVALID",
            "降额申请的目标额度必须低于当前额度。",
            details={"reason_codes": ["not_a_decrease"], "current_limit_minor": card.credit_limit_minor},
        )

    # 被拒的提额申请不能被重新发问变成批准：参数未变更的重复申请直接拒绝
    if direction == "increase":
        rejected = repo.rejected_limit_change(session, card.card_id, requested)
        if rejected is not None:
            raise FinPilotError(
                "POLICY_DENIED",
                "该提额申请已被拒绝，参数未变更前不能重复申请。",
                details={
                    "reason_codes": ["previously_rejected"],
                    "rejected_application_id": rejected.application_id,
                    "decided_at": format_iso(rejected.decided_at) if rejected.decided_at else None,
                },
            )

    application = _new_application(
        session,
        ctx=ctx,
        kind="limit_change",
        account_id=card.account_id,
        card_id=card.card_id,
        requested_limit_minor=requested,
        operation_id=operation.operation_id,
    )
    result = _result(
        operation,
        "card_application",
        application_view(session, application),
        warnings=[SIMULATED_DECISION_LABEL],
        direction=direction,
        current_credit_limit_minor=card.credit_limit_minor,
        card_limit_unchanged=True,
        simulated_decision_rule=simulated_approval_rule(),
        note="客户侧只提交申请；批准额度 approved_limit_minor 由运营审批写入。",
    )
    return _remember(session, operation, result)


# --- 写处理器：card.set_control -------------------------------------------


def set_card_control(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """叠加卡控：支持增加与解除；解除只允许调用者有权解除的控制。"""
    cached = _cached_result(session, operation)
    if cached:
        return cached
    args = dict(operation.typed_args or {})
    _reject_sensitive_args(args)
    card = repo.require_owned_card(session, ctx, args.get("card_id"))
    action = str(args.get("action") or "add").lower()
    if action in ("release", "remove", "clear", "unlock"):
        result = _release_control(session, ctx, operation, card, args)
    elif action in ("add", "create", "set", "lock"):
        result = _add_control(session, ctx, operation, card, args)
    else:
        raise FinPilotError(
            "PARAM_INVALID",
            "action 只能是 add 或 release。",
            details={"reason_codes": ["unsupported_action"], "action": action},
        )
    return _remember(session, operation, result)


def _actor_role(ctx: AuthContext) -> str:
    if "risk_officer" in ctx.roles:
        return "risk"
    if ctx.is_operator:
        return "operator"
    if ctx.is_customer:
        return "customer"
    return "system"


def _add_control(session: Session, ctx: AuthContext, operation: Operation, card: Card, args: dict) -> dict:
    raw_type = str(args.get("control_type") or "").lower()
    control_type = CONTROL_TYPE_ALIASES.get(raw_type, raw_type)
    if control_type not in CONTROL_TYPES:
        raise FinPilotError(
            "PARAM_INVALID",
            "control_type 只能是 user_lock / channel_lock / risk_freeze / limit。",
            details={"reason_codes": ["unsupported_control_type"], "control_type": raw_type},
        )
    if control_type == "risk_freeze" and not ctx.is_operator:
        raise FinPilotError(
            "POLICY_DENIED",
            "银行风控冻结只能由独立运营／风控角色设置。",
            details={"reason_codes": ["operator_only"]},
        )

    channel = args.get("channel")
    limit_minor = _int_arg(args, "limit_minor")
    if control_type == "channel_lock":
        if channel not in CONTROL_CHANNELS:
            raise FinPilotError(
                "PARAM_INVALID",
                "渠道锁必须给出受支持的 channel（atm/pos/online/contactless/all）。",
                details={"reason_codes": ["channel_required"], "channel": channel},
            )
    elif control_type == "limit":
        if limit_minor is None or limit_minor < 0:
            raise FinPilotError(
                "PARAM_INVALID",
                "限额控制必须给出非负的 limit_minor。",
                details={"reason_codes": ["limit_required"]},
            )
    elif channel or limit_minor is not None:
        raise FinPilotError(
            "PARAM_INVALID",
            "该控制类型不接受 channel 或 limit_minor。",
            details={"reason_codes": ["unexpected_field"]},
        )

    now = utcnow()
    role = "risk" if control_type == "risk_freeze" else _actor_role(ctx)
    control = CardControl(
        card_id=card.card_id,
        control_type=control_type,
        channel=channel if control_type == "channel_lock" else None,
        limit_minor=limit_minor if control_type == "limit" else None,
        reason=str(args.get("reason") or ""),
        created_by=ctx.user_id,
        created_by_role=role,
        active=True,
        created_at=now,
    )
    session.add(control)
    session.flush()
    view = card_view(session, card)
    return _result(
        operation,
        "card_control",
        control_view(control),
        action="add",
        card=view,
        effective_controls=view["effective_controls"],
        note="卡控为叠加集合：新增控制不清除既有控制。",
    )


def _release_control(session: Session, ctx: AuthContext, operation: Operation, card: Card, args: dict) -> dict:
    control = repo.get_control(session, args.get("control_id"))
    if control is None:
        raw_type = str(args.get("control_type") or "").lower()
        control_type = CONTROL_TYPE_ALIASES.get(raw_type, raw_type)
        if not control_type:
            raise FinPilotError(
                "PARAM_INVALID",
                "解除控制必须给出 control_id 或 control_type。",
                details={"reason_codes": ["control_reference_required"]},
            )
        control = repo.find_active_control(
            session,
            card.card_id,
            control_type=control_type,
            channel=args.get("channel") if control_type == "channel_lock" else None,
        )
    if control is None or control.card_id != card.card_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该卡控。")

    if not can_release(control, _actor_role(ctx)):
        raise FinPilotError(
            "POLICY_DENIED",
            "当前身份无权解除该控制。",
            details={"reason_codes": ["control_release_not_allowed"], "control_type": control.control_type},
        )

    if not control.active:
        view = card_view(session, card)
        return _result(
            operation,
            "card_control",
            control_view(control),
            action="release",
            already_released=True,
            card=view,
            effective_controls=view["effective_controls"],
        )

    control.active = False
    control.released_by = ctx.user_id
    control.released_at = utcnow()
    session.flush()
    view = card_view(session, card)
    return _result(
        operation,
        "card_control",
        control_view(control),
        action="release",
        card=view,
        effective_controls=view["effective_controls"],
        note="只解除本条控制，其余控制继续生效。",
    )


# --- 写处理器：card.report_lost -------------------------------------------


def report_lost(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """挂失／解挂：active → lost，lost → active；挂失不清除任何风控冻结。"""
    cached = _cached_result(session, operation)
    if cached:
        return cached
    args = dict(operation.typed_args or {})
    _reject_sensitive_args(args)
    card = repo.require_owned_card(session, ctx, args.get("card_id"))

    action = str(args.get("action") or "lost").lower()
    if action in ("lost", "report_lost", "freeze", "report"):
        target = "lost"
    elif action in ("unfreeze", "unlost", "restore", "active", "recover"):
        target = "active"
    else:
        raise FinPilotError(
            "PARAM_INVALID",
            "action 只能是 lost 或 unfreeze。",
            details={"reason_codes": ["unsupported_action"], "action": action},
        )

    assert_card_transition(card.lifecycle_status, target)
    unchanged = card.lifecycle_status == target
    if not unchanged:
        card.lifecycle_status = target
        card.version += 1
        card.updated_at = utcnow()
        session.flush()

    view = card_view(session, card)
    active_controls = [c for c in view["controls"] if c["active"]]
    return _remember(
        session,
        operation,
        _result(
            operation,
            "card_lifecycle",
            view,
            action=target,
            already_in_state=unchanged,
            controls_preserved=active_controls,
            risk_freeze_preserved=view["effective_controls"]["risk_frozen"],
            blocked_reasons=card_blocked_reasons(session, card),
            note="挂失／解挂不改变交易限制：银行风控冻结需要独立运营复核才能解除。",
        ),
    )


# --- 写处理器：card.replace ------------------------------------------------


def replace_card(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """补卡：独立申请关联旧卡；批准后才建新卡并关闭旧卡。"""
    ctx.require_customer()
    cached = _cached_result(session, operation)
    if cached:
        return cached
    existing = repo.application_by_operation(session, operation.operation_id)
    if existing is not None:
        return _remember(session, operation, _result(operation, "card_application", application_view(session, existing)))

    args = dict(operation.typed_args or {})
    _reject_sensitive_args(args)
    old_card = repo.require_owned_card(session, ctx, args.get("card_id"))
    if old_card.lifecycle_status == "closed":
        raise FinPilotError(
            "POLICY_DENIED",
            "已关闭的卡不能补卡。",
            details={"reason_codes": ["card_closed"]},
        )
    if old_card.replaced_by_card_id:
        raise FinPilotError(
            "POLICY_DENIED",
            "该卡已完成补卡。",
            details={"reason_codes": ["card_already_replaced"], "new_card_id": old_card.replaced_by_card_id},
        )
    if repo.open_replacement(session, old_card.card_id) is not None:
        raise FinPilotError(
            "POLICY_DENIED",
            "该卡已有在途补卡申请。",
            details={"reason_codes": ["replacement_already_open"]},
        )

    application = _new_application(
        session,
        ctx=ctx,
        kind="replacement",
        account_id=old_card.account_id,
        card_id=old_card.card_id,
        requested_limit_minor=old_card.credit_limit_minor,
        operation_id=operation.operation_id,
    )
    result = _result(
        operation,
        "card_application",
        application_view(session, application),
        warnings=["新卡创建前旧卡保持原状态；批准后不得同时存在两张无限制有效卡。"],
        replaces_card=card_view(session, old_card),
        note="补卡是独立申请：批准后创建新卡、建立新旧卡关系，旧卡按条款关闭。",
    )
    return _remember(session, operation, result)


# --- 运营动作 -------------------------------------------------------------


def start_review(session: Session, operator_ctx: AuthContext, application_id: str) -> dict:
    """进入审核（submitted → under_review）：运营角色动作，也可由模拟规则推进。"""
    if not operator_ctx.is_operator:
        raise FinPilotError(
            "POLICY_DENIED",
            "审核推进是独立运营角色动作。",
            details={"reason_codes": ["operator_role_required"]},
        )
    application = repo.get_application(session, application_id)
    if application is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该申请。")
    assert_application_transition(application.application_status, "under_review")
    if application.application_status != "under_review":
        application.application_status = "under_review"
        application.version += 1
        session.flush()
    return application_view(session, application)


def auto_review(session: Session, operator_ctx: AuthContext, application_id: str) -> dict:
    """按公开模拟规则推进：under_review → approved / rejected（仍要求运营角色）。"""
    if not operator_ctx.is_operator:
        raise FinPilotError(
            "POLICY_DENIED",
            "模拟审批是独立运营角色动作。",
            details={"reason_codes": ["operator_role_required"]},
        )
    application = repo.get_application(session, application_id)
    if application is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该申请。")
    if application.application_status in ("approved", "rejected", "withdrawn"):
        raise FinPilotError(
            "POLICY_DENIED",
            "该申请已处于终态，不能重复决策。",
            details={"reason_codes": ["application_terminal"], "application_status": application.application_status},
        )
    if application.application_status == "submitted":
        start_review(session, operator_ctx, application_id)
    decision, reason_code = simulated_decision(session, application)
    return approve_application(
        session,
        operator_ctx,
        application_id,
        decision == "approve",
        application.requested_limit_minor,
        f"{SIMULATED_DECISION_LABEL}（{SIMULATED_DECISION_VERSION}）：{reason_code}",
    )


def approve_application(
    session: Session,
    operator_ctx: AuthContext,
    application_id: str,
    approve: bool,
    approved_limit_minor: int | None = None,
    reason: str = "",
) -> dict:
    """运营审批：独立角色动作；批准时才创建有效卡或写入批准额度。

    - 客户与 Agent 不能自行批准（``POLICY_DENIED``）。
    - 只履行原客户请求：批准额度不得超过申请额度，超出须客户重新授权。
    - 记录 ``decided_by / decided_at / decision_reason``。
    """
    if not operator_ctx.is_operator:
        raise FinPilotError(
            "POLICY_DENIED",
            "审批是独立运营角色动作，客户与 Agent 不能自行批准。",
            details={"reason_codes": ["operator_role_required"]},
        )
    application = repo.get_application(session, application_id)
    if application is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该申请。")
    if application.application_status in ("approved", "rejected", "withdrawn"):
        raise FinPilotError(
            "POLICY_DENIED",
            "该申请已处于终态，不能重复决策。",
            details={"reason_codes": ["application_terminal"], "application_status": application.application_status},
        )
    if application.application_status == "submitted":
        assert_application_transition("submitted", "under_review")
        application.application_status = "under_review"

    warnings: list[str] = []
    created_card: Card | None = None
    approved_limit: int | None = None

    if approve:
        approved_limit = _resolve_approved_limit(session, application, approved_limit_minor)
        created_card = _apply_approval(session, application, approved_limit, reason)
        target_status = "approved"
        if approved_limit > SIMULATED_MAX_APPROVED_LIMIT_MINOR:
            warnings.append(f"批准额度超过模拟规则上限 {SIMULATED_MAX_APPROVED_LIMIT_MINOR} 分，已按运营决策记录。")
    else:
        target_status = "rejected"

    assert_application_transition(application.application_status, target_status)
    application.application_status = target_status
    application.approved_limit_minor = approved_limit
    application.decision_reason = reason or ("operator_approved" if approve else "operator_rejected")
    application.decided_by = operator_ctx.user_id
    application.decided_at = utcnow()
    application.version += 1
    session.flush()

    return _result(
        None,
        "card_application",
        application_view(session, application),
        warnings=warnings,
        approved_by_operator=bool(approve),
        created_card=card_view(session, created_card) if created_card is not None else None,
        simulated_decision_rule=simulated_approval_rule(),
        note="批准只履行原客户申请；改变金额或条款须客户重新授权。",
    )


def _resolve_approved_limit(session: Session, application: CardApplication, approved_limit_minor: int | None) -> int:
    requested = int(application.requested_limit_minor or 0)
    if application.kind == "replacement":
        old_card = repo.get_card(session, application.card_id)
        if old_card is None:
            raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问待补卡的旧卡。")
        if approved_limit_minor is not None and int(approved_limit_minor) != old_card.credit_limit_minor:
            raise FinPilotError(
                "POLICY_DENIED",
                "补卡不得顺带改变额度；额度调整须单独的客户申请。",
                details={"reason_codes": ["replacement_limit_change_requires_separate_application"]},
            )
        return old_card.credit_limit_minor

    limit = requested if approved_limit_minor is None else int(approved_limit_minor)
    if limit < 0:
        raise FinPilotError("PARAM_INVALID", "批准额度不能为负。", details={"reason_codes": ["negative_limit"]})
    if limit > requested:
        raise FinPilotError(
            "POLICY_DENIED",
            "运营审批只履行原客户请求；提高申请金额须客户重新授权。",
            details={"reason_codes": ["exceeds_client_request"], "requested_limit_minor": requested},
        )
    return limit


def _apply_approval(session: Session, application: CardApplication, limit: int, reason: str) -> Card | None:
    """批准的业务效果：新建有效卡或写入批准额度。"""
    now = utcnow()
    if application.kind == "new_card":
        card = Card(
            owner_user_id=application.owner_user_id,
            account_id=application.account_id,
            masked_pan=masked_pan_for_test_card(),
            brand="VIRTUAL",
            lifecycle_status="active",
            credit_limit_minor=limit,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(card)
        session.flush()
        application.card_id = card.card_id
        session.flush()
        return card

    old_card = repo.get_card(session, application.card_id)
    if old_card is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问申请关联的卡片。")

    if application.kind == "limit_change":
        if old_card.lifecycle_status != "active":
            raise FinPilotError(
                "POLICY_DENIED",
                "非 active 卡不能调整额度。",
                details={"reason_codes": ["card_not_active"], "lifecycle_status": old_card.lifecycle_status},
            )
        old_card.credit_limit_minor = limit
        old_card.version += 1
        old_card.updated_at = now
        session.flush()
        return None

    if application.kind == "replacement":
        if old_card.replaced_by_card_id:
            raise FinPilotError(
                "POLICY_DENIED",
                "该卡已完成补卡。",
                details={"reason_codes": ["card_already_replaced"]},
            )
        new_card = Card(
            owner_user_id=old_card.owner_user_id,
            account_id=old_card.account_id,
            masked_pan=masked_pan_for_test_card(),
            brand=old_card.brand if old_card.brand in CARD_BRANDS else "VIRTUAL",
            lifecycle_status="active",
            credit_limit_minor=limit,
            replaces_card_id=old_card.card_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(new_card)
        session.flush()
        old_card.replaced_by_card_id = new_card.card_id
        # 旧卡按条款关闭：新旧卡不得同时为无限制有效
        assert_card_transition(old_card.lifecycle_status, "closed")
        old_card.lifecycle_status = "closed"
        old_card.version += 1
        old_card.updated_at = now
        session.flush()
        if old_card.lifecycle_status == "active" and new_card.lifecycle_status == "active":  # pragma: no cover - 防御
            raise FinPilotError(
                "SYSTEM_ERROR",
                "补卡后不得同时存在两张有效卡。",
                details={"reason_codes": ["dual_active_card"]},
            )
        return new_card

    raise FinPilotError(
        "PARAM_INVALID",
        "不支持的申请类型。",
        details={"reason_codes": ["unsupported_application_kind"], "kind": application.kind},
    )


def withdraw_application(session: Session, ctx: AuthContext, application_id: str) -> dict:
    """撤回申请：仅本人、且仅限 submitted / under_review。"""
    ctx.require_customer()
    application = repo.get_application(session, application_id)
    if application is None or application.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该申请。")
    if application.application_status not in ("submitted", "under_review"):
        raise FinPilotError(
            "POLICY_DENIED",
            "只有 submitted / under_review 的申请可以撤回。",
            details={"reason_codes": ["withdraw_not_allowed"], "application_status": application.application_status},
        )
    assert_application_transition(application.application_status, "withdrawn")
    application.application_status = "withdrawn"
    application.decision_reason = "withdrawn_by_owner"
    application.decided_by = ctx.user_id
    application.decided_at = utcnow()
    application.version += 1
    session.flush()
    return _result(
        None,
        "card_application",
        application_view(session, application),
        warnings=["撤回后不生成卡；需要时请重新提交申请。"],
        note="撤回只撤回申请，不涉及任何已经生效的额度。",
    )


# --- 只读查询（网关与 A 客户端使用，命名固定） ------------------------------


def list_cards(session: Session, ctx: AuthContext) -> list[dict]:
    """当前用户（运营角色为全部）的卡片视图：合同对象 + 阻止原因（界面用）。"""
    cards = repo.cards_for_operator(session) if ctx.is_operator else repo.cards_for_owner(session, ctx.user_id)
    items: list[dict] = []
    for card in cards:
        view = card_view(session, card)
        reasons = card_blocked_reasons(session, card)
        items.append({**view, "blocked_reasons": reasons, "blocked": bool(reasons)})
    return items


def card_status(session: Session, ctx: AuthContext, card_id: str) -> dict:
    """单卡状态：生命周期、额度、叠加控制、阻止原因与关联申请。"""
    card = repo.require_owned_card(session, ctx, card_id)
    view = card_view(session, card)
    reasons = card_blocked_reasons(session, card)
    applications = repo.applications_for_card(session, card.card_id)
    return {
        **view,
        "blocked_reasons": reasons,
        "blocked": bool(reasons),
        "applications": [application_view(session, a) for a in applications],
    }


def list_applications(session: Session, ctx: AuthContext, status: str | None = None) -> list[dict]:
    """申请列表，可按申请状态过滤；申请状态与卡生命周期相互独立。"""
    if status is not None and status not in APPLICATION_TRANSITIONS:
        raise FinPilotError(
            "PARAM_INVALID",
            "未知的申请状态过滤条件。",
            details={"reason_codes": ["unsupported_status"], "status": status},
        )
    if ctx.is_operator:
        applications = repo.applications_for_operator(session, status=status)
    else:
        applications = repo.applications_for_owner(session, ctx.user_id, status=status)
    return [application_view(session, a) for a in applications]


# --- 执行器与调度器注册点 -------------------------------------------------


TOOL_HANDLERS = {
    "card.apply": apply_card,
    "card.set_limit": request_limit_change,
    "card.set_control": set_card_control,
    "card.report_lost": report_lost,
    "card.replace": replace_card,
}

# 卡片当前没有定时作业；显式登记空表，便于 workers.scheduler 的注册点在缺失时区分"未实现"与"无作业"。
JOB_HANDLERS: dict = {}

__all__ = [
    "SOURCE",
    "DOMAIN",
    "DOMAIN_VERSION",
    "TERMS_VERSION",
    "TOOL_HANDLERS",
    "JOB_HANDLERS",
    "SIMULATED_DECISION_LABEL",
    "SIMULATED_DECISION_VERSION",
    "SIMULATED_MAX_APPROVED_LIMIT_MINOR",
    "simulated_approval_rule",
    "simulated_decision",
    "apply_card",
    "request_limit_change",
    "set_card_control",
    "report_lost",
    "replace_card",
    "start_review",
    "auto_review",
    "approve_application",
    "withdraw_application",
    "list_cards",
    "card_status",
    "list_applications",
    "effective_controls",
    "can_release",
    "control_block_reasons",
    "card_view",
    "card_blocked_reasons",
    "application_view",
    "control_view",
    "masked_pan_for_test_card",
    "assert_application_transition",
    "assert_card_transition",
]