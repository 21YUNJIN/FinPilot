"""产品比较与排序（V5 第 11.1 节 S3-02、QUANT-03／04、T09／T46）。

顺序不可颠倒：**先硬约束过滤，再按用户明确偏好排序**。

- 硬约束：风险等级（用户测评或用户自己给出的上限）、期限与用款时间、起购与含费预算、
  用户显式流动性下限。被过滤的产品仍会返回，但 ``eligible: false`` 并给出原因码，
  便于解释"为什么这个产品没进可执行方案"。
- 排序：严格按用户给出的偏好顺序做字典序排序；用户没有给偏好时使用**公开的默认顺序**
  （风险 → 费率 → 流动性 → 收益），并在 ``reasons`` 里标注 ``DEFAULT_PRIORITY_USED``。
  绝不偷偷创造风险阈值：风险上限只来自测评结果或用户显式字段。
- 历史指标：样本不足时 ``history_metric`` 为 ``null`` + ``not_applicable``，
  不填零、不编造；输出里禁止出现收益保证措辞。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...models import ProductNav, WealthProduct
from ...money import format_minor
from ...timeutil import business_day, utcnow
from . import assessment, terms
from .suitability import nav_history

COMPARE_VERSION = "wealth-compare-2026.10"
METHOD_VERSION = "wealth-compare-2026.10"

#: 收益类历史指标的最小样本（与 StockQuant 适配器一致：≥60 个有效日收益）。
MIN_HISTORY_POINTS = 60
ANNUALIZATION_FACTOR = 252

#: 用户偏好别名 → 内部键。
PREFERENCE_ALIASES: dict[str, str] = {
    "yield": "yield",
    "return": "yield",
    "returns": "yield",
    "收益": "yield",
    "收益率": "yield",
    "liquidity": "liquidity",
    "流动性": "liquidity",
    "fee": "fee",
    "cost": "fee",
    "费率": "fee",
    "费用": "fee",
    "risk": "risk",
    "风险": "risk",
}
PREFERENCE_LABELS: dict[str, str] = {"yield": "收益", "liquidity": "流动性", "fee": "费率", "risk": "风险"}

#: 没有用户偏好时的公开默认顺序（在输出中明确标注）。
DEFAULT_PRIORITY: tuple[str, ...] = ("risk", "fee", "liquidity", "yield")

#: 禁止出现在输出中的收益／到账保证措辞。
FORBIDDEN_GUARANTEE_TOKENS: tuple[str, ...] = (
    "保证收益",
    "收益保证",
    "保本",
    "保收益",
    "稳赚",
    "无风险",
    "承诺收益",
    "确保收益",
    "保底收益",
)

DISCLAIMER = (
    "历史净值与条款只用于比较，不代表未来收益，也不构成任何形式的收益担保；"
    "排序只反映用户明确偏好与硬约束，不代表产品适合。"
)


def normalize_preferences(preferences: Any) -> dict[str, Any]:
    """把用户偏好统一成 ``{"priority", "risk_level_max", "min_liquidity_days", ...}``。"""
    if preferences is None:
        raw: dict[str, Any] = {}
    elif isinstance(preferences, Mapping):
        raw = dict(preferences)
    elif isinstance(preferences, (list, tuple, set)):
        raw = {"priority": list(preferences)}
    else:
        raise FinPilotError("PARAM_INVALID", "preferences 必须是对象或偏好列表。")

    raw_priority = raw.get("priority", raw.get("preferences", []))
    if isinstance(raw_priority, str):
        raw_priority = [raw_priority]
    if raw_priority is None:
        raw_priority = []
    if not isinstance(raw_priority, Sequence):
        raise FinPilotError("PARAM_INVALID", "preferences.priority 必须是列表。")

    priority: list[str] = []
    for item in raw_priority:
        key = PREFERENCE_ALIASES.get(str(item).strip().lower())
        if key is None:
            raise FinPilotError(
                "PARAM_INVALID",
                "无法识别的偏好项。",
                details={
                    "reason_codes": ["unknown_preference"],
                    "value": str(item),
                    "allowed": sorted(set(PREFERENCE_ALIASES.values())),
                },
            )
        if key not in priority:
            priority.append(key)

    risk_level_max = raw.get("risk_level_max", raw.get("max_risk_level"))
    if risk_level_max is not None:
        risk_level_max = str(risk_level_max).strip().upper()
        if risk_level_max not in assessment.RISK_RANK:
            raise FinPilotError(
                "PARAM_INVALID", "risk_level_max 必须是 R1—R5。",
                details={"reason_codes": ["bad_risk_level"], "value": risk_level_max},
            )

    min_liquidity_days = raw.get("min_liquidity_days")
    if min_liquidity_days is not None:
        try:
            min_liquidity_days = int(min_liquidity_days)
        except (TypeError, ValueError) as exc:
            raise FinPilotError("PARAM_INVALID", "min_liquidity_days 必须是整数天。") from exc
        if min_liquidity_days < 0:
            raise FinPilotError("PARAM_INVALID", "min_liquidity_days 不能为负。")

    product_ids = raw.get("product_ids")
    if product_ids is not None:
        if isinstance(product_ids, str):
            product_ids = [product_ids]
        product_ids = [str(pid) for pid in product_ids]

    return {
        "priority": tuple(priority) if priority else DEFAULT_PRIORITY,
        "explicit_priority": bool(priority),
        "risk_level_max": risk_level_max,
        "min_liquidity_days": min_liquidity_days,
        "product_ids": product_ids,
        "raw": {key: value for key, value in raw.items() if key not in {"priority", "preferences"}},
    }


def _max_principal_within_budget(product: Any, budget_minor: int) -> int:
    """预算为**含费总额**上限时，可申购的最大本金（费用随本金单调递增）。"""
    lo, hi = 0, max(0, int(budget_minor))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid + terms.product_fee_minor(product, mid) <= budget_minor:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _annualized_return_bps(navs: Sequence[ProductNav]) -> dict[str, Any]:
    points = len(navs)
    window = (
        {
            "start": navs[0].as_of.isoformat(),
            "end": navs[-1].as_of.isoformat(),
            "points": points,
            "observation": "全部可用净值点",
        }
        if points
        else None
    )
    metric: dict[str, Any] = {
        "value": None,
        "unit": "bps_annualized",
        "method": "compound_annualized_from_nav_e6",
        "formula": "((nav_last / nav_first) ** (252 / (points - 1)) - 1) * 10000",
        "sample_count": points,
        "required_sample_count": MIN_HISTORY_POINTS,
        "window": window,
        "status": "not_applicable",
        "reason_code": "insufficient_history",
        "reason": f"有效净值点 {points} 个，少于 {MIN_HISTORY_POINTS} 个，不给正式收益比较值。",
    }
    if points < MIN_HISTORY_POINTS:
        return metric
    first, last = int(navs[0].nav_e6), int(navs[-1].nav_e6)
    if first <= 0 or last <= 0:
        metric["reason_code"] = "invalid_nav"
        metric["reason"] = "净值不是正数，无法计算。"
        return metric
    periods = points - 1
    annualized = (last / first) ** (ANNUALIZATION_FACTOR / periods) - 1.0
    metric.update(
        {
            "value": round(annualized * 10000, 2),
            "status": "ok",
            "reason_code": None,
            "reason": None,
            "nav_first_e6": first,
            "nav_last_e6": last,
        }
    )
    return metric


def _history_view(navs: Sequence[ProductNav], include_quant: bool) -> dict[str, Any]:
    points = len(navs)
    window = (
        {"start": navs[0].as_of.isoformat(), "end": navs[-1].as_of.isoformat(), "points": points}
        if points
        else None
    )
    if not include_quant:
        return {
            "applicable": points > 0,
            "nav_points": points,
            "window": window,
            "as_of": navs[-1].as_of.isoformat() if points else None,
            "history_metric": {
                "value": None,
                "unit": "bps_annualized",
                "status": "not_applicable",
                "reason_code": "quant_metrics_removed",
                "reason": "本对照运行移除了量化指标（H4 消融 A4），历史衍生指标不参与排序。",
                "sample_count": points,
            },
            "note": "量化指标已关闭；流动性、费率、风险与条款仍按原逻辑参与排序。",
        }
    metric = _annualized_return_bps(navs)
    return {
        "applicable": points > 0,
        "nav_points": points,
        "window": metric["window"],
        "as_of": navs[-1].as_of.isoformat() if points else None,
        "history_metric": metric,
        "note": "历史指标按固定方法与固定数据计算；样本不足时返回 null 与 not_applicable。",
    }


def _preference_value(item: Mapping[str, Any], key: str) -> float | None:
    if key == "yield":
        value = item["history"]["history_metric"]["value"]
        return None if value is None else -float(value)  # 收益越高越靠前
    if key == "liquidity":
        return float(item["required_holding_days"])
    if key == "fee":
        value = item["effective_fee_bps"]
        return None if value is None else float(value)
    if key == "risk":
        return float(assessment.RISK_RANK.get(item["issuer_risk_level"], 9))
    raise FinPilotError("SYSTEM_ERROR", f"未知偏好键 {key}")


def _preference_reason(item: Mapping[str, Any], key: str) -> dict[str, Any]:
    label = PREFERENCE_LABELS[key]
    if key == "yield":
        metric = item["history"]["history_metric"]
        if metric["value"] is None:
            return {
                "code": "PREFERENCE_YIELD_UNAVAILABLE",
                "detail": f"{label}偏好：{metric['reason'] or '历史指标不适用'}，该项不参与排序（not_applicable）。",
            }
        return {
            "code": "PREFERENCE_YIELD_VALUE",
            "detail": f"{label}偏好：净值窗口年化 {metric['value'] / 100:+.2f}%（{metric['sample_count']} 个净值点）。",
        }
    if key == "liquidity":
        return {
            "code": "PREFERENCE_LIQUIDITY_VALUE",
            "detail": f"{label}偏好：期限 + 到账约 {item['required_holding_days']} 天。",
        }
    if key == "fee":
        return {
            "code": "PREFERENCE_FEE_VALUE",
            "detail": (
                f"{label}偏好：按预算内最大本金 {format_minor(item['planned_amount_minor'])} 元计，"
                f"费用 {format_minor(item['estimated_fee_minor'])} 元（实际费率 {item['effective_fee_bps'] / 100:.4f}%）。"
            ),
        }
    return {
        "code": "PREFERENCE_RISK_VALUE",
        "detail": f"{label}偏好：产品风险等级 {item['issuer_risk_level']}。",
    }


def rank_products(
    session: Session,
    ctx: Any = None,
    *,
    budget_minor: int,
    horizon_days: int,
    preferences: Any = None,
    include_quant: bool = True,
) -> list[dict[str, Any]]:
    """硬约束过滤 + 偏好排序。``include_quant=False`` 用于 H4 消融对照。"""
    if ctx is not None:
        ctx.require_customer()

    try:
        budget = int(budget_minor)
    except (TypeError, ValueError) as exc:
        raise FinPilotError("PARAM_INVALID", "budget_minor 必须是整数分。") from exc
    if budget <= 0:
        raise FinPilotError("PARAM_INVALID", "budget_minor 必须大于 0。", details={"reason_codes": ["bad_budget"]})
    try:
        horizon = int(horizon_days)
    except (TypeError, ValueError) as exc:
        raise FinPilotError("PARAM_INVALID", "horizon_days 必须是整数天。") from exc
    if horizon <= 0:
        raise FinPilotError("PARAM_INVALID", "horizon_days 必须大于 0。", details={"reason_codes": ["bad_horizon"]})

    prefs = normalize_preferences(preferences)
    today: date = business_day(utcnow())

    profile = (
        assessment.current_profile(session, ctx)
        if ctx is not None
        else {"status": "none", "usable": False, "risk_level": None, "questionnaire_version": None}
    )
    profile_rank = assessment.RISK_RANK.get(str(profile.get("risk_level")), 0) if profile["usable"] else 0
    user_limit_rank = assessment.RISK_RANK.get(prefs["risk_level_max"], 0) if prefs["risk_level_max"] else 0

    stmt = select(WealthProduct).where(WealthProduct.is_active.is_(True))
    if prefs["product_ids"]:
        stmt = stmt.where(WealthProduct.product_id.in_(prefs["product_ids"]))
    products = list(session.execute(stmt.order_by(WealthProduct.product_id.asc())).scalars())

    from ...config import get_settings
    from ...adapters.stockquant.remote import fetch_reports, history_view
    backend = get_settings().quant_backend
    if backend not in {"remote", "local_reference"}:
        raise FinPilotError("PARAM_INVALID", "未知量化后端，拒绝默默回退。")
    remote_reports = fetch_reports(session, products, today) if include_quant and backend == "remote" else {}
    items: list[dict[str, Any]] = []
    for product in products:
        terms_view = terms.product_terms(product)
        navs = nav_history(session, product.product_id, as_of=today)
        history = (history_view(remote_reports[product.product_id], navs)
                   if include_quant and backend == "remote" else _history_view(navs, include_quant))

        planned = _max_principal_within_budget(product, budget)
        estimated_fee = terms.product_fee_minor(product, planned)
        effective_fee_bps = round(estimated_fee / planned * 10_000, 4) if planned > 0 else None
        required_days = int(product.term_days or 0) + terms.settlement_days(product)
        risk_level = str(product.issuer_risk_level or "R5")
        product_rank = assessment.RISK_RANK.get(risk_level, 9)

        exclusions: list[dict[str, Any]] = []
        if prefs["product_ids"] and product.product_id not in prefs["product_ids"]:
            exclusions.append({"code": "NOT_REQUESTED", "detail": "不在本次比较的产品范围内。"})
        if user_limit_rank and product_rank > user_limit_rank:
            exclusions.append(
                {
                    "code": "RISK_ABOVE_USER_LIMIT",
                    "detail": f"产品风险 {risk_level} 超过用户显式上限 {prefs['risk_level_max']}。",
                }
            )
        if profile_rank and product_rank > profile_rank:
            exclusions.append(
                {
                    "code": "RISK_MISMATCH",
                    "detail": f"产品风险 {risk_level} 高于测评等级 {profile['risk_level']}。",
                }
            )
        min_cost = int(product.min_minor or 0) + terms.product_fee_minor(product, int(product.min_minor or 0))
        if min_cost > budget:
            exclusions.append(
                {
                    "code": "BUDGET_EXCEEDED",
                    "detail": (
                        f"含费最低成本 {format_minor(min_cost)} 元超过预算 {format_minor(budget)} 元"
                        f"（起购 {format_minor(int(product.min_minor or 0))} 元 + 费用）；"
                        f"预算内最大可申购本金 {format_minor(planned)} 元。"
                    ),
                    "sub_reason": "fee_over_budget" if int(product.min_minor or 0) <= budget else "below_min_amount",
                }
            )
        if required_days > horizon:
            exclusions.append(
                {
                    "code": "TERM_CONFLICT",
                    "detail": f"期限 {int(product.term_days or 0)} 天 + 到账 {terms.settlement_days(product)} 天"
                    f"（约 {required_days} 天）晚于用款时间 {horizon} 天。",
                }
            )
        if prefs["min_liquidity_days"] is not None and required_days > prefs["min_liquidity_days"]:
            exclusions.append(
                {
                    "code": "LIQUIDITY_CONSTRAINT",
                    "detail": f"约 {required_days} 天超过用户要求的流动性上限 {prefs['min_liquidity_days']} 天。",
                }
            )

        eligible = not exclusions
        item: dict[str, Any] = {
            "product_id": product.product_id,
            "name": product.name,
            "issuer": product.issuer,
            "product_type": product.product_type,
            "issuer_risk_level": risk_level,
            "eligible": eligible,
            "rank": None,
            "exclusion_reason_codes": [exclusion["code"] for exclusion in exclusions],
            "min_minor": int(product.min_minor or 0),
            "budget_minor": budget,
            "planned_amount_minor": planned,
            "estimated_fee_minor": estimated_fee,
            "effective_fee_bps": effective_fee_bps,
            "estimated_total_cost_minor": planned + estimated_fee,
            "min_total_cost_minor": min_cost,
            "term_days": int(product.term_days or 0),
            "settlement_days": terms.settlement_days(product),
            "required_holding_days": required_days,
            "open_days": terms_view["open_days"],
            "open_day_rule": terms_view["open_day_rule"],
            "cutoff_time": terms_view["cutoff_time"],
            "liquidity_terms": terms_view["liquidity_terms"],
            "cancel_terms": terms_view["cancel_terms"],
            "cooling_off_days": terms_view["cooling_off_days"],
            "history": history,
            "fee_bps": product.fee_bps,
            "fee_fixed_minor": product.fee_fixed_minor,
            "is_active": product.is_active,
            "assessment_required": True,
            "nav_e6": navs[-1].nav_e6 if navs else None,
            "nav_as_of_date": navs[-1].as_of.isoformat() if navs else None,
            "nav_is_na": not bool(navs),
            "history_ref": product.history_ref,
            "quant_risk": remote_reports.get(product.product_id),
            "reasons": [],
            "amount_basis": "largest_principal_within_budget",
            "amount_basis_formula": "planned_amount = max{p : p + fee(p) <= budget_minor}",
            "data_version": {
                "compare_version": COMPARE_VERSION,
                "method_version": ("sq-wealth-risk/1.0.0" if backend == "remote" and include_quant else METHOD_VERSION),
                "quant_backend": backend if include_quant else "ablation_no_quant",
                "terms_version": product.terms_version,
                "nav_terms_version": navs[-1].terms_version if navs else None,
                "nav_points": len(navs),
                "quant_metrics_included": include_quant,
                "as_of": today.isoformat(),
            },
            "disclaimer": DISCLAIMER,
        }

        reasons: list[dict[str, Any]] = []
        if eligible:
            reasons.append(
                {
                    "code": "HARD_CONSTRAINTS_PASSED",
                    "detail": "风险、期限、起购与含费预算硬约束全部通过。",
                }
            )
        else:
            for exclusion in exclusions:
                reasons.append({"code": f"EXCLUDED_{exclusion['code']}", "detail": exclusion["detail"]})
        if not profile["usable"]:
            reasons.append(
                {
                    "code": "ADVISORY_ONLY",
                    "detail": f"测评状态为 {profile['status']}：只能做一般信息比较，申购前必须完成有效测评。",
                }
            )
        if not prefs["explicit_priority"]:
            reasons.append(
                {
                    "code": "DEFAULT_PRIORITY_USED",
                    "detail": "用户未给出明确偏好，使用公开默认顺序：风险 → 费率 → 流动性 → 收益。",
                }
            )
        else:
            reasons.append(
                {
                    "code": "PREFERENCE_ORDER_APPLIED",
                    "detail": "按用户明确偏好顺序排序："
                    + " → ".join(PREFERENCE_LABELS[key] for key in prefs["priority"])
                    + "。",
                }
            )
        for key in prefs["priority"]:
            reasons.append(_preference_reason(item, key))
        if not include_quant:
            reasons.append(
                {
                    "code": "QUANT_METRICS_REMOVED_FOR_ABLATION",
                    "detail": "本对照运行按 A4 移除量化指标，历史衍生指标不参与排序。",
                }
            )
        item["reasons"] = reasons
        items.append(item)

    eligible_items = [item for item in items if item["eligible"]]
    excluded_items = [item for item in items if not item["eligible"]]

    priority = prefs["priority"]

    def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        keys: list[Any] = []
        for key in priority:
            value = _preference_value(item, key)
            keys.append((1, 0.0) if value is None else (0, value))
        keys.append((0, item["product_id"]))
        return tuple(keys)

    eligible_items.sort(key=sort_key)
    for index, item in enumerate(eligible_items, start=1):
        item["rank"] = index
    excluded_items.sort(key=lambda item: item["product_id"])
    return eligible_items + excluded_items


def compare_products(
    session: Session,
    ctx: Any,
    *,
    budget_minor: int,
    horizon_days: int,
    preferences: Any = None,
) -> list[dict[str, Any]]:
    """只读产品比较：硬约束过滤后再按用户明确偏好排序。"""
    return rank_products(
        session,
        ctx,
        budget_minor=budget_minor,
        horizon_days=horizon_days,
        preferences=preferences,
        include_quant=True,
    )


def ensure_no_guarantee_wording(payload: Any) -> list[str]:
    """扫描输出里的收益保证措辞，返回命中的词（空列表表示合规）。"""
    text = str(payload)
    return [token for token in FORBIDDEN_GUARANTEE_TOKENS if token in text]


__all__ = [
    "ANNUALIZATION_FACTOR",
    "COMPARE_VERSION",
    "DEFAULT_PRIORITY",
    "DISCLAIMER",
    "FORBIDDEN_GUARANTEE_TOKENS",
    "METHOD_VERSION",
    "MIN_HISTORY_POINTS",
    "PREFERENCE_ALIASES",
    "compare_products",
    "ensure_no_guarantee_wording",
    "normalize_preferences",
    "rank_products",
]