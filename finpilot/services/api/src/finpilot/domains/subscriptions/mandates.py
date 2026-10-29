"""银行代扣授权（mandate）领域逻辑（V5 第 13 节 S5-03/S5-04、T14/T31）。

本模块**只负责银行代扣这一端**：

- ``revoke_mandate`` 只把 ``Mandate.status`` 改为 ``revoked``，绝不改动 ``Subscription.status``；
  商户服务合同是否仍然有效由调用方按返回值如实展示，不能互相冒充。
- 解除代扣**不自动产生退款**：已扣费退款是独立流程，只有实际回执到账才记资金。
- 同一 ``operation_id`` 重放返回首次领域结果（幂等），不产生第二次效果。

工具映射：``mandate.revoke`` → :func:`revoke_mandate`（RED，需要强验证）；
只读映射：``mandate.status`` → :func:`list_mandates` / :func:`mandate_view`。
写处理器返回 ``DomainResult``（``domain/result_type/data/source/observed_at/warnings``）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...kernel.identity import AuthContext
from ...kernel.submission import transition
from ...models import Mandate, Operation, Subscription
from ...timeutil import format_iso, utcnow

DOMAIN = "subscriptions"
RESULT_TYPE_REVOKE = "mandate_revoke"
SOURCE = "local_ledger"

#: 代扣状态 → 面向用户的说明（不把「已解除代扣」说成「服务已终止」）
MANDATE_STATUS_LABELS: dict[str, str] = {
    "active": "授权有效：银行侧仍可能按合同扣款",
    "pending_revocation": "解除处理中：银行侧尚未确认",
    "revoked": "已解除：银行侧不再扣款",
}

#: 合同状态 → 面向用户的说明（与订阅模块的口径一致，避免两侧互相冒充）
CONTRACT_STATUS_LABELS: dict[str, str] = {
    "active": "合同有效",
    "pending_cancellation": "取消处理中（未获商户确认，合同仍有效）",
    "cancelled": "已取消",
    "ended": "已结束",
}

#: 领域处理器只接受已经过 submit_operation 的操作
WRITE_OPERATION_STATUSES = ("authorized", "dispatching", "pending", "succeeded")

#: 解除代扣时的状态优先级：优先解除仍然有效的授权
_MANDATE_PREFERENCE = {"active": 0, "pending_revocation": 1, "revoked": 2}


def _label(mapping: dict[str, str], status: str | None) -> str:
    if not status:
        return ""
    return mapping.get(status, status)


def mandate_contract_view(mandate: Mandate) -> dict:
    """``contracts/schemas/mandate.json`` 形状的代扣授权对象。"""
    return {
        "mandate_id": mandate.mandate_id,
        "owner_user_id": mandate.owner_user_id,
        "subscription_id": mandate.subscription_id,
        "merchant_id": mandate.merchant_id,
        "account_id": mandate.account_id,
        "status": mandate.status,
        "revoked_at": format_iso(mandate.revoked_at) if mandate.revoked_at else None,
        "version": mandate.version,
        "created_at": format_iso(mandate.created_at),
    }


def mandate_view(session: Session, mandate: Mandate, *, subscription: Subscription | None = None) -> dict:
    """代扣授权展示视图：只描述银行侧授权，不冒充合同状态。"""
    sub = subscription
    if sub is None and mandate.subscription_id:
        sub = session.get(Subscription, mandate.subscription_id)
    contract_status = sub.status if sub is not None else None
    return {
        **mandate_contract_view(mandate),
        "merchant_name": sub.merchant_name if sub is not None else "",
        "scope": "bank_debit",
        "scope_label": "银行代扣（仅银行侧扣款授权）",
        "status_label": _label(MANDATE_STATUS_LABELS, mandate.status),
        "active": mandate.status == "active",
        "contract_status": contract_status,
        "contract_status_label": _label(CONTRACT_STATUS_LABELS, contract_status),
        "contract_still_active": bool(contract_status == "active"),
        "refund_created": False,
        "source": {
            "table": "mandates",
            "data_source": "银行侧代扣授权记录",
            "observed_at": format_iso(utcnow()),
        },
        "note": "解除代扣只停止银行侧扣款授权；商户服务合同状态不受影响，也不会自动退款。",
    }


def resolve_mandate(
    session: Session,
    owner_user_id: str,
    *,
    mandate_id: str | None = None,
    subscription_id: str | None = None,
    for_update: bool = False,
) -> Mandate | None:
    """按 mandate_id 或 subscription_id 解析当前用户的代扣授权（归属过滤在 SQL 层完成）。"""
    if mandate_id:
        stmt = select(Mandate).where(Mandate.mandate_id == mandate_id, Mandate.owner_user_id == owner_user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return session.execute(stmt).scalar_one_or_none()
    if not subscription_id:
        return None
    stmt = select(Mandate).where(
        Mandate.subscription_id == subscription_id, Mandate.owner_user_id == owner_user_id
    )
    rows = session.execute(stmt).scalars().all()
    if not rows:
        return None
    rows.sort(key=lambda m: (_MANDATE_PREFERENCE.get(m.status, 9), m.created_at))
    chosen = rows[0]
    if for_update:
        chosen = session.execute(
            select(Mandate).where(Mandate.mandate_id == chosen.mandate_id).with_for_update()
        ).scalar_one()
    return chosen


def list_mandates(session: Session, ctx: AuthContext, *, subscription_id: str | None = None) -> list[dict]:
    """当前用户的代扣授权清单（工具 ``mandate.status`` 的只读实现）。"""
    stmt = select(Mandate).where(Mandate.owner_user_id == ctx.user_id)
    if subscription_id:
        stmt = stmt.where(Mandate.subscription_id == subscription_id)
    rows = session.execute(stmt.order_by(Mandate.created_at)).scalars().all()
    return [mandate_view(session, m) for m in rows]


def mandate_status_view(session: Session, ctx: AuthContext, *, subscription_id: str | None = None) -> dict:
    """``mandate.status`` 的只读信封：查询类无资金效果，不需要确认。"""
    mandates = list_mandates(session, ctx, subscription_id=subscription_id)
    return {
        "mandates": mandates,
        "count": len(mandates),
        "active_count": sum(1 for m in mandates if m["status"] == "active"),
        "requires_confirmation": False,
        "required_level": "GREEN",
        "observed_at": format_iso(utcnow()),
    }


# ---------------------------------------------------------------------------
# 写工具：解除银行代扣（mandate.revoke）
# ---------------------------------------------------------------------------


def _assert_write_operation(ctx: AuthContext, operation: Operation, *, tool: str) -> None:
    """写处理器前置检查：归属、工具、必须已经过提交事务（否则不得产生效果）。"""
    ctx.require_customer()
    if operation is None or operation.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该操作。")
    if operation.tool != tool:
        raise FinPilotError(
            "PARAM_INVALID",
            f"操作与工具不一致：期望 {tool}。",
            details={"reason_codes": ["tool_mismatch"], "tool": operation.tool},
        )
    if operation.status not in WRITE_OPERATION_STATUSES:
        raise FinPilotError(
            "POLICY_DENIED",
            "该操作尚未通过提交事务，不能产生业务效果。",
            details={"reason_codes": ["operation_not_submitted"], "status": operation.status},
        )


def _stored_result(operation: Operation) -> dict | None:
    """按 operation_id 幂等：返回首次领域结果（兼容执行器直接存入的 DomainResult）。"""
    stored = operation.domain_result or {}
    if stored.get("domain") != DOMAIN or stored.get("result_type") != RESULT_TYPE_REVOKE:
        return None
    return dict(stored)


def _store_result(session: Session, operation: Operation, result: dict) -> None:
    operation.domain_result = result
    session.flush()


def revoke_mandate(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """工具 ``mandate.revoke``：只解除银行代扣授权，不动商户合同、不产生退款。

    失败语义（刻意保持确定性，便于复核与回归）：

    - 授权不存在或不属于当前用户 → ``OBJECT_NOT_ACCESSIBLE``（不泄露他人对象是否存在）；
    - 银行侧尚未确认（``pending_revocation``）→ ``CANCEL_NOT_EFFECTIVE``，
      此时**不伪造成已解除**，授权仍可能扣款；
    - 已解除 → 幂等成功（``already_revoked=True``，不产生第二次效果）。
    """
    _assert_write_operation(ctx, operation, tool="mandate.revoke")

    stored = _stored_result(operation)
    if stored is not None:
        stored["idempotent_replay"] = True
        return stored

    args = operation.typed_args or {}
    mandate_id = args.get("mandate_id")
    subscription_id = args.get("subscription_id")
    if not mandate_id and not subscription_id:
        raise FinPilotError(
            "PARAM_INVALID",
            "解除代扣需要提供 mandate_id 或 subscription_id。",
            details={"reason_codes": ["missing_mandate_reference"]},
        )

    mandate = resolve_mandate(
        session,
        ctx.user_id,
        mandate_id=mandate_id,
        subscription_id=subscription_id,
        for_update=True,
    )
    if mandate is None:
        raise FinPilotError(
            "OBJECT_NOT_ACCESSIBLE",
            "无法访问该代扣授权。",
            details={"reason_codes": ["mandate_not_found"]},
        )

    now = utcnow()
    already_revoked = mandate.status == "revoked"
    if mandate.status == "pending_revocation":
        raise FinPilotError(
            "CANCEL_NOT_EFFECTIVE",
            "银行侧尚未确认解除代扣，原授权仍可能扣款。",
            details={
                "reason_codes": ["bank_side_not_confirmed"],
                "mandate_id": mandate.mandate_id,
                "mandate_status": mandate.status,
            },
        )
    if mandate.status not in ("active", "revoked"):
        raise FinPilotError(
            "POLICY_DENIED",
            "该代扣授权状态不支持解除。",
            details={"reason_codes": ["mandate_status_not_revocable"], "mandate_status": mandate.status},
        )

    changed = False
    if not already_revoked:
        mandate.status = "revoked"
        mandate.revoked_at = now
        mandate.version += 1
        changed = True
        session.flush()

    subscription = session.get(Subscription, mandate.subscription_id) if mandate.subscription_id else None
    contract_status = subscription.status if subscription is not None else None
    contract_active = contract_status == "active"

    unresolved: list[str] = []
    next_actions: list[str] = []
    if contract_active:
        unresolved.append(
            "商户服务合同仍然有效：解除代扣只停止银行侧扣款，商户可能继续按合同计费并产生欠费或被停服。"
        )
        next_actions.append("如需终止服务，请另行确认执行 subscription.cancel（商户合同取消）。")
    if contract_status == "pending_cancellation":
        unresolved.append("商户取消尚未确认，合同状态仍为取消处理中，可能继续产生扣费义务。")
    unresolved.append("解除代扣不会自动退款：已扣费退款是独立流程，只有实际回执到账才记资金。")

    result = {
        "domain": DOMAIN,
        "result_type": RESULT_TYPE_REVOKE,
        "data": mandate_contract_view(mandate),
        "source": SOURCE,
        "observed_at": format_iso(now),
        "warnings": [],
        "status": "succeeded",
        "tool": "mandate.revoke",
        "leg": "bank_mandate",
        "operation_id": operation.operation_id,
        "task_id": operation.task_id,
        "already_revoked": already_revoked,
        "changed": changed,
        "mandate": mandate_view(session, mandate, subscription=subscription),
        "contract": {
            "subscription_id": mandate.subscription_id,
            "merchant_id": mandate.merchant_id,
            "status": contract_status,
            "status_label": _label(CONTRACT_STATUS_LABELS, contract_status),
            "still_active": contract_active,
        },
        "contract_still_active": contract_active,
        "unresolved_obligations": unresolved,
        "next_actions": next_actions,
        "refund_created": False,
        "refund_policy": "解除代扣不产生退款；取消与退款是彼此独立的流程。",
        "effect": {
            "mandate_status": mandate.status,
            "revoked_at": format_iso(mandate.revoked_at) if mandate.revoked_at else None,
        },
    }

    _store_result(session, operation, result)
    transition(session, operation, "succeeded", reason="mandate_revoked", detail={"mandate_id": mandate.mandate_id})
    session.flush()
    return result


def revoke_mandate_leg(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """协调器专用：把解除代扣的异常转成结构化的一端失败结果，不静默吞掉原因。"""
    try:
        return {"ok": True, "result": revoke_mandate(session, ctx, operation)}
    except FinPilotError as exc:
        return {
            "ok": False,
            "error": exc.to_error_object(),
            "operation_id": getattr(operation, "operation_id", None),
            "requested_at": format_iso(utcnow()),
        }