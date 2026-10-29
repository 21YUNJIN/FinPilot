"""Assemble one product's read-only risk report.

The report is *data*: no buy/sell recommendation, no expected return, no
statement that a drawdown cannot be exceeded.  Ordering decisions live in
``rank.py`` and always apply the bank's hard constraints first.
"""
from __future__ import annotations

import copy
import os
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Sequence

from .canonical import METHOD_VERSION, SCHEMA_VERSION, iso_now, sha256_json
from .errors import EngineUnavailable
from .metrics import TRADING_DAYS_PER_YEAR, compute_risk_metrics, risk_band
from .nav import validate_series


def engine_info() -> Dict[str, Any]:
    """Identify the actual kernel that produced the numbers."""
    try:
        import stockquant
    except Exception as exc:  # pragma: no cover - only on a broken install
        raise EngineUnavailable(
            "StockQuant 内核不可导入，拒绝用替代实现冒充原引擎",
            detail={"error": type(exc).__name__},
        ) from exc
    return {
        "name": "stockquant",
        "version": getattr(stockquant, "__version__", "unknown"),
        "commit": os.environ.get("SQ_BANK_ENGINE_COMMIT", "unknown"),
        "module": "stockquant.backtest.metrics",
        "kernel_functions": ["daily_returns", "max_drawdown", "sharpe_statistics"],
        "trading_days_per_year": TRADING_DAYS_PER_YEAR,
        "method_version": METHOD_VERSION,
    }


DISCLAIMERS: List[str] = [
    "历史样本统计，不代表未来收益，也不构成收益承诺。",
    "最大回撤是整个观察窗口的样本结果，不是损失上限。",
    "observed_market_risk_band 仅由净值统计推导，产品官方风险等级仍以产品条款为准。",
    "本服务只读：不申购、不赎回、不修改策略、不读取交易凭证。",
]


@lru_cache(maxsize=512)
def _cached_metrics(nav_values: tuple, as_of: str, risk_free_rate: float) -> Dict[str, Any]:
    """Metrics depend only on the series; cache them for the daily NAV cadence.

    ``computed_at`` is *not* cached: it is stamped per response so a cached
    number can never look fresher than the request that produced it.
    """
    return compute_risk_metrics(list(nav_values), risk_free_rate=risk_free_rate, as_of=as_of)


def build_product_report(
    product: Mapping[str, Any],
    *,
    as_of: str,
    risk_free_rate: float = 0.015,
) -> Dict[str, Any]:
    product_id = str(product.get("product_id") or product.get("id") or "").strip()
    series = validate_series(product_id, product.get("nav") or [], as_of=as_of)
    metrics = copy.deepcopy(_cached_metrics(tuple(series.values), as_of, risk_free_rate))
    band = risk_band(metrics)

    official = product.get("official_risk_level") or product.get("risk_level")
    warnings: List[str] = list(series.quality.get("warnings", []))
    if official and band.get("band") and band["band"] != official:
        warnings.append(
            f"净值统计风险档 {band['band']} 与产品官方等级 {official} 不一致，需核对产品映射或数据"
        )

    nav_values = series.values
    report: Dict[str, Any] = {
        "product_id": series.product_id,
        "product_name": product.get("product_name") or product.get("name") or series.product_id,
        "issuer": product.get("issuer"),
        "product_type": product.get("product_type"),
        "official_risk_level": official,
        "observed_market_risk_band": band,
        "data_window": {
            "start": series.start,
            "end": series.end,
            "points": len(nav_values),
            "returns": max(0, len(nav_values) - 1),
            "as_of": as_of,
            "stale_lag_days": series.quality["stale_lag_days"],
        },
        "nav": {
            "first": round(nav_values[0], 6),
            "last": round(nav_values[-1], 6),
            "min": round(min(nav_values), 6),
            "max": round(max(nav_values), 6),
        },
        "metrics": metrics,
        "usable_for_ranking": metrics["annual_volatility"]["value"] is not None,
        "warnings": warnings,
        "disclaimers": DISCLAIMERS,
        "provenance": {
            "adapter": "stockquant_bank.wealth_risk/1",
            "method_version": METHOD_VERSION,
            "computed_at": iso_now(),
            "source_hash": series.source_hash,
            "source": product.get("nav_source") or "caller_supplied_nav",
            "input_hash": sha256_json({
                "product_id": series.product_id,
                "as_of": as_of,
                "nav_hash": series.source_hash,
                "risk_free_rate": risk_free_rate,
            }),
        },
    }
    return report


def build_risk_report(
    products: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    risk_free_rate: float = 0.015,
) -> Dict[str, Any]:
    reports: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for product in products:
        try:
            reports.append(build_product_report(product, as_of=as_of, risk_free_rate=risk_free_rate))
        except Exception as exc:  # per-product isolation: one bad series must not hide the rest
            from .errors import QuantError

            pid = str(product.get("product_id") or product.get("id") or "unknown")
            if isinstance(exc, QuantError):
                failures.append({"product_id": pid, **exc.to_payload()})
            else:
                failures.append({
                    "product_id": pid,
                    "code": "PRODUCT_COMPUTE_FAILED",
                    "safe_message": "该产品净值无法计算，已跳过",
                    "retry_mode": "fix_request",
                })
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "as_of": as_of,
        "risk_free_rate": risk_free_rate,
        "products": reports,
        "failures": failures,
        "summary": {
            "requested": len(products),
            "computed": len(reports),
            "usable_for_ranking": sum(1 for r in reports if r["usable_for_ranking"]),
            "failed": len(failures),
        },
        "disclaimers": DISCLAIMERS,
    }
