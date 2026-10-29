"""分级授权策略（V5 6.1、AUTH-01、PLAN-04、S4-03）。

服务端是唯一策略权威：客户端与模型都不能降低等级。
等级边界：当日累计（含待执行占用）≤100000 分 YELLOW，>100000 分 RED；
手续费不计入转账本金累计；订阅查询保留官方 YELLOW 例外。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..models import Account, Card, CardControl, Payee
from ..timeutil import utcnow
from ..kernel.identity import AuthContext

POLICY_VERSION = "policy-2026.10"

GREEN = "GREEN"
YELLOW = "YELLOW"
RED = "RED"
OPERATOR = "OPERATOR"

LEVEL_RANK = {GREEN: 0, YELLOW: 1, RED: 2, OPERATOR: 3}

# 工具 → (默认等级, 是否对模型暴露, 说明)
TOOL_POLICY: dict[str, tuple[str, bool, str]] = {
    # 只读 / 无资金效果
    "account.balance": (GREEN, True, "纯查询，无资金效果"),
    "payee.resolve": (GREEN, True, "只读检索当前用户联系人"),
    "bill.report": (GREEN, True, "只读账单分析"),
    "transfer.prepare": (GREEN, True, "只登记草稿，不产生外部支付"),
    "transfer.status": (GREEN, True, "只读操作查询"),
    "wealth.compare": (GREEN, True, "只读产品比较"),
    "wealth.status": (GREEN, True, "只读订单查询"),
    "card.status": (GREEN, True, "只读卡状态"),
    "mandate.status": (GREEN, True, "只读代扣状态"),
    "merchant.quote": (GREEN, True, "只读报价，无资金效果"),
    "merchant.status": (GREEN, True, "只读预订状态"),
    "quant.risk_report": (GREEN, True, "只读风险计算"),
    "aa.status": (GREEN, True, "只读 AA 状态"),
    "schedule.status": (GREEN, True, "只读定时任务状态"),
    # 订阅：官方黄色例外，查询也要明确确认
    "subscription.list": (YELLOW, True, "官方 YELLOW 例外：订阅查询需明确确认"),
    "subscription.cancel": (YELLOW, True, "取消商户合同，需明确确认"),
    # 资金与账户写入
    "transfer.submit": (YELLOW, False, "金额决定等级：≤100000 分 YELLOW，>100000 分 RED"),
    "schedule.create": (YELLOW, False, "继承具体动作等级，至少 YELLOW"),
    "schedule.cancel": (YELLOW, False, "取消定时任务"),
    "aa.create": (YELLOW, False, "仅创建收款请求，不含参与人付款授权"),
    "reminder.create": (YELLOW, False, "提醒设置"),
    "reminder.cancel": (YELLOW, False, "提醒取消"),
    "risk.assess": (YELLOW, False, "提交风险问卷"),
    # 卡片
    "card.apply": (YELLOW, False, "虚拟卡申请"),
    "card.set_limit": (YELLOW, False, "调低额度为 YELLOW；申请提额为 RED"),
    "card.set_control": (YELLOW, False, "增加用户限制为 YELLOW；解除重要限制为 RED"),
    "card.report_lost": (RED, False, "挂失／解挂"),
    "card.replace": (RED, False, "补卡确认"),
    # 理财与服务
    "wealth.purchase": (RED, False, "申购"),
    "wealth.redeem": (RED, False, "赎回"),
    "merchant.book": (RED, False, "付费商户预订"),
    "merchant.cancel": (YELLOW, False, "取消预订，按条款可能产生取消费"),
    "mandate.revoke": (RED, False, "解除银行代扣"),
    # 运营
    "operator.card_decision": (OPERATOR, False, "模拟银行审批，独立运营角色"),
    "operator.risk_unfreeze": (OPERATOR, False, "风控解冻，需理由与独立角色"),
    "operator.case_action": (OPERATOR, False, "接管工单动作（查询原操作、复核、升级）"),
    "operator.release_risk_freeze": (OPERATOR, False, "风控解冻（领域别名，与 operator.risk_unfreeze 同义）"),
}

# 对模型暴露的工具（人类确认与强验证端点永不在此集合内）
AGENT_EXPOSED_TOOLS = frozenset(name for name, (_lvl, exposed, _n) in TOOL_POLICY.items() if exposed)
WRITE_TOOLS = frozenset(
    name for name, (_lvl, exposed, _n) in TOOL_POLICY.items() if not exposed and not name.startswith("operator.")
)
PROVIDER_TOOLS = frozenset({"transfer.submit", "merchant.book", "merchant.cancel"})
TRANSFER_TOOLS = frozenset({"transfer.submit", "schedule.create"})


@dataclass
class PolicyDecision:
    allow: bool
    required_level: str
    reason_codes: list[str] = field(default_factory=list)
    policy_version: str = POLICY_VERSION
    notes: list[str] = field(default_factory=list)
    snapshot_version: str | None = None

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "required_level": self.required_level,
            "reason_codes": self.reason_codes,
            "policy_version": self.policy_version,
            "notes": self.notes,
        }


def _level_for_card_limit(typed_args: dict) -> str:
    direction = str(typed_args.get("direction", "")).lower()
    if direction in ("increase", "up", "raise"):
        return RED
    return YELLOW


def _level_for_card_control(typed_args: dict) -> str:
    action = str(typed_args.get("action", "add")).lower()
    control_type = str(typed_args.get("control_type", "")).lower()
    if action in ("release", "remove", "clear"):
        # 解除重要限制为 RED；风控锁只能由运营解除
        if control_type in ("risk_freeze", "bank_freeze", "significant"):
            return OPERATOR
        return RED
    return YELLOW


def _resolve_level(session: Session, ctx: AuthContext, tool: str, typed_args: dict) -> tuple[str, list[str], list[str]]:
    base, _exposed, note = TOOL_POLICY[tool]
    reasons: list[str] = []
    notes: list[str] = [note] if note else []

    if tool == "transfer.submit":
        from ..kernel.limits import grade_transfer

        principal = int(typed_args.get("amount_minor") or 0)
        if principal <= 0:
            raise FinPilotError("PARAM_INVALID", "转账金额必须大于 0。")
        level = grade_transfer(session, ctx, principal_minor=principal)
        reasons.append("daily_limit_applied")
        limit = get_settings().transfer_daily_yellow_limit_minor
        notes.append(f"当日累计阈值 {limit} 分（含待执行占用）")
        return level, reasons, notes

    if tool == "schedule.create":
        from ..kernel.limits import grade_transfer

        principal = int(typed_args.get("amount_minor") or 0)
        if principal > 0:
            # 定时任务不提前消耗未来日额度，但按本金给等级下限
            level = RED if principal > get_settings().transfer_daily_yellow_limit_minor else YELLOW
            reasons.append("scheduled_inherits_action_level")
            return level, reasons, notes
        return YELLOW, ["scheduled_minimum_yellow"], notes

    if tool == "card.set_limit":
        return _level_for_card_limit(typed_args), ["card_limit_direction"], notes
    if tool == "card.set_control":
        level = _level_for_card_control(typed_args)
        if level == OPERATOR:
            reasons.append("operator_only")
            notes.append("风控冻结只能由独立运营角色解除")
        return level, reasons, notes
    if tool == "wealth.redeem":
        return RED, ["redemption_requires_strong_verification"], notes
    if tool == "merchant.cancel":
        return YELLOW, ["cancellation_may_incur_fee"], notes
    return base, reasons, notes


def evaluate_policy(
    session: Session,
    ctx: AuthContext,
    *,
    tool: str,
    typed_args: dict,
    snapshot: dict | None = None,
    task_id: str | None = None,
) -> PolicyDecision:
    """服务端策略判定。客户端与模型不能降低等级，也不能跳过拒绝结论。"""
    if tool not in TOOL_POLICY:
        raise FinPilotError("UNKNOWN_TOOL", "该能力未在工具目录中登记，不能执行。")

    level, reasons, notes = _resolve_level(session, ctx, tool, typed_args or {})

    if level == OPERATOR and not ctx.is_operator:
        return PolicyDecision(
            allow=False,
            required_level=OPERATOR,
            reason_codes=reasons + ["operator_role_required"],
            notes=notes,
        )

    if tool in AGENT_EXPOSED_TOOLS and ctx.is_customer:
        # 只读工具对模型可见，但写工具不经此路径
        pass

    if level == RED:
        reasons.append("strong_verification_required")
    elif level == YELLOW:
        reasons.append("explicit_confirmation_required")

    # 卡片：被拒绝的提额申请不能通过重新发问变为批准
    if tool == "card.set_limit" and typed_args.get("direction", "").lower() in ("increase", "up", "raise"):
        card_id = typed_args.get("card_id")
        if card_id:
            from ..models import CardApplication

            rejected = session.query(CardApplication).filter(
                CardApplication.card_id == card_id,
                CardApplication.kind == "limit_change",
                CardApplication.application_status == "rejected",
            ).count()
            if rejected:
                return PolicyDecision(
                    allow=False,
                    required_level=RED,
                    reason_codes=["previously_rejected_application"],
                    notes=["该提额申请已被拒绝，不能通过重新发问变为批准"],
                )

    if not _ownership_ok(session, ctx, tool, typed_args):
        return PolicyDecision(allow=False, required_level=level, reason_codes=["object_not_owned"], notes=notes)

    return PolicyDecision(allow=True, required_level=level, reason_codes=reasons, notes=notes,
                          snapshot_version=(snapshot or {}).get("policy_version"))


def _ownership_ok(session: Session, ctx: AuthContext, tool: str, typed_args: dict) -> bool:
    """在策略层再做一次归属检查；持久层与领域入口仍会重复校验。"""
    account_id = typed_args.get("source_account_id") or typed_args.get("account_id")
    if account_id:
        account = session.get(Account, account_id)
        if account is None:
            return False
        if ctx.is_customer and account.owner_user_id != ctx.user_id:
            return False
    payee_id = typed_args.get("payee_id")
    if payee_id:
        payee = session.get(Payee, payee_id)
        if payee is None or (ctx.is_customer and payee.owner_user_id != ctx.user_id):
            return False
    card_id = typed_args.get("card_id")
    if card_id:
        card = session.get(Card, card_id)
        if card is None or (ctx.is_customer and card.owner_user_id != ctx.user_id):
            return False
    return True


def requires_strong_factor(level: str) -> bool:
    return LEVEL_RANK.get(level, 0) >= LEVEL_RANK[RED]


def tool_is_agent_exposed(tool: str) -> bool:
    return tool in AGENT_EXPOSED_TOOLS


def tool_required_level(tool: str) -> str:
    return TOOL_POLICY[tool][0]


def effective_level(*levels: str) -> str:
    """取最高等级：任何一侧要求 RED 即为 RED。"""
    return max(levels, key=lambda lv: LEVEL_RANK.get(lv, 0))


def policy_catalog() -> list[dict]:
    return [
        {
            "tool": name,
            "default_level": level,
            "agent_exposed": exposed,
            "note": note,
        }
        for name, (level, exposed, note) in sorted(TOOL_POLICY.items())
    ]


def card_control_block_reason(session: Session, card: Card, *, channel: str = "online", amount_minor: int = 0) -> list[str]:
    """返回阻止交易的卡控原因码（叠加，不互相清除）。"""
    reasons: list[str] = []
    if card.lifecycle_status != "active":
        reasons.append(f"card_{card.lifecycle_status}")
    controls = session.query(CardControl).filter(CardControl.card_id == card.card_id, CardControl.active.is_(True)).all()
    for control in controls:
        if control.control_type in ("user_lock", "risk_freeze", "bank_freeze"):
            reasons.append(control.control_type)
        elif control.control_type == "channel_lock" and control.channel == channel:
            reasons.append("channel_lock")
        elif control.control_type == "limit" and control.limit_minor is not None and amount_minor > control.limit_minor:
            reasons.append("over_limit")
    return reasons