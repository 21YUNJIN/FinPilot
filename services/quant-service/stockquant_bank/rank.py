"""Suitability-aware comparison ordering for bank wealth products.

Order of operations is the point of this module:

1. the bank's hard constraints run first and *exclude* products (risk level,
   horizon, liquidity, minimum amount) -- the model cannot talk its way past
   them;
2. only then do risk metrics order what is left, by the preference the user
   actually stated;
3. the same eligible set is ranked with the quant metrics removed, so the H4
   ablation ("did quant change the decision?") needs no second run.

No expected return is invented.  Historical returns appear only as labelled
sample statistics.

Scoring scales are absolute (not min-max over the current catalog) so that the
same product keeps the same score when the catalog changes; otherwise an
ablation comparison would silently depend on which products were sent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .canonical import METHOD_VERSION, SCHEMA_VERSION
from .errors import InvalidRequest
from .report import DISCLAIMERS, build_product_report

RISK_ORDER = ["R1", "R2", "R3", "R4", "R5"]

MODE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "stability": {"vol": 0.40, "drawdown": 0.30, "cvar": 0.20, "fee": 0.10},
    "balanced": {"vol": 0.25, "drawdown": 0.20, "cvar": 0.15, "fee": 0.20, "liquidity": 0.10, "term": 0.10},
    # "return" ranks by *historical risk-adjusted* return, not by expected
    # return: cash can win on stability mode, a bond fund usually wins here.
    "return": {"sharpe": 0.60, "cagr": 0.25, "fee": 0.15},
    "cost": {"fee": 0.50, "liquidity": 0.30, "term": 0.20},
}
BASELINE_WEIGHTS: Dict[str, float] = {"fee": 0.50, "liquidity": 0.30, "term": 0.20}

# Absolute reference scales for scoring only.  These are *not* user risk
# thresholds and never filter a product; they keep scores comparable across
# catalogs and across the A4 ablation.
SCORING_SCALES: Dict[str, Any] = {
    "vol_full_credit_below": 0.015,
    "vol_zero_credit_above": 0.300,
    "drawdown_full_credit_below": 0.02,
    "drawdown_zero_credit_above": 0.400,
    "cvar_full_credit_below": 0.001,
    "cvar_zero_credit_above": 0.030,
    "cagr_zero_credit_at": -0.100,
    "cagr_full_credit_at": 0.120,
    "sharpe_zero_credit_at": -1.000,
    "sharpe_full_credit_at": 2.000,
    "fee_bps_zero_credit_at": 200.0,
    "settlement_days_zero_credit_at": 10.0,
    "term_days_zero_credit_at": 1825.0,
}


def _risk_index(level: Any) -> Optional[int]:
    if isinstance(level, int) and 1 <= level <= 5:
        return level - 1
    text = str(level or "").strip().upper()
    if text in RISK_ORDER:
        return RISK_ORDER.index(text)
    if text.isdigit() and 1 <= int(text) <= 5:
        return int(text) - 1
    return None


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _descending(value: float, full_below: float, zero_above: float) -> float:
    """1.0 when ``value <= full_below``, 0.0 when ``value >= zero_above``.

    Works for negative figures by passing their absolute value.
    """
    if zero_above <= full_below:
        return 0.0
    return _clip01((zero_above - value) / (zero_above - full_below))


def _ascending(value: float, zero_at: float, full_at: float) -> float:
    if full_at <= zero_at:
        return 0.0
    return _clip01((value - zero_at) / (full_at - zero_at))


def factor_scores(report: Mapping[str, Any], product: Mapping[str, Any]) -> Dict[str, float]:
    metrics = report["metrics"]
    vol = metrics["annual_volatility"]["value"]
    dd = metrics["max_drawdown"]["value"]
    cvar = metrics["historical_cvar_95"]["value"]
    cagr = metrics["cagr"]["value"]
    scale = SCORING_SCALES

    def na_as(value: Optional[float], default: float = 0.0) -> float:
        return default if value is None else float(value)

    return {
        "vol": _descending(na_as(vol, scale["vol_zero_credit_above"]),
                           scale["vol_full_credit_below"], scale["vol_zero_credit_above"]),
        "drawdown": _descending(abs(na_as(dd, scale["drawdown_zero_credit_above"])),
                                scale["drawdown_full_credit_below"], scale["drawdown_zero_credit_above"]),
        "cvar": _descending(abs(na_as(cvar, scale["cvar_zero_credit_above"])),
                            scale["cvar_full_credit_below"], scale["cvar_zero_credit_above"]),
        "cagr": _ascending(na_as(cagr, scale["cagr_zero_credit_at"]),
                           scale["cagr_zero_credit_at"], scale["cagr_full_credit_at"]),
        "sharpe": _ascending(na_as(metrics["sharpe"]["value"], scale["sharpe_zero_credit_at"]),
                             scale["sharpe_zero_credit_at"], scale["sharpe_full_credit_at"]),
        "fee": _descending(float(product.get("total_fee_bps") or 0.0),
                           0.0, scale["fee_bps_zero_credit_at"]),
        "liquidity": _descending(float(product.get("settlement_days") or 0.0),
                                 0.0, scale["settlement_days_zero_credit_at"]),
        "term": _descending(float(product.get("term_days") or 0.0),
                            0.0, scale["term_days_zero_credit_at"]),
    }


def _weighted(factors: Mapping[str, float], weights: Mapping[str, float]) -> tuple:
    total = 0.0
    contributions: Dict[str, float] = {}
    for key, weight in weights.items():
        points = factors.get(key, 0.0) * weight * 100.0
        contributions[key] = round(points, 4)
        total += points
    return round(total, 4), contributions


def hard_filter_reasons(
    product: Mapping[str, Any],
    report: Mapping[str, Any],
    user: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    reasons: List[Dict[str, Any]] = []
    user_risk = _risk_index(user.get("risk_level"))
    product_risk = _risk_index(product.get("official_risk_level") or product.get("risk_level"))
    if user_risk is None:
        raise InvalidRequest("必须提供有效的用户风险等级 R1-R5", detail={"field": "user.risk_level"})
    if product_risk is None:
        reasons.append({"code": "PRODUCT_RISK_LEVEL_UNKNOWN", "message": "产品缺少官方风险等级，不能进入可申购比较"})
    elif product_risk > user_risk:
        reasons.append({
            "code": "RISK_LEVEL_EXCEEDS_USER",
            "message": f"产品风险 {RISK_ORDER[product_risk]} 高于用户承受等级 {RISK_ORDER[user_risk]}",
        })

    horizon = user.get("horizon_days")
    term_days = product.get("term_days")
    if horizon is not None and term_days is not None and float(term_days) > float(horizon):
        reasons.append({
            "code": "TERM_EXCEEDS_HORIZON",
            "message": f"产品期限 {term_days} 天超过用款时间 {horizon} 天",
        })

    need = user.get("liquidity_need_days")
    settle = product.get("settlement_days")
    if need is not None and settle is not None and float(settle) > float(need):
        reasons.append({
            "code": "LIQUIDITY_NOT_SETTLED_IN_TIME",
            "message": f"赎回到账约 {settle} 天，晚于用户可等待的 {need} 天",
        })

    budget = user.get("budget_minor")
    minimum = product.get("min_amount_minor")
    if budget is not None and minimum is not None and int(minimum) > int(budget):
        reasons.append({
            "code": "BELOW_MIN_INVESTMENT",
            "message": f"起购金额 {minimum} 分高于预算 {budget} 分",
        })
    return reasons


def rank_products(
    products: Sequence[Mapping[str, Any]],
    *,
    user: Mapping[str, Any],
    as_of: str,
    risk_free_rate: float = 0.015,
    include_quant: bool = True,
) -> Dict[str, Any]:
    if not products:
        raise InvalidRequest("products 不能为空", detail={"field": "products"})
    mode = str(user.get("preference_mode") or "balanced")
    if mode not in MODE_WEIGHTS:
        raise InvalidRequest(
            "preference_mode 必须是 stability/balanced/return/cost 之一",
            detail={"field": "user.preference_mode"},
        )
    by_id = {str(p.get("product_id") or p.get("id")): p for p in products}

    reports: Dict[str, Dict[str, Any]] = {}
    excluded: List[Dict[str, Any]] = []
    product_errors: List[Dict[str, Any]] = []
    for product in products:
        pid = str(product.get("product_id") or product.get("id"))
        try:
            report = build_product_report(product, as_of=as_of, risk_free_rate=risk_free_rate)
        except Exception as exc:
            product_errors.append({"product_id": pid, "message": str(exc)[:200]})
            continue
        reasons = hard_filter_reasons(product, report, user)
        if reasons:
            excluded.append({"product_id": pid, "reasons": reasons})
        else:
            reports[pid] = report

    eligible_ids = [pid for pid in by_id if pid in reports]
    quant_ids = [pid for pid in eligible_ids if reports[pid]["usable_for_ranking"]]

    weights = MODE_WEIGHTS[mode]

    def row_for(pid: str, score: float, contributions: Dict[str, float], factors: Dict[str, float]) -> Dict[str, Any]:
        report = reports[pid]
        product = by_id[pid]
        return {
            "product_id": pid,
            "product_name": report["product_name"],
            "score": score,
            "score_contributions": contributions,
            "score_factors": {key: round(value, 6) for key, value in factors.items()},
            "official_risk_level": report["official_risk_level"],
            "observed_market_risk_band": report["observed_market_risk_band"],
            "total_fee_bps": product.get("total_fee_bps"),
            "term_days": product.get("term_days"),
            "settlement_days": product.get("settlement_days"),
            "data_window": report["data_window"],
            "metrics_brief": {
                "annual_volatility": report["metrics"]["annual_volatility"]["value"],
                "max_drawdown": report["metrics"]["max_drawdown"]["value"],
                "historical_cvar_95": report["metrics"]["historical_cvar_95"]["value"],
                "cagr": report["metrics"]["cagr"]["value"],
                "sample_count": report["metrics"]["annual_volatility"]["sample_count"],
            },
            "warnings": report["warnings"],
        }

    def order(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows.sort(key=lambda row: (
            -row["score"],
            float(by_id[row["product_id"]].get("total_fee_bps") or 0.0),
            row["product_id"],
        ))
        for idx, row in enumerate(rows, start=1):
            row["rank"] = idx
        return rows

    ranked: List[Dict[str, Any]] = []
    for pid in quant_ids:
        factors = factor_scores(reports[pid], by_id[pid])
        score, contributions = _weighted(factors, weights)
        ranked.append(row_for(pid, score, contributions, factors))
    order(ranked)
    ranked_ids = {row["product_id"] for row in ranked}
    not_ranked = [
        {
            "product_id": pid,
            "code": "INSUFFICIENT_NAV_HISTORY",
            "message": "通过硬约束，但净值样本不足 60 个有效收益，未参与量化排序",
        }
        for pid in eligible_ids if pid not in ranked_ids
    ]

    baseline: List[Dict[str, Any]] = []
    for pid in eligible_ids:
        factors = factor_scores(reports[pid], by_id[pid])
        score, contributions = _weighted(factors, BASELINE_WEIGHTS)
        baseline.append({
            "product_id": pid,
            "score": score,
            "score_contributions": contributions,
        })
    order(baseline)

    baseline_rank = {row["product_id"]: row["rank"] for row in baseline}
    changes = [
        {
            "product_id": row["product_id"],
            "quant_rank": row["rank"],
            "baseline_rank": baseline_rank[row["product_id"]],
            "delta": baseline_rank[row["product_id"]] - row["rank"],
        }
        for row in ranked
    ]
    changes.sort(key=lambda row: (-abs(row["delta"]), row["product_id"]))

    if not include_quant:
        # A4 deployment: quant metrics removed, so the served order *is* the
        # cost/liquidity/term order -- including products whose history is too
        # short to score on risk.  Field names stay identical so FinPilot's
        # rendering code does not branch.
        a4: List[Dict[str, Any]] = []
        for pid in eligible_ids:
            factors = factor_scores(reports[pid], by_id[pid])
            score, contributions = _weighted(factors, BASELINE_WEIGHTS)
            item = row_for(pid, score, contributions, {})
            item["quant_disabled"] = True
            a4.append(item)
        ranked = order(a4)
        changes = []

    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "as_of": as_of,
        "include_quant": include_quant,
        "user": {
            "risk_level": user.get("risk_level"),
            "horizon_days": user.get("horizon_days"),
            "liquidity_need_days": user.get("liquidity_need_days"),
            "budget_minor": user.get("budget_minor"),
            "preference_mode": mode,
        },
        "weights_used": weights,
        "scoring_scales": SCORING_SCALES,
        "eligible": len(eligible_ids),
        "excluded": excluded,
        "not_ranked": not_ranked,
        "product_errors": product_errors,
        "ranked": ranked,
        "baseline_ranked": baseline,
        "rank_changes": changes,
        "rank_basis": (
            "先按产品官方风险等级、期限、赎回到账、起购金额做硬过滤；"
            "再以用户明确偏好对风险指标、费用和流动性加权排序。"
            "分数是相对比较值，不是收益预测。"
        ),
        "disclaimers": DISCLAIMERS + [
            "排序变化只说明指标影响了相对顺序，不代表未来业绩。",
            "模型不能创建新阈值：用户未表达偏好时使用 balanced 默认权重，界面须披露。",
            "量化排序不覆盖未通过硬约束的产品，硬约束结果不可由分数推翻。",
        ],
    }