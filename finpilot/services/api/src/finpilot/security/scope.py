"""授权范围构造与展示摘要（V5 6.4、AUTH-04）。

显示摘要与签名参数必须来自同一结构：scope_hash 由 build_scope 的输出计算，
确认界面只渲染 describe_scope(scope) 的结果，禁止另写一套展示文案。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..errors import FinPilotError
from ..ids import canonical_hash, scope_hash as compute_scope_hash
from ..money import format_minor
from ..timeutil import format_iso, utcnow
from .policy import POLICY_VERSION, TOOL_POLICY

IRREVERSIBLE_TOOLS = frozenset(
    {"transfer.submit", "merchant.book", "wealth.purchase", "wealth.redeem", "mandate.revoke"}
)


def build_scope(
    tool: str,
    typed_args: dict,
    *,
    task_id: str | None = None,
    plan_id: str | None = None,
    plan_revision: int = 1,
    parameters_hash: str | None = None,
    operation_id: str | None = None,
    required_level: str = "YELLOW",
    terms_version: str | None = None,
) -> dict:
    """构造与后端校验同源的授权范围。"""
    if tool not in TOOL_POLICY:
        raise FinPilotError("UNKNOWN_TOOL", "该能力未在工具目录中登记，不能执行。")

    args = dict(typed_args or {})
    amount = int(args.get("amount_minor") or args.get("total_minor") or 0)
    fee = int(args.get("fee_minor") or 0)
    scope: dict = {
        "kind": "exact",
        "tool": tool,
        "task_id": task_id,
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "operation_id": operation_id,
        "parameters_hash": parameters_hash or canonical_hash({"tool": tool, "args": args}),
        "account_id": args.get("source_account_id") or args.get("account_id"),
        "amount_minor": amount,
        "fee_cap_minor": fee,
        "currency": "CNY",
        "payee_id": args.get("payee_id"),
        "payee_account_ref": args.get("payee_account_ref"),
        "product_id": args.get("product_id"),
        "merchant_id": args.get("merchant_id"),
        "card_id": args.get("card_id"),
        "quantity": args.get("quantity"),
        "execute_after": args.get("execute_after"),
        "deadline": args.get("deadline"),
        "terms_version": terms_version or args.get("terms_version"),
        "required_level": required_level,
        "irreversible": tool in IRREVERSIBLE_TOOLS,
        "policy_version": POLICY_VERSION,
        "created_at": format_iso(utcnow()),
    }
    scope["scope_hash"] = compute_scope_hash({k: v for k, v in scope.items() if k != "scope_hash"})
    return scope


def build_scoped_scope(
    *,
    task_id: str,
    plan_id: str | None,
    plan_revision: int,
    allowed_tools: list[str],
    allowed_account_ids: list[str],
    max_amount_per_op_minor: int,
    max_total_minor: int,
    max_uses: int,
    deadline: datetime,
    merchants: list[dict] | None = None,
    products: list[str] | None = None,
    allowed_compensation: list[str] | None = None,
    required_level: str = "RED",
) -> dict:
    """范围授权：无法结构化表达的"都可以"不能成为无限授权。"""
    if not allowed_tools or not allowed_account_ids:
        raise FinPilotError("PARAM_INVALID", "范围授权必须列出允许的动作与账户集合。")
    if max_amount_per_op_minor <= 0 or max_total_minor <= 0 or max_uses <= 0:
        raise FinPilotError("PARAM_INVALID", "范围授权必须给出正的每笔上限、总额与次数。")
    scope = {
        "kind": "scoped",
        "task_id": task_id,
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "allowed_tools": list(allowed_tools),
        "allowed_account_ids": list(allowed_account_ids),
        "max_amount_per_op_minor": int(max_amount_per_op_minor),
        "max_total_minor": int(max_total_minor),
        "max_uses": int(max_uses),
        "deadline": deadline.isoformat(),
        "merchants": merchants or [],
        "products": products or [],
        "allowed_compensation": allowed_compensation or [],
        "required_level": required_level,
        "policy_version": POLICY_VERSION,
        "created_at": format_iso(utcnow()),
    }
    scope["scope_hash"] = compute_scope_hash({k: v for k, v in scope.items() if k != "scope_hash"})
    return scope


def describe_scope(scope: dict) -> dict:
    """面向用户的展示摘要：金额、费用、对象、时间窗、不可逆影响。"""
    amount = int(scope.get("amount_minor") or 0)
    fee_cap = int(scope.get("fee_cap_minor") or 0)
    deadline = scope.get("deadline")
    execute_after = scope.get("execute_after")
    kind = scope.get("kind", "exact")

    impacts: list[str] = []
    if scope.get("irreversible"):
        impacts.append("该操作提交后不可撤销，或仅能按条款申请退款")
    if fee_cap:
        impacts.append(f"预计费用 {format_minor(fee_cap)} 元，费用计入资金预算但不计入转账本金累计")
    if execute_after:
        impacts.append("这是一笔定时执行的操作，执行前需要独立的未来授权")
    if kind == "scoped":
        impacts.append(
            f"范围授权：最多 {scope.get('max_uses')} 次、累计不超过 {format_minor(int(scope.get('max_total_minor') or 0))} 元"
        )

    return {
        "kind": kind,
        "tool": scope.get("tool"),
        "tool_label": _tool_label(scope.get("tool")),
        "required_level": scope.get("required_level"),
        "amount": format_minor(amount) if amount else None,
        "amount_minor": amount,
        "fee_cap": format_minor(fee_cap) if fee_cap else None,
        "currency": scope.get("currency", "CNY"),
        "account_id": scope.get("account_id"),
        "payee_id": scope.get("payee_id"),
        "payee_account_ref": scope.get("payee_account_ref"),
        "product_id": scope.get("product_id"),
        "merchant_id": scope.get("merchant_id"),
        "card_id": scope.get("card_id"),
        "terms_version": scope.get("terms_version"),
        "execute_after": execute_after,
        "deadline": deadline,
        "time_window": _time_window(execute_after, deadline),
        "irreversible": bool(scope.get("irreversible")),
        "impacts": impacts,
        "scope_hash": scope.get("scope_hash") or compute_scope_hash(
            {k: v for k, v in scope.items() if k != "scope_hash"}
        ),
        "policy_version": scope.get("policy_version", POLICY_VERSION),
    }


def _time_window(execute_after: str | None, deadline: str | None) -> str:
    if execute_after and deadline:
        return f"{execute_after} 至 {deadline}"
    if execute_after:
        return f"{execute_after} 之后执行"
    return "立即执行"


def _tool_label(tool: str | None) -> str:
    labels = {
        "transfer.submit": "转账",
        "schedule.create": "定时转账",
        "merchant.book": "商户预订",
        "wealth.purchase": "理财申购",
        "wealth.redeem": "理财赎回",
        "card.apply": "卡片申请",
        "card.set_limit": "额度调整",
        "card.set_control": "交易限制",
        "card.report_lost": "挂失／解挂",
        "card.replace": "补卡",
        "aa.create": "AA 收款请求",
        "subscription.cancel": "取消订阅",
        "mandate.revoke": "解除代扣",
        "reminder.create": "续费提醒",
        "risk.assess": "风险测评",
        "subscription.list": "订阅查询",
        "merchant.cancel": "取消预订",
    }
    return labels.get(tool or "", tool or "操作")


def scope_expiry(scope: dict, *, default_seconds: int = 300) -> datetime:
    deadline = scope.get("deadline")
    if deadline:
        try:
            parsed = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            return parsed
        except ValueError:
            pass
    return utcnow() + timedelta(seconds=default_seconds)