"""订阅与代扣领域服务（V5 第 13 节 S5-01—S5-04、T07/T14/T15/T16/T22/T27/T31）。

三件事严格分开，任何一步都不能互相冒充：

1. **周期识别**（:func:`detect_recurring`）：只从 ``billing_transactions`` 推断
   「同商户、相似金额、稳定间隔」的周期支出，输出 ``evidence="inferred"`` 与 ``confidence``，
   并强制 ``cancellable=False``、``note="仅疑似周期支出"``——只有流水证据时不得声称存在
   可取消订阅，只能建议用户去查询商户合同。
2. **合同查询**（:func:`list_subscriptions` / :func:`subscriptions_view`）：返回商户合同状态、
   下一扣费日、金额、终止条件、银行代扣授权状态与数据来源时间。订阅查询是**官方 YELLOW 例外**，
   返回结构带 ``requires_confirmation=True`` 供网关要求用户明确确认。
3. **两端取消**：
   ``subscription.cancel`` → :func:`cancel_subscription` 只改 ``Subscription.status``；
   ``mandate.revoke`` → :func:`mandates.revoke_mandate` 只改 ``Mandate.status``。
   两端独立，可一端成功一端失败；:func:`cancel_subscription_and_mandate` 合并两端结果为
   ``succeeded`` / ``partial_success`` / ``failed`` 并给出未解决义务说明。
   **任何取消都不自动产生退款**：退款是独立流程，只有实际回执到账才记资金。

写工具处理器约定（供执行器/网关调用）::

    TOOL_HANDLERS = {
        "subscription.cancel": cancel_subscription,
        "mandate.revoke": revoke_mandate,
        "reminder.create": create_reminder,
        "reminder.cancel": cancel_reminder,
    }
    # 签名 (session, ctx: AuthContext, operation: Operation) -> dict 领域结果
    # 在 submit_operation 成功后同事务调用；按 operation_id 幂等
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...ids import canonical_hash
from ...kernel.identity import AuthContext
from ...kernel.submission import transition
from ...models import BillingTransaction, Operation, Subscription
from ...money import format_minor
from ...security.policy import POLICY_VERSION
from ...timeutil import format_iso, to_utc, utcnow
from .mandates import (
    CONTRACT_STATUS_LABELS,
    MANDATE_STATUS_LABELS,
    mandate_status_view,
    mandate_view,
    resolve_mandate,
    revoke_mandate,
    revoke_mandate_leg,
)
from .reminders import (
    CHANNEL_LABELS,
    JOB_HANDLERS as REMINDER_JOB_HANDLERS,
    PERIOD_ALIASES,
    cancel_reminder,
    create_reminder,
    fire_reminder,
    handle_reminder_job,
    list_reminders,
    normalize_period,
    pending_reminder_count,
    shift_period,
)

# ---------------------------------------------------------------------------
# 常量：周期、阈值与文案（阈值是工程阈值，不是赛事评分线）
# ---------------------------------------------------------------------------

#: 周期口径与 ``contracts/schemas/subscription.json`` 一致（yearly；接受 annual 作为别名）
PERIOD_LABELS: dict[str, str] = {"weekly": "每周", "monthly": "每月", "quarterly": "每季度", "yearly": "每年"}
PERIOD_DAYS: dict[str, int] = {"weekly": 7, "monthly": 30, "quarterly": 91, "yearly": 365}
DOMAIN = "subscriptions"
RESULT_TYPE_CANCEL = "subscription_cancel"
SOURCE = "local_ledger"

SUBSCRIPTION_STATUS_LABELS: dict[str, str] = dict(CONTRACT_STATUS_LABELS)

#: 周期识别的确定性阈值
MONTHLY_INTERVAL_RANGE = (25.0, 35.0)      # 相邻扣费间隔（天）
ANNUAL_INTERVAL_RANGE = (330.0, 400.0)
INTERVAL_RELATIVE_TOLERANCE = 0.20         # 间隔相对中位数的最大偏差
AMOUNT_RELATIVE_TOLERANCE = 0.05           # 金额相对中位数的最大偏差
MIN_MONTHLY_CHARGES = 3                    # 每月订阅至少 3 笔（两个完整间隔）
MIN_ANNUAL_CHARGES = 2                     # 每年订阅至少 2 笔
MIN_GAP_DAYS = 5.0                         # 同商户短于该间隔的重复扣费不计入周期（属异常而非周期）

#: 不参与周期识别的流水类型：内部转账、资产调拨、退款与手续费不是「订阅支出」
EXCLUDED_KINDS = frozenset(
    {
        "transfer",
        "internal_transfer",
        "wealth_purchase",
        "wealth_redeem",
        "refund",
        "aa_settlement",
        "aa_transfer",
        "fee",
        "topup",
    }
)

INFERRED_NOTE = "仅疑似周期支出"
INFERRED_EVIDENCE_LABEL = "流水推断（非合同证据）"
CONTRACT_EVIDENCE_LABEL = "商户合同（可查询终止条件）"
SUBSCRIPTION_QUERY_EXCEPTION = "subscription_query_yellow"

#: 领域处理器只接受已经过 submit_operation 的操作
WRITE_OPERATION_STATUSES = ("authorized", "dispatching", "pending", "succeeded")


def _label(mapping: dict[str, str], status: str | None) -> str:
    if not status:
        return ""
    return mapping.get(status, status)


def _merchant_key(name: str | None) -> str:
    return "".join(str(name or "").split()).casefold()


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    size = len(ordered)
    if size == 0:
        return 0.0
    middle = size // 2
    if size % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


# ---------------------------------------------------------------------------
# 读取：合同查询（官方 YELLOW 例外）
# ---------------------------------------------------------------------------


def subscription_contract_view(subscription: Subscription, *, cancelled_at: datetime | None = None) -> dict:
    """``contracts/schemas/subscription.json`` 形状的合同对象。"""
    period = normalize_period(subscription.period) or subscription.period
    return {
        "subscription_id": subscription.subscription_id,
        "owner_user_id": subscription.owner_user_id,
        "merchant_id": subscription.merchant_id,
        "merchant_name": subscription.merchant_name,
        "amount_minor": int(subscription.amount_minor),
        "period": period,
        "next_charge_at": format_iso(subscription.next_charge_at) if subscription.next_charge_at else None,
        "status": subscription.status,
        "evidence": subscription.evidence,
        "is_contract_evidence": subscription.evidence == "contract",
        "terms_version": subscription.terms_version,
        "cancel_terms": subscription.cancel_terms,
        "mandate_id": None,
        "last_charged_at": format_iso(subscription.last_charged_at) if subscription.last_charged_at else None,
        "cancelled_at": format_iso(cancelled_at) if cancelled_at is not None else None,
        "version": subscription.version,
        "created_at": format_iso(subscription.created_at),
    }


def subscription_view(session: Session, subscription: Subscription) -> dict:
    """商户合同展示视图：合同状态、下一扣费日、金额、终止条件、代扣授权、来源与时间。"""
    now = utcnow()
    mandate = resolve_mandate(session, subscription.owner_user_id, subscription_id=subscription.subscription_id)
    next_charge_in_days = None
    if subscription.next_charge_at is not None:
        next_charge_in_days = int((to_utc(subscription.next_charge_at) - now).total_seconds() // 86400)
    period = normalize_period(subscription.period) or subscription.period

    obligations: list[str] = []
    if subscription.status == "active" and mandate is not None and mandate.status == "active":
        obligations.append("银行代扣授权仍然有效：取消商户合同不会自动解除代扣。")
    if mandate is not None and mandate.status == "pending_revocation":
        obligations.append("代扣解除尚未获得银行确认，授权仍可能扣款。")
    if subscription.status == "pending_cancellation":
        obligations.append("取消尚未获得商户确认，合同仍然有效。")

    contract = subscription_contract_view(subscription)
    if mandate is not None:
        contract["mandate_id"] = mandate.mandate_id
    return {
        **contract,
        "amount": format_minor(int(subscription.amount_minor)),
        "currency": "CNY",
        "period_label": PERIOD_LABELS.get(period, period),
        "period_days": PERIOD_DAYS.get(period),
        "status_label": _label(SUBSCRIPTION_STATUS_LABELS, subscription.status),
        "next_charge_in_days": next_charge_in_days,
        "evidence_label": CONTRACT_EVIDENCE_LABEL,
        "cancel_terms": subscription.cancel_terms or "商户未提供终止条件，需以商户条款为准。",
        "cancellable": subscription.status == "active",
        "cancellable_reason": (
            None
            if subscription.status == "active"
            else f"当前合同状态为 {_label(SUBSCRIPTION_STATUS_LABELS, subscription.status)}，不能重复取消。"
        ),
        "mandate": mandate_view(session, mandate, subscription=subscription) if mandate is not None else None,
        "mandate_status": mandate.status if mandate is not None else None,
        "mandate_status_label": _label(MANDATE_STATUS_LABELS, mandate.status if mandate is not None else None),
        "mandate_active": bool(mandate is not None and mandate.status == "active"),
        "requires_confirmation": True,
        "required_level": "YELLOW",
        "policy": {
            "exception": SUBSCRIPTION_QUERY_EXCEPTION,
            "policy_version": POLICY_VERSION,
            "note": "订阅查询是官方 YELLOW 例外：返回前需要用户明确确认。",
        },
        "unknown_obligations": obligations,
        "refund_created": False,
        "refund_policy": "取消不产生退款；已扣费退款是独立流程。",
        "source": {
            "table": "subscriptions",
            "data_source": "商户合同记录",
            "observed_at": format_iso(now),
            "next_charge_at": contract["next_charge_at"],
            "last_charged_at": contract["last_charged_at"],
        },
    }


def list_contracts(session: Session, ctx: AuthContext) -> list[Subscription]:
    return list(
        session.execute(
            select(Subscription)
            .where(Subscription.owner_user_id == ctx.user_id)
            .order_by(Subscription.next_charge_at.is_(None), Subscription.next_charge_at, Subscription.subscription_id)
        ).scalars()
    )


def list_subscriptions(session: Session, ctx: AuthContext, *, include_inferred: bool = True) -> list[dict]:
    """合同订阅 + （可选）疑似周期支出。

    合同项 ``evidence="contract"``（可取消）；疑似项 ``evidence="inferred"``、
    ``cancellable=False``、``note="仅疑似周期支出"``（不可取消，只能建议去查询合同）。
    """
    ctx.require_customer()
    contracts = list_contracts(session, ctx)
    items = [subscription_view(session, s) for s in contracts]
    if not include_inferred:
        return items

    contract_keys = {_merchant_key(s.merchant_name or s.merchant_id) for s in contracts}
    for entry in detect_recurring(session, ctx):
        entry = dict(entry)
        entry["subscription_id"] = None
        if entry["merchant_key"] in contract_keys:
            # 已有合同证据：合同项才是权威来源，推断项不再重复展示
            continue
        items.append(entry)
    items.sort(key=lambda item: (item.get("next_charge_at") or item.get("next_estimate_at") or "~", item.get("subscription_id") or ""))
    return items


def subscriptions_view(session: Session, ctx: AuthContext, *, include_inferred: bool = True) -> dict:
    """``subscription.list`` 的返回信封：带官方 YELLOW 例外标记，供网关二次确认。"""
    items = list_subscriptions(session, ctx, include_inferred=include_inferred)
    contract_count = sum(1 for i in items if i["evidence"] == "contract")
    inferred_count = len(items) - contract_count
    return {
        "subscriptions": items,
        "count": len(items),
        "contract_count": contract_count,
        "inferred_count": inferred_count,
        "requires_confirmation": True,
        "required_level": "YELLOW",
        "policy": {
            "exception": SUBSCRIPTION_QUERY_EXCEPTION,
            "policy_version": POLICY_VERSION,
            "note": "订阅查询保留官方 YELLOW 例外：查询也要用户明确确认。",
        },
        "detection": {
            "method": "same_merchant + similar_amount + stable_interval",
            "note": "仅流水推断的疑似周期支出不可取消，必须另行查询商户合同。",
            "inferred_note": INFERRED_NOTE,
        },
        "observed_at": format_iso(utcnow()),
        "user_id": ctx.user_id,
    }


# ---------------------------------------------------------------------------
# 读取：周期识别（只有流水证据时不声称存在可取消订阅）
# ---------------------------------------------------------------------------


def _daily_first(charges: list[BillingTransaction]) -> list[BillingTransaction]:
    """同一天的同商户重复扣费只保留最早一笔（重复扣费属异常识别，不是周期证据）。"""
    kept: list[BillingTransaction] = []
    seen_days: set = set()
    for bt in charges:
        if bt.business_day in seen_days:
            continue
        seen_days.add(bt.business_day)
        kept.append(bt)
    return kept


def _detect_entry(
    merchant_name: str,
    charges: list[BillingTransaction],
    *,
    contract: Subscription | None,
    now: datetime,
) -> dict | None:
    if len(charges) < MIN_ANNUAL_CHARGES:
        return None
    amounts = [abs(int(c.amount_minor or 0)) for c in charges]
    gaps = [
        (to_utc(charges[i + 1].posted_at) - to_utc(charges[i].posted_at)).total_seconds() / 86400.0
        for i in range(len(charges) - 1)
    ]
    gaps = [g for g in gaps if g >= MIN_GAP_DAYS]
    if not gaps:
        return None
    median_gap = _median(gaps)
    median_amount = int(round(_median([float(a) for a in amounts])))

    period: str | None = None
    if MONTHLY_INTERVAL_RANGE[0] <= median_gap <= MONTHLY_INTERVAL_RANGE[1]:
        period = "monthly"
    elif ANNUAL_INTERVAL_RANGE[0] <= median_gap <= ANNUAL_INTERVAL_RANGE[1]:
        period = "yearly"
    if period is None:
        return None
    required_charges = MIN_MONTHLY_CHARGES if period == "monthly" else MIN_ANNUAL_CHARGES
    if len(charges) < required_charges:
        return None

    gap_tolerance = max(3.0, median_gap * INTERVAL_RELATIVE_TOLERANCE)
    max_gap_dev = max(abs(g - median_gap) for g in gaps)
    if max_gap_dev > gap_tolerance:
        return None

    amount_tolerance = max(1, int(round(median_amount * AMOUNT_RELATIVE_TOLERANCE)))
    max_amount_dev = max(abs(a - median_amount) for a in amounts)
    if max_amount_dev > amount_tolerance:
        return None

    interval_stability = 1.0 - min(1.0, max_gap_dev / max(median_gap, 1e-9))
    amount_stability = 1.0 - min(1.0, max_amount_dev / max(median_amount, 1))
    confidence = round(
        min(0.95, 0.40 + 0.10 * (len(charges) - 2) + 0.25 * interval_stability + 0.20 * amount_stability),
        2,
    )

    last_charge_at = to_utc(charges[-1].posted_at)
    next_estimate_at = shift_period(last_charge_at, period)
    detection_id = "rec_inferred_" + canonical_hash(
        {
            "merchant_key": _merchant_key(merchant_name),
            "amount_minor": median_amount,
            "period": period,
            "charge_count": len(charges),
            "last_charge_at": format_iso(last_charge_at),
        }
    )[:12]

    reason = (
        f"同一商户 {len(charges)} 笔支出：金额相近（中位数 {format_minor(median_amount)} 元，"
        f"最大偏差 {format_minor(max_amount_dev)} 元）、间隔稳定（中位数 {median_gap:.1f} 天，"
        f"最大偏差 {max_gap_dev:.1f} 天），符合{PERIOD_LABELS[period]}周期特征。"
    )
    return {
        "detection_id": detection_id,
        "subscription_id": None,
        "merchant_id": "",
        "merchant_key": _merchant_key(merchant_name),
        "merchant_name": merchant_name,
        "amount_minor": median_amount,
        "amount": format_minor(median_amount),
        "currency": "CNY",
        "period": period,
        "period_label": PERIOD_LABELS[period],
        "period_days": PERIOD_DAYS[period],
        "interval_days": round(median_gap, 1),
        "charge_count": len(charges),
        "evidence": "inferred",
        "evidence_label": INFERRED_EVIDENCE_LABEL,
        "confidence": confidence,
        "last_charge_at": format_iso(last_charge_at),
        "next_estimate_at": format_iso(next_estimate_at),
        "reason": reason,
        "cancellable": False,
        "note": INFERRED_NOTE,
        "cancellable_reason": "只有流水证据，未查到商户合同：不能声称存在可取消订阅，建议先查询商户合同。",
        "requires_confirmation": True,
        "required_level": "YELLOW",
        "matched_subscription_id": contract.subscription_id if contract is not None else None,
        "matched_subscription_status": contract.status if contract is not None else None,
        "transaction_ids": [c.bt_id for c in charges],
        "charges": [
            {"bt_id": c.bt_id, "posted_at": format_iso(c.posted_at), "amount_minor": abs(int(c.amount_minor or 0))}
            for c in charges
        ],
        "source": {
            "table": "billing_transactions",
            "data_source": "本行账单流水（推断，非合同证据）",
            "observed_at": format_iso(now),
            "method": "same_merchant + similar_amount + stable_interval",
            "thresholds": {
                "interval_relative_tolerance": INTERVAL_RELATIVE_TOLERANCE,
                "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
                "monthly_interval_days": list(MONTHLY_INTERVAL_RANGE),
                "annual_interval_days": list(ANNUAL_INTERVAL_RANGE),
                "min_monthly_charges": MIN_MONTHLY_CHARGES,
                "min_annual_charges": MIN_ANNUAL_CHARGES,
            },
        },
    }


def detect_recurring(session: Session, ctx: AuthContext) -> list[dict]:
    """从流水识别月度／年度周期支出，输出 ``evidence="inferred"`` 的疑似项。

    只读、确定性：同商户（``merchant_name``，缺失时退回 ``counterparty``）分组，
    金额相近、间隔稳定；内部转账、理财申赎、退款与手续费不参与识别。
    """
    ctx.require_customer()
    rows = session.execute(
        select(BillingTransaction)
        .where(BillingTransaction.owner_user_id == ctx.user_id)
        .order_by(BillingTransaction.posted_at, BillingTransaction.bt_id)
    ).scalars().all()

    groups: dict[str, list[BillingTransaction]] = {}
    display_names: dict[str, str] = {}
    for bt in rows:
        if bt.direction != "out" or bt.kind in EXCLUDED_KINDS:
            continue
        if abs(int(bt.amount_minor or 0)) <= 0:
            continue
        name = (bt.merchant_name or bt.counterparty or "").strip()
        if not name:
            continue
        key = _merchant_key(name)
        groups.setdefault(key, []).append(bt)
        display_names.setdefault(key, name)

    contracts = {_merchant_key(s.merchant_name or s.merchant_id): s for s in list_contracts(session, ctx)}
    now = utcnow()
    entries: list[dict] = []
    for key in sorted(groups):
        charges = _daily_first(groups[key])
        entry = _detect_entry(display_names[key], charges, contract=contracts.get(key), now=now)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: (e["next_estimate_at"], e["merchant_key"]))
    return entries


# ---------------------------------------------------------------------------
# 写工具：取消商户合同（subscription.cancel）
# ---------------------------------------------------------------------------


def _assert_write_operation(ctx: AuthContext, operation: Operation, *, tool: str) -> None:
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
    if stored.get("domain") != DOMAIN or stored.get("result_type") != RESULT_TYPE_CANCEL:
        return None
    return dict(stored)


def _store_result(session: Session, operation: Operation, result: dict) -> None:
    operation.domain_result = result
    session.flush()


def cancel_subscription(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """工具 ``subscription.cancel``：**只取消商户合同**，不动银行代扣、不产生退款。

    - 合同不存在或不属于当前用户 → ``OBJECT_NOT_ACCESSIBLE``（不泄露他人对象）；
    - ``expected_version`` 不一致 → ``VERSION_CONFLICT``（合同未被改动，仍然有效）；
    - 合同已结束（``ended``）→ ``NOT_REVERSIBLE``；
    - 商户侧尚未确认（``pending_cancellation``）→ ``CANCEL_NOT_EFFECTIVE``，合同仍有效；
    - 已取消 → 幂等成功（``already_cancelled=True``）。

    返回值中的 ``mandate_still_active`` 与 ``unresolved_obligations`` 用于如实展示
    「合同已取消但代扣仍在」这一未解决义务。
    """
    _assert_write_operation(ctx, operation, tool="subscription.cancel")

    stored = _stored_result(operation)
    if stored is not None:
        stored["idempotent_replay"] = True
        return stored

    args = operation.typed_args or {}
    subscription_id = args.get("subscription_id")
    if not subscription_id:
        raise FinPilotError(
            "PARAM_INVALID",
            "取消订阅需要提供 subscription_id。",
            details={"reason_codes": ["missing_subscription_id"]},
        )

    subscription = session.execute(
        select(Subscription)
        .where(Subscription.subscription_id == subscription_id, Subscription.owner_user_id == ctx.user_id)
        .with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise FinPilotError(
            "OBJECT_NOT_ACCESSIBLE",
            "无法访问该订阅。",
            details={"reason_codes": ["subscription_not_found"]},
        )

    expected_version = args.get("expected_version")
    if expected_version is not None and int(expected_version) != int(subscription.version):
        raise FinPilotError(
            "VERSION_CONFLICT",
            "对象已被其他操作更新，请刷新后重试。",
            details={
                "reason_codes": ["version_mismatch"],
                "current_version": subscription.version,
                "contract_status": subscription.status,
            },
        )

    now = utcnow()
    status = subscription.status
    if status == "ended":
        raise FinPilotError(
            "NOT_REVERSIBLE",
            "该订阅已结束，不能再次取消。",
            details={"reason_codes": ["subscription_ended"], "subscription_id": subscription.subscription_id},
        )
    if status == "pending_cancellation":
        raise FinPilotError(
            "CANCEL_NOT_EFFECTIVE",
            "商户侧尚未确认取消，合同仍然有效。",
            details={
                "reason_codes": ["merchant_not_confirmed"],
                "subscription_id": subscription.subscription_id,
                "contract_status": status,
            },
        )
    if status not in ("active", "cancelled"):
        raise FinPilotError(
            "POLICY_DENIED",
            "该订阅状态不支持取消。",
            details={"reason_codes": ["subscription_status_not_cancellable"], "contract_status": status},
        )

    already_cancelled = status == "cancelled"
    changed = not already_cancelled
    if changed:
        subscription.status = "cancelled"
        subscription.next_charge_at = None
        subscription.version += 1
        session.flush()
    cancelled_at = now if changed else None

    mandate = resolve_mandate(session, ctx.user_id, subscription_id=subscription.subscription_id)
    mandate_active = bool(mandate is not None and mandate.status == "active")
    pending_reminders = pending_reminder_count(
        session, ctx.user_id, subscription_id=subscription.subscription_id
    )

    unresolved: list[str] = []
    next_actions: list[str] = []
    warnings: list[str] = []
    if mandate_active:
        unresolved.append(
            "银行代扣授权仍然有效：取消商户合同不会自动解除代扣，仍可能被扣款。"
        )
        next_actions.append("如不再希望扣款，请另行确认执行 mandate.revoke（解除银行代扣，需强验证）。")
    elif mandate is not None and mandate.status == "pending_revocation":
        unresolved.append("代扣解除尚未获得银行确认，授权仍可能扣款。")
    if pending_reminders:
        warnings.append(f"仍有 {pending_reminders} 条续费提醒处于已设置状态，取消合同不会自动删除同意记录。")
        next_actions.append(f"可按需执行 reminder.cancel 取消这些提醒（{pending_reminders} 条）。")
    unresolved.append("取消不自动退款：已扣费退款是独立流程，只有实际回执到账才记资金。")

    result = {
        "domain": DOMAIN,
        "result_type": RESULT_TYPE_CANCEL,
        "data": subscription_contract_view(subscription, cancelled_at=cancelled_at),
        "source": SOURCE,
        "observed_at": format_iso(now),
        "warnings": list(warnings),
        "status": "succeeded",
        "tool": "subscription.cancel",
        "leg": "merchant_contract",
        "operation_id": operation.operation_id,
        "task_id": operation.task_id,
        "already_cancelled": already_cancelled,
        "changed": changed,
        "subscription": subscription_view(session, subscription),
        "mandate": mandate_view(session, mandate, subscription=subscription) if mandate is not None else None,
        "mandate_still_active": mandate_active,
        "pending_reminders": pending_reminders,
        "unresolved_obligations": unresolved,
        "next_actions": next_actions,
        "refund_created": False,
        "refund_policy": "取消不产生退款；退款是独立流程，只有实际回执到账才记资金。",
        "effect": {
            "contract_status": subscription.status,
            "next_charge_at": None,
            "cancelled_at": format_iso(cancelled_at) if cancelled_at is not None else None,
        },
        "reason": args.get("reason") or "user_requested",
    }
    _store_result(session, operation, result)
    transition(
        session,
        operation,
        "succeeded",
        reason="subscription_cancelled",
        detail={"subscription_id": subscription.subscription_id},
    )
    session.flush()
    return result


def cancel_subscription_leg(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """协调器专用：把商户合同取消的异常转成结构化的一端失败结果。"""
    try:
        return {"ok": True, "result": cancel_subscription(session, ctx, operation)}
    except FinPilotError as exc:
        return {
            "ok": False,
            "error": exc.to_error_object(),
            "operation_id": getattr(operation, "operation_id", None),
            "requested_at": format_iso(utcnow()),
        }


def cancel_subscription_and_mandate(
    session: Session,
    ctx: AuthContext,
    operation: Operation,
    *,
    mandate_operation: Operation | None = None,
) -> dict:
    """「以后别再扣了」的合并视图：两端分别执行、分别记录，返回部分成功与未解决义务。

    本函数不是工具：两端仍各自需要独立的操作与授权（``subscription.cancel`` 为 YELLOW，
    ``mandate.revoke`` 为 RED 且需强验证），这里只把两端结果合并且不掩盖任何一端失败。
    """
    contract_leg = cancel_subscription_leg(session, ctx, operation)
    mandate_leg: dict | None = None
    if mandate_operation is not None:
        mandate_leg = revoke_mandate_leg(session, ctx, mandate_operation)
    elif (operation.typed_args or {}).get("revoke_mandate"):
        mandate_leg = {
            "ok": False,
            "error": {
                "code": "GRANT_REQUIRED",
                "safe_message": "解除银行代扣需要独立的强验证授权，尚未执行。",
                "retry_mode": "after_reauthorization",
            },
            "operation_id": None,
            "requested_at": format_iso(utcnow()),
        }

    attempted: list[tuple[str, dict]] = [("merchant_contract", contract_leg)]
    if mandate_leg is not None:
        attempted.append(("bank_mandate", mandate_leg))
    succeeded = [leg for _name, leg in attempted if leg["ok"]]
    failed = [leg for _name, leg in attempted if not leg["ok"]]
    if failed and succeeded:
        status = "partial_success"
    elif failed and not succeeded:
        status = "failed"
    else:
        status = "succeeded"

    contract_ok = contract_leg["ok"]
    mandate_ok = mandate_leg is not None and mandate_leg["ok"]
    notes: list[str] = []
    unresolved: list[str] = []
    next_actions: list[str] = []
    if status == "succeeded":
        if mandate_leg is None:
            notes.append("商户合同已取消。银行代扣未在本任务范围内处理。")
            if contract_ok:
                unresolved.extend(contract_leg["result"].get("unresolved_obligations", []))
        else:
            notes.append("商户合同已取消，银行代扣已解除。")
            notes.append("两端状态互相独立，均已在本结果中记录。")
    elif status == "partial_success" and contract_ok and not mandate_ok:
        notes.append("部分成功：商户合同已取消，但银行代扣解除未生效。")
        unresolved.append(
            "银行代扣授权仍可能扣款："
            + str((mandate_leg or {}).get("error", {}).get("safe_message", "解除未生效"))
        )
        next_actions.append("请重试 mandate.revoke（需要重新完成强验证）或转人工处理。")
    elif status == "partial_success" and mandate_ok and not contract_ok:
        notes.append("部分成功：银行代扣已解除，但商户合同取消失败。")
        notes.append("说明：商户服务合同仍然有效，可能继续计费并产生欠费或被停服。")
        unresolved.append(
            "商户服务合同仍有效："
            + str(contract_leg.get("error", {}).get("safe_message", "取消失败"))
            + "（合同状态未改变）"
        )
        next_actions.append("请确认合同终止方式（重试 subscription.cancel 或联系商户）后再次处理。")
    else:
        notes.append("两端均未生效：合同与代扣状态未改变。")
        unresolved.append("取消未生效，需要重新确认或转人工处理。")

    unresolved.append("任何取消都不自动产生退款：已扣费退款是独立流程，只有实际回执到账才记资金。")
    contract_result = contract_leg.get("result") or {}
    mandate_result = (mandate_leg or {}).get("result") or {}
    result = {
        "domain": DOMAIN,
        "result_type": "subscription_cancel_and_mandate",
        "data": {
            "subscription_id": (operation.typed_args or {}).get("subscription_id"),
            "contract_status": (contract_result.get("data") or {}).get("status"),
            "mandate_status": (mandate_result.get("data") or {}).get("status"),
            "legs_attempted": [name for name, _leg in attempted],
            "legs_succeeded": [name for name, leg in attempted if leg["ok"]],
            "legs_failed": [name for name, leg in attempted if not leg["ok"]],
        },
        "source": SOURCE,
        "observed_at": format_iso(utcnow()),
        "warnings": list(notes),
        "status": status,
        "tool": "subscription.cancel+mandate.revoke",
        "subscription_id": (operation.typed_args or {}).get("subscription_id"),
        "contract_leg": contract_leg,
        "mandate_leg": mandate_leg,
        "legs_attempted": [name for name, _leg in attempted],
        "legs_succeeded": [name for name, leg in attempted if leg["ok"]],
        "legs_failed": [name for name, leg in attempted if not leg["ok"]],
        "summary": " ".join(notes),
        "notes": notes,
        "unresolved_obligations": unresolved,
        "next_actions": next_actions,
        "refund_created": False,
        "refund_policy": "取消不产生退款；退款是独立流程，只有实际回执到账才记资金。",
    }
    return result


# ---------------------------------------------------------------------------
# 只读工具处理器与写工具处理器注册
# ---------------------------------------------------------------------------


def subscription_list_handler(session: Session, ctx: AuthContext, typed_args: dict | None = None) -> dict:
    """``subscription.list`` 只读处理器：带官方 YELLOW 例外标记。"""
    args = dict(typed_args or {})
    return subscriptions_view(session, ctx, include_inferred=bool(args.get("include_inferred", True)))


def mandate_status_handler(session: Session, ctx: AuthContext, typed_args: dict | None = None) -> dict:
    """``mandate.status`` 只读处理器：分别展示银行代扣状态。"""
    args = dict(typed_args or {})
    return mandate_status_view(session, ctx, subscription_id=args.get("subscription_id"))


def reminder_list_handler(session: Session, ctx: AuthContext, typed_args: dict | None = None) -> dict:
    """提醒清单（同意记录 + 已触发的模拟提醒），供网关展示。"""
    args = dict(typed_args or {})
    reminders = list_reminders(session, ctx, include_fired=bool(args.get("include_fired", True)))
    return {
        "reminders": reminders,
        "count": len(reminders),
        "scheduled_count": sum(1 for r in reminders if r["status"] == "scheduled"),
        "fired_count": sum(1 for r in reminders if r["status"] == "fired"),
        "delivery_mode": "simulated",
        "channel_labels": dict(CHANNEL_LABELS),
        "observed_at": format_iso(utcnow()),
    }


READ_HANDLERS = {
    "subscription.list": subscription_list_handler,
    "mandate.status": mandate_status_handler,
    "reminder.list": reminder_list_handler,
}

TOOL_HANDLERS = {
    "subscription.cancel": cancel_subscription,
    "mandate.revoke": revoke_mandate,
    "reminder.create": create_reminder,
    "reminder.cancel": cancel_reminder,
}

#: 供 workers.scheduler 的 job_type 分派点注册（与 reminders.JOB_HANDLERS 同源）
JOB_HANDLERS = dict(REMINDER_JOB_HANDLERS)

__all__ = [
    "CHANNEL_LABELS",
    "DOMAIN",
    "INFERRED_NOTE",
    "JOB_HANDLERS",
    "PERIOD_ALIASES",
    "PERIOD_LABELS",
    "READ_HANDLERS",
    "RESULT_TYPE_CANCEL",
    "SOURCE",
    "SUBSCRIPTION_QUERY_EXCEPTION",
    "TOOL_HANDLERS",
    "cancel_reminder",
    "cancel_subscription",
    "cancel_subscription_and_mandate",
    "create_reminder",
    "detect_recurring",
    "fire_reminder",
    "handle_reminder_job",
    "list_contracts",
    "list_reminders",
    "list_subscriptions",
    "normalize_period",
    "resolve_mandate",
    "revoke_mandate",
    "subscription_contract_view",
    "subscription_view",
    "subscriptions_view",
]