"""操作登记与提交事务（V5 8.2、MONEY-03、API-02）。

提交事务内按固定顺序完成：
    归属 → 参数哈希 → 服务端策略再判定 → 日额度 → 授权（精确/范围子集）→ 资金 → 版本
    → 登记 GrantUse、额度占用、Outbox（本地持久化提交意图）
外部效果只能在该事务提交之后由 Outbox 驱动发生。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..ids import idempotency_request_hash, new_id, parameters_hash as compute_parameters_hash
from ..models import (
    Account,
    Grant,
    IdempotencyKey,
    Operation,
    OperationEvent,
    Outbox,
    Payee,
)
from ..money import format_minor
from ..security.policy import (
    AGENT_EXPOSED_TOOLS,
    POLICY_VERSION,
    PROVIDER_TOOLS,
    TRANSFER_TOOLS,
    evaluate_policy,
    effective_level,
)
from ..timeutil import utcnow
from .identity import AuthContext
from .ledger import account_spendable_minor, reserved_minor
from .limits import daily_usage, hold_daily_quota
from .state_machine import assert_transition

FORBIDDEN_ARG_FIELDS = frozenset(
    {"user_id", "owner_user_id", "posted_balance_minor", "grant_id", "status", "effect_confirmed", "version"}
)


@dataclass
class OperationDraft:
    tool: str
    typed_args: dict
    operation_id: str = ""
    task_id: str | None = None
    plan_id: str | None = None
    plan_revision: int = 1
    idempotency_key: str = ""
    source_account_id: str | None = None
    amount_minor: int = 0
    fee_minor: int = 0
    provider_id: str | None = None
    required_level: str = "GREEN"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.operation_id:
            self.operation_id = new_id("op")
        if not self.idempotency_key:
            self.idempotency_key = self.operation_id


def _validate_args(tool: str, typed_args: dict) -> None:
    offending = FORBIDDEN_ARG_FIELDS.intersection(typed_args.keys())
    if offending:
        raise FinPilotError(
            "EXTRA_DANGEROUS_FIELD",
            "请求包含不允许的额外字段。",
            details={"reason_codes": ["forbidden_field"], "fields": sorted(offending)},
        )
    if tool not in AGENT_EXPOSED_TOOLS and tool not in PROVIDER_TOOLS and not tool.startswith("card.") and not tool.startswith(
        ("aa.", "reminder.", "risk.", "schedule.", "subscription.", "mandate.", "wealth.", "merchant.")
    ):
        raise FinPilotError("UNKNOWN_TOOL", "该能力未在工具目录中登记，不能执行。")


def prepare_operation(session: Session, ctx: AuthContext, draft: OperationDraft) -> Operation:
    """登记稳定操作草稿：只准备，不消费授权、不产生外部支付。"""
    ctx.require_customer()
    _validate_args(draft.tool, draft.typed_args)

    request_hash = idempotency_request_hash(draft.tool, draft.typed_args)
    param_hash = compute_parameters_hash(draft.typed_args)

    existing = session.get(Operation, draft.operation_id)
    if existing is not None:
        if existing.parameters_hash != param_hash:
            raise FinPilotError("IDEMPOTENCY_CONFLICT", "同一操作编号被用于不同参数，已拒绝。")
        return existing

    key_row = session.get(IdempotencyKey, draft.idempotency_key)
    if key_row is not None:
        if key_row.request_hash != request_hash:
            raise FinPilotError("IDEMPOTENCY_CONFLICT", "同一幂等键被用于不同参数，已拒绝。")
        bound = session.get(Operation, key_row.operation_id)
        if bound is not None:
            return bound

    decision = evaluate_policy(session, ctx, tool=draft.tool, typed_args=draft.typed_args, task_id=draft.task_id)
    if not decision.allow:
        raise FinPilotError("POLICY_DENIED", "服务端策略不允许执行该操作。", details={"reason_codes": decision.reason_codes})

    level = effective_level(decision.required_level, draft.required_level)
    now = utcnow()
    operation = Operation(
        operation_id=draft.operation_id,
        owner_user_id=ctx.user_id,
        task_id=draft.task_id,
        plan_id=draft.plan_id,
        plan_revision=draft.plan_revision,
        tool=draft.tool,
        typed_args=draft.typed_args,
        parameters_hash=param_hash,
        idempotency_key=draft.idempotency_key,
        request_hash=request_hash,
        status="authorized" if level == "GREEN" else "awaiting_authorization",
        required_level=level,
        source_account_id=draft.source_account_id or draft.typed_args.get("source_account_id"),
        amount_minor=int(draft.amount_minor or draft.typed_args.get("amount_minor") or 0),
        fee_minor=int(draft.fee_minor or draft.typed_args.get("fee_minor") or 0),
        provider_id=draft.provider_id,
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    session.add(
        IdempotencyKey(
            key=draft.idempotency_key,
            owner_user_id=ctx.user_id,
            operation_id=operation.operation_id,
            request_hash=request_hash,
            created_at=now,
        )
    )
    session.flush()
    _record_event(session, operation, None, operation.status, "prepared", {"level": level})
    return operation


def _record_event(session: Session, operation: Operation, from_status: str | None, to_status: str, reason: str, detail: dict) -> None:
    session.add(
        OperationEvent(
            operation_id=operation.operation_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            detail=detail,
            created_at=utcnow(),
        )
    )


def transition(session: Session, operation: Operation, to_status: str, *, reason: str, detail: dict | None = None) -> Operation:
    if operation.status == to_status:
        return operation
    assert_transition("operation", operation.status, to_status)
    from_status = operation.status
    operation.status = to_status
    operation.version += 1
    operation.updated_at = utcnow()
    if to_status == "unknown" and operation.unknown_since is None:
        operation.unknown_since = utcnow()
    if to_status in ("succeeded", "failed", "cancelled"):
        operation.settled_at = utcnow()
        if to_status == "succeeded":
            operation.effect_confirmed = True
    _record_event(session, operation, from_status, to_status, reason, detail or {})
    session.flush()
    return operation


def _check_funds(session: Session, ctx: AuthContext, operation: Operation, *, require_spendable: bool = True) -> None:
    if operation.amount_minor <= 0 and operation.fee_minor <= 0:
        return
    account_id = operation.source_account_id
    if not account_id:
        raise FinPilotError("PARAM_INVALID", "缺少付款账户。")
    account = session.get(Account, account_id)
    if account is None or account.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
    if account.status != "active":
        raise FinPilotError("POLICY_DENIED", "该账户状态不允许付款。")

    total = operation.amount_minor + operation.fee_minor
    from .snapshots import protection_policy

    policy = protection_policy(session, ctx.user_id)
    floor = policy.protected_floor_minor if (policy and account_id in (policy.account_scope or [account_id])) else 0
    reserved = reserved_minor(session, account_id)

    # 本任务自己的预留可以覆盖本次支付；其他任务的预留不可挪用
    own_reserved = 0
    if operation.task_id:
        from ..models import Reservation

        own_reserved = sum(
            r.remaining_minor
            for r in session.execute(
                select(Reservation).where(
                    Reservation.task_id == operation.task_id,
                    Reservation.account_id == account_id,
                    Reservation.status.in_(("active", "partially_captured")),
                )
            ).scalars()
        )

    available_for_this = account.posted_balance_minor - (reserved - own_reserved) - account.frozen_other_minor
    if available_for_this < total:
        raise FinPilotError(
            "INSUFFICIENT_FUNDS",
            "可用余额不足。",
            details={
                "reason_codes": ["insufficient_available"],
                "required_minor": total,
                "available_minor": available_for_this,
            },
        )
    if require_spendable and own_reserved <= 0:
        spendable = account_spendable_minor(session, account_id, floor)
        if spendable < total:
            raise FinPilotError(
                "PROTECTED_FLOOR_VIOLATION",
                "该操作会穿透已确认的应急金底线。",
                details={
                    "reason_codes": ["protected_floor"],
                    "protected_floor_minor": floor,
                    "spendable_minor": spendable,
                    "required_minor": total,
                },
            )


def submit_operation(
    session: Session,
    ctx: AuthContext,
    *,
    operation_id: str,
    grant_id: str | None = None,
    expected_version: int | None = None,
) -> Operation:
    """原子检查策略、资金、授权与版本，登记消费与提交意图。"""
    ctx.require_customer()
    operation = session.execute(
        select(Operation).where(Operation.operation_id == operation_id).with_for_update()
    ).scalar_one_or_none()
    if operation is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该操作。")
    if operation.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该操作。")

    if operation.status == "succeeded":
        return operation  # 幂等：重复提交不产生新的金融效果
    if operation.status in ("failed", "cancelled"):
        raise FinPilotError("POLICY_DENIED", "该操作已处于终态，不能重新提交。")
    if operation.status in ("dispatching", "pending", "unknown"):
        return operation  # 已提交：沿用同一 operation_id 查询，不再重复登记

    if expected_version is not None and expected_version != operation.version:
        raise FinPilotError(
            "VERSION_CONFLICT",
            "对象已被其他操作更新，请刷新后重试。",
            details={"reason_codes": ["version_mismatch"], "current_version": operation.version},
        )

    # 服务端再次判定策略：初始 UI 级别不是最终凭证
    decision = evaluate_policy(
        session, ctx, tool=operation.tool, typed_args=operation.typed_args, task_id=operation.task_id
    )
    if not decision.allow:
        _record_event(session, operation, operation.status, operation.status, "policy_denied", {"reasons": decision.reason_codes})
        session.flush()
        raise FinPilotError("POLICY_DENIED", "服务端策略不允许执行该操作。", details={"reason_codes": decision.reason_codes})

    level = effective_level(decision.required_level, operation.required_level)

    _check_funds(session, ctx, operation)

    # 日额度：先在同一事务内锁定当日额度桶并登记本次占用，再据锁定后的读数判定等级。
    # 这样并发分拆无法各自按旧值判为 YELLOW（V5 6.1、8.2、T16）。
    if operation.tool in TRANSFER_TOOLS and operation.amount_minor > 0:
        from ..timeutil import business_day

        day = business_day()
        hold_daily_quota(
            session,
            ctx,
            operation_id=operation.operation_id,
            principal_minor=operation.amount_minor,
            business_day=day,
        )
        usage = daily_usage(session, ctx, day)
        if usage["used_minor"] > usage["yellow_limit_minor"]:
            level = "RED"

    if level != operation.required_level:
        operation.required_level = level
        operation.version += 1

    if level != "GREEN":
        if not grant_id:
            raise FinPilotError(
                "GRANT_REQUIRED",
                "该操作需要用户明确确认。",
                details={"reason_codes": ["grant_missing"], "required_level": level},
            )
        grant = session.execute(select(Grant).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
        if grant is None or grant.owner_user_id != ctx.user_id:
            raise FinPilotError("GRANT_INVALID", "授权不存在或已失效。")
        if level == "RED" and grant.verification_level != "TOTP":
            raise FinPilotError(
                "GRANT_REQUIRED",
                "该操作需要完成强验证。",
                details={"reason_codes": ["strong_verification_required"], "required_level": "RED"},
            )
        from .grants import consume_grant, grant_covers

        candidate_scope = {
            **operation.typed_args,
            "tool": operation.tool,
            "operation_id": operation.operation_id,
            "account_id": operation.source_account_id,
            "amount_minor": operation.amount_minor,
            "required_level": level,
        }
        covered, reason = grant_covers(
            grant,
            plan_revision=operation.plan_revision,
            candidate_scope=candidate_scope,
            amount_minor=operation.amount_minor,
            parameter_hash=operation.parameters_hash,
        )
        if not covered:
            raise FinPilotError(
                "GRANT_SCOPE_VIOLATION",
                "该操作超出已授权范围，需要重新确认。",
                details={"reason_codes": [reason]},
            )
        consume_grant(
            session,
            ctx,
            grant_id=grant.grant_id,
            operation_id=operation.operation_id,
            parameter_hash=operation.parameters_hash,
            amount_minor=operation.amount_minor,
        )
        operation.grant_id = grant.grant_id

    if operation.status == "awaiting_authorization":
        transition(session, operation, "authorized", reason="authorization_granted", detail={"level": level})
    transition(session, operation, "dispatching", reason="submitted", detail={"level": level})

    if operation.tool in PROVIDER_TOOLS:
        _enqueue_dispatch(session, operation)

    session.flush()
    return operation


def _default_provider_for(tool: str) -> str:
    """按工具选择提供方：商户类走商户沙箱，资金类走银行沙箱。"""
    from .provider_adapter import default_provider_id
    return default_provider_id(tool)


def _enqueue_dispatch(session: Session, operation: Operation) -> Outbox:
    dedupe = f"{operation.operation_id}:dispatch"
    existing = session.execute(select(Outbox).where(Outbox.dedupe_key == dedupe)).scalar_one_or_none()
    if existing is not None:
        return existing
    provider_id = operation.provider_id or _default_provider_for(operation.tool)
    operation.provider_id = provider_id
    now = utcnow()
    row = Outbox(
        aggregate_id=operation.operation_id,
        operation_id=operation.operation_id,
        task_id=operation.task_id,
        event_type="operation.dispatch",
        payload={
            "operation_id": operation.operation_id,
            "owner_user_id": operation.owner_user_id,
            "tool": operation.tool,
            "provider_id": provider_id,
            "typed_args": operation.typed_args,
        },
        dedupe_key=dedupe,
        delivery_state="pending",
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row


def get_operation(session: Session, ctx: AuthContext, operation_id: str) -> Operation:
    operation = session.get(Operation, operation_id)
    if operation is None or (ctx.is_customer and operation.owner_user_id != ctx.user_id):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该操作。")
    return operation


def operation_view(session: Session, ctx: AuthContext, operation_id: str) -> dict:
    operation = get_operation(session, ctx, operation_id)
    return operation_to_view(session, operation)


def operation_to_view(session: Session, operation: Operation) -> dict:
    from ..models import Receipt, Reservation

    receipt = session.execute(
        select(Receipt).where(Receipt.operation_id == operation.operation_id).order_by(Receipt.received_at.desc())
    ).scalars().first()

    reservations = session.execute(
        select(Reservation).where(Reservation.operation_id == operation.operation_id)
    ).scalars().all()

    pending_items: list[str] = []
    if operation.status == "unknown":
        pending_items.append("结果未知：正在用原操作编号查询，请勿重复提交")
    if operation.status in ("dispatching", "pending"):
        pending_items.append("等待提供方回执")
    if operation.tool.startswith("wealth.") and operation.status == "pending":
        pending_items.append("申购已受理，尚未确认正式份额")

    return {
        "operation_id": operation.operation_id,
        "task_id": operation.task_id,
        "plan_id": operation.plan_id,
        "plan_revision": operation.plan_revision,
        "tool": operation.tool,
        "status": operation.status,
        "required_level": operation.required_level,
        "amount_minor": operation.amount_minor,
        "amount": format_minor(operation.amount_minor) if operation.amount_minor else None,
        "fee_minor": operation.fee_minor,
        "fee": format_minor(operation.fee_minor) if operation.fee_minor else None,
        "currency": operation.currency,
        "source_account_id": operation.source_account_id,
        "provider_id": operation.provider_id,
        "provider_order_ref": operation.provider_order_ref,
        "effect_confirmed": operation.effect_confirmed,
        "error_code": operation.error_code,
        "version": operation.version,
        "created_at": operation.created_at.isoformat(),
        "updated_at": operation.updated_at.isoformat(),
        "submitted_at": operation.submitted_at.isoformat() if operation.submitted_at else None,
        "settled_at": operation.settled_at.isoformat() if operation.settled_at else None,
        "unknown_since": operation.unknown_since.isoformat() if operation.unknown_since else None,
        "receipt": (
            {
                "receipt_id": receipt.receipt_id,
                "provider_ref": receipt.provider_ref,
                "source": receipt.source,
                "received_at": receipt.received_at.isoformat(),
                "verified_status": receipt.verified_status,
                "payload_hash": receipt.payload_hash,
            }
            if receipt is not None
            else None
        ),
        "reservations": [
            {
                "reservation_id": r.reservation_id,
                "original_minor": r.original_minor,
                "remaining_minor": r.remaining_minor,
                "captured_minor": r.captured_minor,
                "status": r.status,
            }
            for r in reservations
        ],
        "pending_items": pending_items,
        "domain_result": operation.domain_result or {},
    }
