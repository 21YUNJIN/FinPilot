"""StockQuant 只读受限适配器（V5 第 15 节 QUANT-01～04、T46／T50）。

边界（写死在代码里，不靠调用方自觉）：

- **只读**：只查 ``ProductNav`` 与 ``WealthProduct``，不写任何表、不下单、
  不暴露交易所凭证、不执行任意脚本。
- **数据来源唯一**：日收益取 ``ProductNav.daily_return_bps``；不同资产频率不混算，
  组合序列按观测日**交集**对齐。
- **不足即 NA**：有效日收益 < 60 不算年化波动；< 252 不报告 95% 历史 CVaR 的正式
  比较值（返回 ``null`` + 原因），不填零、不外推。
- **不替代产品风险等级**：存款类／非净值产品返回 ``applicable: false``，不用证券
  回测冒充真实产品风险等级，也不从行情猜流动性或条款。
- **可复现**：同一数据、同一方法版本得到同一 ``source_hash`` 与同一指标值；
  ``computed_at`` 是唯一随时间变化的字段。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...ids import canonical_hash
from ...models import ProductNav, WealthProduct
from ...timeutil import SHANGHAI, business_day, format_iso, parse_iso, to_shanghai, utcnow

METHOD_VERSION = "sq-risk-2026.10"
ADAPTER_ID = "finpilot-local-reference"
ADAPTER_VERSION = "1.0.0"

#: 年化波动的最小有效收益样本。
MIN_VOL_POINTS = 60
#: 历史 95% CVaR 的最小有效收益样本（少于该值不输出正式比较值）。
MIN_CVAR_POINTS = 252
#: 最大回撤的最小有效收益样本（低于该值只给窗口说明，不给数值）。
MIN_DRAWDOWN_POINTS = 2
#: 明确写进方法卡的年化交易日因子。
ANNUALIZATION_FACTOR = 252
CVAR_CONFIDENCE = 0.95
#: 收益频率判断：中位间隔超过该天数即视为非日频，混算前必须显式处理。
DAILY_FREQUENCY_MAX_GAP_DAYS = 5

#: 存款类／非净值产品：不使用证券回测代替真实产品风险等级。
NON_NAV_PRODUCT_TYPES = frozenset(
    {
        "deposit",
        "time_deposit",
        "demand_deposit",
        "notice_deposit",
        "structured_deposit",
        "savings",
        "savings_plan",
        "insurance",
        "insurance_product",
        "guaranteed",
        "principal_protected",
    }
)
NON_NAV_TYPE_TOKENS: tuple[str, ...] = ("deposit", "insurance", "存款", "保险", "保本")

DISCLAIMER = (
    "本报告是只读的历史统计，不代表未来收益，也不构成任何形式的收益担保；"
    "样本估计不能替代产品风险等级、适当性评估与条款事实。"
)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalize_as_of(value: Any) -> date:
    if value is None:
        return business_day(utcnow())
    if isinstance(value, datetime):
        return to_shanghai(value).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return parse_iso(text).astimezone(SHANGHAI).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FinPilotError(
            "PARAM_INVALID",
            "as_of 必须是 date 或带时区的 ISO 8601 时间。",
            details={"reason_codes": ["bad_as_of"], "value": text},
        ) from exc


def _normalize_weights(asset_ids: Sequence[str], weights: Any) -> dict[str, float]:
    if weights is None:
        share = 1.0 / len(asset_ids)
        normalized = {asset_id: share for asset_id in asset_ids}
    elif isinstance(weights, Mapping):
        unknown = sorted(set(weights) - set(asset_ids))
        if unknown:
            raise FinPilotError(
                "PARAM_INVALID",
                "weights 含未在 asset_ids 中登记的资产。",
                details={"reason_codes": ["unknown_weight_asset"], "assets": unknown},
            )
        normalized = {asset_id: float(weights.get(asset_id, 0.0)) for asset_id in asset_ids}
    elif isinstance(weights, Sequence) and not isinstance(weights, (str, bytes)):
        if len(weights) != len(asset_ids):
            raise FinPilotError(
                "PARAM_INVALID",
                "weights 长度必须与 asset_ids 一致。",
                details={
                    "reason_codes": ["weight_length_mismatch"],
                    "asset_count": len(asset_ids),
                    "weight_count": len(weights),
                },
            )
        normalized = {asset_id: float(weight) for asset_id, weight in zip(asset_ids, weights)}
    else:
        raise FinPilotError("PARAM_INVALID", "weights 必须是列表或资产到权重的映射。")

    for asset_id, weight in normalized.items():
        if not math.isfinite(weight) or weight < 0:
            raise FinPilotError(
                "PARAM_INVALID",
                "权重必须是非负有限数。",
                details={"reason_codes": ["bad_weight"], "asset_id": asset_id, "weight": weight},
            )
    total = sum(normalized.values())
    if abs(total - 1.0) > 1e-6:
        raise FinPilotError(
            "PARAM_INVALID",
            "权重之和必须为 1。",
            details={"reason_codes": ["weights_not_normalized"], "total_weight": round(total, 8)},
        )
    return normalized


def _is_non_nav(product: WealthProduct | None) -> tuple[bool, str | None]:
    if product is None:
        return False, None
    product_type = str(product.product_type or "").strip().lower()
    if product_type in NON_NAV_PRODUCT_TYPES:
        return True, f"产品类型 {product_type} 属于存款类／非净值产品"
    for token in NON_NAV_TYPE_TOKENS:
        if token in product_type:
            return True, f"产品类型 {product_type} 命中非净值特征「{token}」"
    meta = product.meta if isinstance(product.meta, Mapping) else {}
    if meta.get("nav_based") is False:
        return True, "产品元数据声明 nav_based=false"
    return False, None


def _window(series: Sequence[tuple[date, float]]) -> dict[str, Any] | None:
    if not series:
        return None
    gaps = [(series[i][0] - series[i - 1][0]).days for i in range(1, len(series))]
    median_gap = statistics.median(gaps) if gaps else None
    return {
        "start": series[0][0].isoformat(),
        "end": series[-1][0].isoformat(),
        "points": len(series),
        "observation": "全部观察窗口（as_of 及之前的所有有效日收益）",
        "median_gap_days": median_gap,
    }


def _na_metric(
    unit: str,
    *,
    method: str,
    reason_code: str,
    reason: str,
    sample_count: int,
    required_sample_count: int | None = None,
    window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "value": None,
        "unit": unit,
        "method": method,
        "status": "not_applicable",
        "reason_code": reason_code,
        "reason": reason,
        "sample_count": sample_count,
        "required_sample_count": required_sample_count,
        "window": dict(window) if window else None,
    }


def annualized_volatility(series: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """年化波动：日收益样本标准差 × √252（≥60 个有效收益）。"""
    window = _window(series)
    count = len(series)
    method = "sample_stdev_daily_return_bps * sqrt(252)"
    if count < MIN_VOL_POINTS:
        return _na_metric(
            "bps_annualized",
            method=method,
            reason_code="insufficient_history_for_volatility",
            reason=f"有效日收益 {count} 个，少于 {MIN_VOL_POINTS} 个，不输出年化波动。",
            sample_count=count,
            required_sample_count=MIN_VOL_POINTS,
            window=window,
        )
    stdev = statistics.stdev([value for _day, value in series])
    return {
        "value": round(stdev * math.sqrt(ANNUALIZATION_FACTOR), 4),
        "unit": "bps_annualized",
        "method": method,
        "status": "ok",
        "reason_code": None,
        "reason": None,
        "sample_count": count,
        "required_sample_count": MIN_VOL_POINTS,
        "annualization_factor": ANNUALIZATION_FACTOR,
        "daily_stdev_bps": round(stdev, 6),
        "window": window,
    }


def max_drawdown(series: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """最大回撤：按日收益复利累计净值的峰谷差（负值表示回撤）。"""
    window = _window(series)
    count = len(series)
    method = "min(index_t / running_peak - 1), index_t = Π(1 + daily_return_bps/10000)"
    if count < MIN_DRAWDOWN_POINTS:
        return _na_metric(
            "bps",
            method=method,
            reason_code="insufficient_history_for_drawdown",
            reason=f"有效日收益 {count} 个，不足以计算最大回撤。",
            sample_count=count,
            required_sample_count=MIN_DRAWDOWN_POINTS,
            window=window,
        )
    index = 1.0
    peak = 1.0
    worst = 0.0
    for _day, value in series:
        index *= 1.0 + value / 10_000.0
        peak = max(peak, index)
        worst = min(worst, index / peak - 1.0)
    return {
        "value": round(worst * 10_000.0, 4),
        "unit": "bps",
        "sign_convention": "负值表示回撤（-1900 bps = 回撤 19%）",
        "magnitude_bps": round(abs(worst) * 10_000.0, 4),
        "method": method,
        "status": "ok",
        "reason_code": None,
        "reason": None,
        "sample_count": count,
        "window": window,
        "note": "最大回撤在整个观察窗口内计算；样本内回撤不是未来损失上限。",
    }


def historical_cvar(series: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """95% 历史 CVaR：最差 5% 日收益的均值（< 252 个有效收益返回 null）。"""
    window = _window(series)
    count = len(series)
    method = "mean(worst max(1, floor(n * 0.05)) daily returns)"
    if count < MIN_CVAR_POINTS:
        return _na_metric(
            "bps",
            method=method,
            reason_code="insufficient_history_for_cvar",
            reason=(
                f"有效日收益 {count} 个，少于 {MIN_CVAR_POINTS} 个："
                "按项目方法约定不报告历史 95% CVaR 的正式比较值。"
            ),
            sample_count=count,
            required_sample_count=MIN_CVAR_POINTS,
            window=window,
        )
    tail_count = max(1, int(count * (1.0 - CVAR_CONFIDENCE)))
    ordered = sorted(value for _day, value in series)
    tail = ordered[:tail_count]
    return {
        "value": round(statistics.fmean(tail), 4),
        "unit": "bps",
        "method": method,
        "confidence": CVAR_CONFIDENCE,
        "tail_count": tail_count,
        "tail_rule": "max(1, floor(n * (1 - confidence)))",
        "threshold_bps": round(tail[-1], 4),
        "status": "ok",
        "reason_code": None,
        "reason": None,
        "sample_count": count,
        "required_sample_count": MIN_CVAR_POINTS,
        "window": window,
        "note": "历史 CVaR 是样本统计，不是损失上限，也不构成任何形式的收益担保。",
    }


def _returns_from_navs(navs: Sequence[ProductNav]) -> list[tuple[date, float]]:
    series: list[tuple[date, float]] = []
    for nav in navs:
        value = nav.daily_return_bps
        if value is None:
            continue
        as_float = float(value)
        if not math.isfinite(as_float):
            continue
        series.append((nav.as_of, as_float))
    return series


def _combine_series(
    per_asset: Mapping[str, Sequence[tuple[date, float]]],
    weights: Mapping[str, float],
) -> list[tuple[date, float]]:
    """按观测日交集对齐的固定权重组合日收益（不做再平衡）。"""
    included = [asset_id for asset_id in per_asset if weights.get(asset_id, 0.0) > 0]
    if not included:
        return []
    date_sets = [{day for day, _value in per_asset[asset_id]} for asset_id in included]
    common = sorted(set.intersection(*date_sets)) if date_sets else []
    maps = {asset_id: dict(per_asset[asset_id]) for asset_id in included}
    return [
        (day, sum(weights[asset_id] * maps[asset_id][day] for asset_id in included))
        for day in common
    ]


def _hash_payload(
    *,
    method_version: str,
    as_of: date,
    history_snapshot_id: str | None,
    asset_payload: Sequence[Mapping[str, Any]],
    weights: Mapping[str, float],
) -> dict[str, Any]:
    return {
        "adapter": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "method_version": method_version,
        "as_of": as_of,
        "history_snapshot_id": history_snapshot_id,
        "weights": {key: round(float(value), 10) for key, value in sorted(weights.items())},
        "assets": list(asset_payload),
    }


def risk_report(
    session: Session,
    *,
    asset_ids: Sequence[str],
    weights: Any = None,
    as_of: Any = None,
    history_snapshot_id: str | None = None,
    method_version: str = METHOD_VERSION,
) -> dict[str, Any]:
    """只读组合风险报告：年化波动、最大回撤、95% 历史 CVaR。

    数据不足或资产不适用时输出 ``value: null`` + ``reason``，不填零、不外推。
    """
    if method_version != METHOD_VERSION:
        # 方法版本必须显式冻结：不接受调用方用未登记的方法名掩盖口径变化。
        raise FinPilotError(
            "CAPABILITY_NOT_SUPPORTED",
            "未登记的方法版本。",
            details={"reason_codes": ["unknown_method_version"], "method_version": method_version},
        )
    if not asset_ids:
        raise FinPilotError("PARAM_INVALID", "asset_ids 不能为空。", details={"reason_codes": ["empty_asset_ids"]})

    asset_ids = [str(asset_id) for asset_id in asset_ids]
    if len(set(asset_ids)) != len(asset_ids):
        raise FinPilotError("PARAM_INVALID", "asset_ids 不能重复。", details={"reason_codes": ["duplicate_asset"]})

    normalized_weights = _normalize_weights(asset_ids, weights)
    observation_date = _normalize_as_of(as_of)
    warnings: list[dict[str, Any]] = []
    if history_snapshot_id is None:
        warnings.append(
            {
                "code": "history_snapshot_id_missing",
                "message": "未提供 history_snapshot_id，历史快照无法回溯到具体数据版本。",
            }
        )

    assets: list[dict[str, Any]] = []
    per_asset_series: dict[str, list[tuple[date, float]]] = {}
    hash_assets: list[dict[str, Any]] = []

    for asset_id in asset_ids:
        product = session.get(WealthProduct, asset_id)
        if product is None:
            assets.append(
                {
                    "asset_id": asset_id,
                    "name": None,
                    "product_type": None,
                    "issuer_risk_level": None,
                    "applicable": False,
                    "reason_code": "asset_not_found",
                    "reason": "资产不在产品目录中。",
                    "volatility": None,
                    "max_drawdown": None,
                    "cvar_95": None,
                    "sample_count": 0,
                    "window": None,
                }
            )
            warnings.append(
                {"code": "asset_not_found", "message": f"资产 {asset_id} 不在产品目录中，已从组合计算排除。"}
            )
            continue

        navs = list(
            session.execute(
                select(ProductNav)
                .where(ProductNav.product_id == asset_id, ProductNav.as_of <= observation_date)
                .order_by(ProductNav.as_of.asc(), ProductNav.nav_id.asc())
            ).scalars()
        )
        series = _returns_from_navs(navs)
        non_nav, non_nav_reason = _is_non_nav(product)

        if non_nav:
            reason_code, reason = "non_nav_product", (
                f"{non_nav_reason}；不使用证券回测代替真实产品风险等级。"
            )
        elif not series:
            reason_code, reason = "no_nav_history", "没有可用净值序列，无法计算风险指标。"
        else:
            reason_code, reason = None, None

        if reason_code is not None:
            assets.append(
                {
                    "asset_id": asset_id,
                    "name": product.name,
                    "product_type": product.product_type,
                    "issuer_risk_level": product.issuer_risk_level,
                    "applicable": False,
                    "reason_code": reason_code,
                    "reason": reason,
                    "volatility": None,
                    "max_drawdown": None,
                    "cvar_95": None,
                    "sample_count": len(series),
                    "window": _window(series),
                }
            )
            warnings.append({"code": reason_code, "message": f"{asset_id}：{reason}"})
            hash_assets.append({"asset_id": asset_id, "applicable": False, "reason_code": reason_code})
            continue

        volatility = annualized_volatility(series)
        drawdown = max_drawdown(series)
        cvar = historical_cvar(series)
        per_asset_series[asset_id] = series

        window = _window(series)
        median_gap = window["median_gap_days"] if window else None
        if median_gap is not None and median_gap > DAILY_FREQUENCY_MAX_GAP_DAYS:
            warnings.append(
                {
                    "code": "non_daily_frequency",
                    "message": f"{asset_id} 的观测中位间隔 {median_gap} 天，非日频数据不能不经处理与其他频率混算。",
                    "asset_id": asset_id,
                }
            )
        if volatility["status"] != "ok":
            warnings.append(
                {
                    "code": "insufficient_history_for_volatility",
                    "message": f"{asset_id}：{volatility['reason']}",
                    "asset_id": asset_id,
                }
            )
        if cvar["status"] != "ok":
            warnings.append(
                {
                    "code": "insufficient_history_for_cvar",
                    "message": f"{asset_id}：{cvar['reason']}",
                    "asset_id": asset_id,
                }
            )
        if window and window["points"] < MIN_VOL_POINTS:
            warnings.append(
                {
                    "code": "short_window_for_drawdown",
                    "message": f"{asset_id} 观察窗口仅 {window['points']} 个收益点，最大回撤的解释力有限。",
                    "asset_id": asset_id,
                }
            )

        assets.append(
            {
                "asset_id": asset_id,
                "name": product.name,
                "product_type": product.product_type,
                "issuer_risk_level": product.issuer_risk_level,
                "applicable": True,
                "reason_code": None,
                "reason": None,
                "volatility": volatility,
                "max_drawdown": drawdown,
                "cvar_95": cvar,
                "sample_count": len(series),
                "window": window,
                "data_version": {
                    "terms_version": product.terms_version,
                    "method_version": method_version,
                    "source": "ProductNav.daily_return_bps",
                },
            }
        )
        hash_assets.append(
            {
                "asset_id": asset_id,
                "applicable": True,
                "points": len(series),
                "window": window,
                "returns_sha256": canonical_hash([round(value, 6) for _day, value in series]),
            }
        )

    # --- 组合层 -----------------------------------------------------------
    weights_used = {
        asset_id: weight for asset_id, weight in normalized_weights.items() if asset_id in per_asset_series
    }
    excluded_assets = sorted(set(normalized_weights) - set(per_asset_series))
    raw_total = sum(weights_used.values())
    renormalized = False
    if weights_used and abs(raw_total - 1.0) > 1e-9:
        weights_used = {asset_id: weight / raw_total for asset_id, weight in weights_used.items()}
        renormalized = True
    if excluded_assets:
        warnings.append(
            {
                "code": "weights_renormalized",
                "message": "不适用的资产已从组合计算排除，剩余权重按比例重新归一。",
                "excluded_assets": excluded_assets,
                "renormalized": renormalized,
            }
        )

    portfolio_series = _combine_series(per_asset_series, weights_used)
    if not per_asset_series:
        portfolio: dict[str, Any] = {
            "applicable": False,
            "reason_code": "no_applicable_assets",
            "reason": "所有资产都不适用（非净值产品或无净值数据），不输出组合指标。",
            "volatility": None,
            "max_drawdown": None,
            "cvar_95": None,
            "sample_count": 0,
            "window": None,
            "weights_used": {},
        }
    else:
        portfolio = {
            "applicable": True,
            "reason_code": None,
            "reason": None,
            "volatility": annualized_volatility(portfolio_series),
            "max_drawdown": max_drawdown(portfolio_series),
            "cvar_95": historical_cvar(portfolio_series),
            "sample_count": len(portfolio_series),
            "window": _window(portfolio_series),
            "weights_used": {key: round(value, 10) for key, value in sorted(weights_used.items())},
            "alignment": "intersection_of_observation_dates",
            "method": "fixed_weight_daily_return_sum",
        }

    payload = _hash_payload(
        method_version=method_version,
        as_of=observation_date,
        history_snapshot_id=history_snapshot_id,
        asset_payload=hash_assets,
        weights=normalized_weights,
    )

    return {
        "tool": "quant.risk_report",
        "adapter": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "read_only": True,
        "side_effects": [],
        "applicable": bool(per_asset_series),
        "method_version": method_version,
        "method_card": {
            "data_source": "ProductNav.daily_return_bps",
            "annualization_factor": ANNUALIZATION_FACTOR,
            "volatility": "样本标准差（ddof=1）× √252",
            "max_drawdown": "累计净值的峰谷最大跌幅（负值）",
            "cvar": "最差 max(1, floor(n×0.05)) 个日收益的均值",
            "cvar_confidence": CVAR_CONFIDENCE,
            "min_vol_points": MIN_VOL_POINTS,
            "min_cvar_points": MIN_CVAR_POINTS,
            "alignment": "observation_date_intersection",
            "frequency_policy": "非日频数据不与其他频率混算",
            "not_for": "不用于替代产品风险等级、适当性评估、流动性或条款判断",
        },
        "as_of": observation_date.isoformat(),
        "computed_at": format_iso(utcnow()),
        "history_snapshot_id": history_snapshot_id,
        "source_hash": canonical_hash(payload),
        "hash_payload_version": "sq-hash-1",
        "asset_ids": asset_ids,
        "weights": {key: round(value, 10) for key, value in sorted(normalized_weights.items())},
        "assets": assets,
        "portfolio": portfolio,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def risk_rank_without_quant(
    session: Session,
    ctx: Any = None,
    *,
    budget_minor: int,
    horizon_days: int,
    preferences: Any = None,
) -> list[dict[str, Any]]:
    """H4 消融对照（A4）：**同一排序逻辑去掉量化指标**。

    实现上复用 ``wealth_rules.compare.rank_products(include_quant=False)``，
    只关闭历史衍生指标（收益），风险等级、费率、流动性、期限与条款照旧参与
    硬过滤与排序，因此差异可以被完整解释为"量化指标带来的增量"。
    """
    from ...domains.wealth_rules.compare import rank_products

    return rank_products(
        session,
        ctx,
        budget_minor=budget_minor,
        horizon_days=horizon_days,
        preferences=preferences,
        include_quant=False,
    )


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "ANNUALIZATION_FACTOR",
    "CVAR_CONFIDENCE",
    "DISCLAIMER",
    "METHOD_VERSION",
    "MIN_CVAR_POINTS",
    "MIN_DRAWDOWN_POINTS",
    "MIN_VOL_POINTS",
    "NON_NAV_PRODUCT_TYPES",
    "annualized_volatility",
    "historical_cvar",
    "max_drawdown",
    "risk_rank_without_quant",
    "risk_report",
]