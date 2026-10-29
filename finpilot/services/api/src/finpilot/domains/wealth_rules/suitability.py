"""适当性校验：风险、期限、预算、起购与净值数据（V5 第 11 节 S3-01／S3-02、T09）。

``validate_domain`` 是**服务端硬约束**入口：先于推荐排序执行，结论只来自
持久化事实（测评记录、产品条款、净值序列、资金快照），不接受客户端或模型下调。

阻断规则（``blocking_reason_codes``）：

- ``ASSESSMENT_REQUIRED``：无有效测评、测评过期或被新版本取代。
- ``RISK_MISMATCH``：产品风险等级高于用户承受等级。
- ``TERM_CONFLICT``：期限与用款时间冲突（``term_days + settlement_days > horizon_days``）。
- ``BUDGET_EXCEEDED``：低于起购、含费超预算、或超过快照可支配金额。
- ``NAV_UNAVAILABLE``：完全没有净值数据，无法给出任何可核对的份额估算。

非阻断规则（``reason_codes`` 中保留、可在方案里提示）：

- ``INSUFFICIENT_HISTORY``：净值样本不足以计算正式风险指标，只能给估算区间。
- ``STALE_SNAPSHOT``：快照观测时间过旧，提交前必须重检。
- ``PRODUCT_INACTIVE``：产品已下架（阻断）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...models import ProductNav, WealthProduct
from ...money import format_minor
from ...timeutil import business_day, parse_iso, utcnow
from . import assessment, terms

VALIDATION_VERSION = "suitability-2026.10"

#: 年化波动等正式指标的样本门槛（与 StockQuant 适配器保持一致）。
MIN_NAV_HISTORY = 60

#: 快照超过该秒数即视为过旧，只能提示、不阻断（阻断发生在提交时的重检）。
SNAPSHOT_MAX_AGE_SECONDS = 300

#: 本规则引擎负责的门槛；其余理财工具由对应领域负责。
WEALTH_TOOLS = frozenset({"wealth.purchase", "wealth.compare"})
NON_GATED_TOOLS = frozenset({"wealth.redeem", "wealth.status"})


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def operation_args(operation: Any) -> dict[str, Any]:
    """把 dict 或 ``Operation`` 统一成参数视图（``typed_args`` 优先）。"""
    if operation is None:
        return {}
    if isinstance(operation, Mapping):
        return dict(operation)
    args: dict[str, Any] = {}
    typed = getattr(operation, "typed_args", None)
    if isinstance(typed, Mapping):
        args.update(typed)
    for name in ("tool", "operation_id", "product_id", "amount_minor", "fee_minor", "source_account_id", "task_id"):
        if name in args and args[name] is not None:
            continue
        value = getattr(operation, name, None)
        if value is not None:
            args[name] = value
    return args


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FinPilotError(
            "PARAM_INVALID", f"{field} 必须是整数。",
            details={"reason_codes": ["bad_integer"], "field": field},
        ) from exc


def _snapshot_spendable_minor(snapshot: Mapping[str, Any] | None) -> int | None:
    if not isinstance(snapshot, Mapping):
        return None
    group = snapshot.get("group")
    if isinstance(group, Mapping) and group.get("spendable_minor") is not None:
        return int(group["spendable_minor"])
    accounts = snapshot.get("accounts")
    if isinstance(accounts, list) and accounts:
        total = 0
        for account in accounts:
            if isinstance(account, Mapping):
                total += int(account.get("spendable_minor") or 0)
        return total
    return None


def _snapshot_age_seconds(snapshot: Mapping[str, Any] | None) -> float | None:
    if not isinstance(snapshot, Mapping):
        return None
    observed = snapshot.get("observed_at")
    if not observed:
        return None
    try:
        observed_at = parse_iso(str(observed))
    except ValueError:
        return None
    return (utcnow() - observed_at).total_seconds()


def nav_history(
    session: Session, product_id: str, *, as_of: date | None = None
) -> list[ProductNav]:
    stmt = select(ProductNav).where(ProductNav.product_id == product_id)
    if as_of is not None:
        stmt = stmt.where(ProductNav.as_of <= as_of)
    return list(session.execute(stmt.order_by(ProductNav.as_of.asc())).scalars())


def _check(
    rule: str,
    reason_code: str,
    *,
    passed: bool,
    blocking: bool,
    message: str,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "rule": rule,
        "reason_code": reason_code,
        "passed": passed,
        "blocking": bool(blocking) and not passed,
        "message": message,
        "detail": dict(detail or {}),
    }


def validate_domain(
    session: Session,
    ctx: Any,
    operation: Any,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 ``{valid, reason_codes, validation_version}`` 及可解释的逐条检查。"""
    if ctx is not None:
        ctx.require_customer()

    args = operation_args(operation)
    tool = str(args.get("tool") or "wealth.purchase")
    today = business_day(utcnow())

    base: dict[str, Any] = {
        "valid": True,
        "reason_codes": [],
        "blocking_reason_codes": [],
        "validation_version": VALIDATION_VERSION,
        "checks": [],
        "tool": tool,
        "applicable": True,
        "operation_id": args.get("operation_id"),
        "product_id": args.get("product_id"),
        "estimate": None,
        "profile": None,
        "product": None,
        "warnings": [],
    }

    if tool in NON_GATED_TOOLS:
        return {
            **base,
            "applicable": False,
            "reason_codes": ["NOT_APPLICABLE_TOOL"],
            "notes": [f"{tool} 不经过适当性门槛；赎回份额与到账由理财订单领域校验。"],
        }
    if tool not in WEALTH_TOOLS:
        return {
            **base,
            "applicable": False,
            "reason_codes": ["NOT_APPLICABLE_TOOL"],
            "notes": [f"{tool} 不是理财工具，本校验不适用。"],
        }

    product_id = str(args.get("product_id") or "").strip()
    if not product_id:
        raise FinPilotError("PARAM_INVALID", "缺少 product_id。", details={"reason_codes": ["missing_product_id"]})
    product = session.get(WealthProduct, product_id)
    if product is None:
        raise FinPilotError(
            "PARAM_INVALID", "产品不存在或已下架。",
            details={"reason_codes": ["product_not_found"], "product_id": product_id},
        )

    amount = _as_int(args.get("amount_minor") or 0, "amount_minor")
    if amount <= 0:
        raise FinPilotError("PARAM_INVALID", "申购金额必须大于 0。", details={"reason_codes": ["bad_amount"]})
    fee = args.get("fee_minor")
    fee_minor = terms.product_fee_minor(product, amount) if fee is None else _as_int(fee, "fee_minor")
    budget = args.get("budget_minor")
    budget_minor = None if budget is None else _as_int(budget, "budget_minor")
    horizon_days = args.get("horizon_days", args.get("need_within_days"))
    horizon = None if horizon_days is None else _as_int(horizon_days, "horizon_days")

    checks: list[dict[str, Any]] = []

    # --- 1 测评有效性 ------------------------------------------------------
    profile = assessment.current_profile(session, ctx) if ctx is not None else {
        "status": "none",
        "usable": False,
        "risk_level": None,
        "reason_codes": ["profile_not_found"],
    }
    assessment_ok = bool(profile["usable"])
    checks.append(
        _check(
            "assessment_valid",
            "ASSESSMENT_REQUIRED",
            passed=assessment_ok,
            blocking=True,
            message="已完成有效风险测评。" if assessment_ok else f"测评状态为 {profile['status']}，申购前必须完成有效测评。",
            detail={
                "profile_status": profile["status"],
                "questionnaire_version": profile.get("questionnaire_version"),
                "risk_level": profile.get("risk_level"),
                "expires_at": profile.get("expires_at"),
            },
        )
    )

    # --- 2 产品可用性 ------------------------------------------------------
    product_active = bool(getattr(product, "is_active", True))
    checks.append(
        _check(
            "product_active",
            "PRODUCT_INACTIVE",
            passed=product_active,
            blocking=True,
            message="产品在售。" if product_active else "产品已下架，不能申购。",
            detail={"product_id": product.product_id, "terms_version": product.terms_version},
        )
    )

    # --- 3 风险等级 --------------------------------------------------------
    product_level = str(product.issuer_risk_level or "R5")
    user_level = profile.get("risk_level")
    risk_detail: dict[str, Any] = {
        "product_risk_level": product_level,
        "user_risk_level": user_level,
        "evaluated": assessment_ok,
        "risk_rank": {
            "product": assessment.RISK_RANK.get(product_level),
            "user": assessment.RISK_RANK.get(str(user_level)) if user_level else None,
        },
    }
    if not assessment_ok:
        # 无有效测评时风险比对不执行：阻断原因由 ASSESSMENT_REQUIRED 单独承担，
        # 避免同一条件被记成两个不同的阻断原因。
        checks.append(
            _check(
                "risk_within_profile",
                "RISK_MISMATCH",
                passed=True,
                blocking=False,
                message="缺少有效测评，风险等级比对未执行；阻断原因见 ASSESSMENT_REQUIRED。",
                detail=risk_detail,
            )
        )
    else:
        risk_ok = assessment.RISK_RANK.get(product_level, 5) <= assessment.RISK_RANK.get(str(user_level), 0)
        checks.append(
            _check(
                "risk_within_profile",
                "RISK_MISMATCH",
                passed=risk_ok,
                blocking=True,
                message=(
                    f"产品风险 {product_level} 未超过用户承受等级 {user_level}。"
                    if risk_ok
                    else f"产品风险 {product_level} 高于用户承受等级 {user_level}。"
                ),
                detail=risk_detail,
            )
        )

    # --- 4 期限冲突 --------------------------------------------------------
    required_days = int(product.term_days or 0) + terms.settlement_days(product)
    term_ok = True if horizon is None else required_days <= horizon
    checks.append(
        _check(
            "term_within_horizon",
            "TERM_CONFLICT",
            passed=term_ok,
            blocking=True,
            message=(
                f"期限 {product.term_days} 天 + 到账 {terms.settlement_days(product)} 天 ≤ 用款时间 {horizon} 天。"
                if term_ok
                else f"产品需要约 {required_days} 天（期限 {product.term_days} 天 + 到账 "
                f"{terms.settlement_days(product)} 天），晚于用款时间 {horizon} 天。"
            ),
            detail={
                "term_days": int(product.term_days or 0),
                "settlement_days": terms.settlement_days(product),
                "required_holding_days": required_days,
                "horizon_days": horizon,
                "formula": "required_holding_days = term_days + settlement_days",
            },
        )
    )

    # --- 5 起购与预算 ------------------------------------------------------
    below_min = amount < int(product.min_minor or 0)
    fee_over_budget = budget_minor is not None and amount + fee_minor > budget_minor
    spendable = _snapshot_spendable_minor(snapshot)
    over_spendable = spendable is not None and amount + fee_minor > spendable
    budget_ok = not (below_min or fee_over_budget or over_spendable)
    budget_messages: list[str] = []
    if below_min:
        budget_messages.append(f"金额 {format_minor(amount)} 元低于起购 {format_minor(int(product.min_minor or 0))} 元")
    if fee_over_budget:
        budget_messages.append(
            f"金额 + 手续费合计 {format_minor(amount + fee_minor)} 元超过预算 {format_minor(int(budget_minor or 0))} 元"
        )
    if over_spendable:
        budget_messages.append(
            f"金额 + 手续费合计 {format_minor(amount + fee_minor)} 元超过可支配金额 {format_minor(int(spendable or 0))} 元"
        )
    checks.append(
        _check(
            "budget_within_limits",
            "BUDGET_EXCEEDED",
            passed=budget_ok,
            blocking=True,
            message="；".join(budget_messages) + "。" if budget_messages else "起购、含费预算与可支配金额均满足。",
            detail={
                "amount_minor": amount,
                "fee_minor": fee_minor,
                "total_cost_minor": amount + fee_minor,
                "min_minor": int(product.min_minor or 0),
                "budget_minor": budget_minor,
                "spendable_minor": spendable,
                "reason_codes": [code for code, hit in (
                    ("below_min_amount", below_min),
                    ("fee_over_budget", fee_over_budget),
                    ("insufficient_spendable", over_spendable),
                ) if hit],
            },
        )
    )

    # --- 6 净值数据与份额估算 ---------------------------------------------
    navs = nav_history(session, product.product_id, as_of=today)
    nav_count = len(navs)
    history_ok = nav_count >= MIN_NAV_HISTORY
    nav_available = nav_count > 0
    checks.append(
        _check(
            "nav_history_sufficient",
            "INSUFFICIENT_HISTORY",
            passed=history_ok,
            blocking=not nav_available,
            message=(
                f"净值样本 {nav_count} 个，满足正式指标门槛 {MIN_NAV_HISTORY}。"
                if history_ok
                else f"净值样本仅 {nav_count} 个（< {MIN_NAV_HISTORY}），只能给估算区间，不输出正式风险比较值。"
            ),
            detail={
                "sample_count": nav_count,
                "required_sample_count": MIN_NAV_HISTORY,
                "estimate_only": not history_ok,
            },
        )
    )
    checks.append(
        _check(
            "nav_available",
            "NAV_UNAVAILABLE",
            passed=nav_available,
            blocking=True,
            message=(
                "存在可核对净值，可给估算份额。"
                if nav_available
                else "完全没有净值数据，无法给出任何可核对的份额估算，不编造持仓。"
            ),
            detail={"sample_count": nav_count},
        )
    )

    estimate: dict[str, Any]
    if nav_available:
        latest = navs[-1]
        estimate = terms.estimate_shares(amount, int(latest.nav_e6), fee_minor)
        estimate["nav_as_of"] = latest.as_of.isoformat()
        estimate["nav_terms_version"] = latest.terms_version
        estimate["range"] = terms.estimate_shares_range(
            amount,
            min(int(nav.nav_e6) for nav in navs),
            max(int(nav.nav_e6) for nav in navs),
            fee_minor,
        )
        estimate["estimate_only"] = not history_ok
    else:
        estimate = {
            **terms.estimate_shares(amount, None, fee_minor),
            "estimate_only": True,
            "range": terms.estimate_shares_range(amount, None, None, fee_minor),
        }

    # --- 7 快照新鲜度（非阻断） -------------------------------------------
    age_seconds = _snapshot_age_seconds(snapshot)
    if snapshot is None:
        checks.append(
            _check(
                "snapshot_provided",
                "STALE_SNAPSHOT",
                passed=True,
                blocking=False,
                message="未提供资金快照，可支配金额检查跳过（提交时由内核重检）。",
                detail={"snapshot_provided": False},
            )
        )
    else:
        fresh = age_seconds is not None and age_seconds <= SNAPSHOT_MAX_AGE_SECONDS
        checks.append(
            _check(
                "snapshot_freshness",
                "STALE_SNAPSHOT",
                passed=fresh,
                blocking=False,
                message=(
                    "快照在有效期内。" if fresh else "快照已变旧，提交前必须重新核验资金与额度。"
                ),
                detail={
                    "snapshot_provided": True,
                    "age_seconds": None if age_seconds is None else round(age_seconds, 1),
                    "max_age_seconds": SNAPSHOT_MAX_AGE_SECONDS,
                },
            )
        )

    reason_codes = [check["reason_code"] for check in checks if not check["passed"]]
    blocking_reason_codes = [check["reason_code"] for check in checks if check["blocking"]]
    warnings = [
        {
            "code": check["reason_code"],
            "message": check["message"],
            "detail": check["detail"],
        }
        for check in checks
        if not check["passed"]
    ]

    return {
        **base,
        "valid": not blocking_reason_codes,
        "reason_codes": reason_codes,
        "blocking_reason_codes": blocking_reason_codes,
        "checks": checks,
        "amount_minor": amount,
        "fee_minor": fee_minor,
        "total_cost_minor": amount + fee_minor,
        "budget_minor": budget_minor,
        "horizon_days": horizon,
        "profile": {
            "status": profile["status"],
            "usable": profile["usable"],
            "risk_level": profile.get("risk_level"),
            "expires_at": profile.get("expires_at"),
        },
        "product": terms.product_terms(product),
        "estimate": estimate,
        "warnings": warnings,
        "disclaimer": "适当性校验只判断是否满足硬约束；不预测收益，也不代表产品适合或未来表现。",
    }


__all__ = [
    "MIN_NAV_HISTORY",
    "SNAPSHOT_MAX_AGE_SECONDS",
    "VALIDATION_VERSION",
    "nav_history",
    "operation_args",
    "validate_domain",
]