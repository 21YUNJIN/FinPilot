"""Risk metrics for bank wealth products, built on the StockQuant kernel.

Why this module exists
----------------------
StockQuant's own analytics are stock/factor oriented and expect the 5.5 GB
``data/real`` corpus.  A bank wealth product is a *NAV series* plus contract
terms.  What is reusable is the statistics discipline, not the stock pipeline,
so this module keeps the computation but feeds it product NAV.

The functions imported from ``stockquant.backtest.metrics`` are the same ones
used by the StockQuant backtest reports (population standard deviation,
Lo-2002 autocorrelation-adjusted Sharpe standard error).  Keeping the import
hard (no silent fallback) is deliberate: provenance must say which kernel
produced the number.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, Optional, Sequence

from stockquant.backtest.metrics import daily_returns, max_drawdown, sharpe_statistics

TRADING_DAYS_PER_YEAR = 252
MIN_RETURNS_FOR_VOL = 60
MIN_RETURNS_FOR_CVAR = 252
CVAR_ALPHA = 0.05


def _metric(
    value: Optional[float],
    *,
    unit: str,
    method: str,
    sample_count: int,
    window: str,
    na_reason: Optional[str] = None,
) -> Dict[str, object]:
    return {
        "value": None if value is None else round(float(value), 10),
        "unit": unit,
        "method": method,
        "sample_count": int(sample_count),
        "window": window,
        "na_reason": na_reason,
    }


def historical_cvar(returns: Sequence[float], alpha: float = CVAR_ALPHA) -> Optional[float]:
    """Historical CVaR (expected shortfall) of a return series.

    Same convention as ``stockquant.portfolio.optimizer.portfolio_cvar``: the
    mean of the worst ``ceil(alpha * n)`` returns, expressed as a *return*
    (negative = loss).  Returns ``None`` for an empty series.
    """
    values = [float(r) for r in returns if math.isfinite(float(r))]
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, int(math.ceil(alpha * len(ordered))))
    return sum(ordered[:k]) / k


def cumulative_return(nav: Sequence[float]) -> Optional[float]:
    if len(nav) < 2 or nav[0] <= 0:
        return None
    return nav[-1] / nav[0] - 1.0


def cagr(nav: Sequence[float], returns_count: int) -> Optional[float]:
    """Annualised return over the observed window (not a forecast)."""
    if len(nav) < 2 or nav[0] <= 0 or returns_count <= 0:
        return None
    years = returns_count / TRADING_DAYS_PER_YEAR
    if years <= 1e-9:
        return None
    ratio = nav[-1] / nav[0]
    if ratio <= 0:
        return None
    return ratio ** (1.0 / years) - 1.0


def downside_deviation(returns: Sequence[float]) -> Optional[float]:
    if len(returns) < 2:
        return None
    downs = [min(0.0, float(r)) for r in returns]
    return math.sqrt(sum(r * r for r in downs) / len(downs)) * math.sqrt(TRADING_DAYS_PER_YEAR)


def annual_volatility(returns: Sequence[float]) -> Optional[float]:
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


def compute_risk_metrics(
    nav_values: Sequence[float],
    *,
    risk_free_rate: float = 0.015,
    as_of: str,
) -> Dict[str, Dict[str, object]]:
    """Build the metric block for one product.

    Guardrails follow BANKAGENT_SRS_V5 §15.1:

    * annualised volatility needs >= 60 valid daily returns (252-day factor);
    * the formal historical 95% CVaR comparison is withheld below 252 returns;
    * maximum drawdown is reported over the *whole* observation window and is
      labelled a sample statistic, never a loss ceiling.
    """
    values = [float(v) for v in nav_values]
    returns = daily_returns(values)
    n = len(returns)
    window = f"observed_until:{as_of}"
    cagr_ok = n >= MIN_RETURNS_FOR_VOL
    metrics: Dict[str, Dict[str, object]] = {}

    metrics["cumulative_return"] = _metric(
        cumulative_return(values), unit="ratio", method="nav_last/nav_first-1",
        sample_count=len(values), window=window,
        na_reason=None if len(values) >= 2 else "insufficient_history",
    )
    metrics["cagr"] = _metric(
        cagr(values, n), unit="ratio_per_year", method="(nav_last/nav_first)^(252/n_returns)-1",
        sample_count=n, window=window,
        na_reason=None if (cagr_ok and len(values) >= 2) else "insufficient_history",
    )
    metrics["annual_volatility"] = _metric(
        annual_volatility(returns) if n >= MIN_RETURNS_FOR_VOL else None,
        unit="ratio_per_year", method="pstdev(daily_returns)*sqrt(252)",
        sample_count=n, window=window,
        na_reason=None if n >= MIN_RETURNS_FOR_VOL else "insufficient_history_min_60_returns",
    )
    metrics["downside_deviation"] = _metric(
        downside_deviation(returns) if n >= MIN_RETURNS_FOR_VOL else None,
        unit="ratio_per_year", method="sqrt(mean(min(0,r)^2))*sqrt(252)",
        sample_count=n, window=window,
        na_reason=None if n >= MIN_RETURNS_FOR_VOL else "insufficient_history_min_60_returns",
    )
    metrics["max_drawdown"] = _metric(
        max_drawdown(values) if len(values) >= 2 else None,
        unit="ratio", method="min(nav/peak-1) over whole window",
        sample_count=len(values), window=window,
        na_reason=None if len(values) >= 2 else "insufficient_history",
    )
    metrics["historical_cvar_95"] = _metric(
        historical_cvar(returns, CVAR_ALPHA) if n >= MIN_RETURNS_FOR_CVAR else None,
        unit="daily_return", method="mean(worst ceil(5%*n) daily returns)",
        sample_count=n, window=window,
        na_reason=None if n >= MIN_RETURNS_FOR_CVAR else "insufficient_history_min_252_returns",
    )
    metrics["worst_day"] = _metric(
        min(returns) if returns else None, unit="daily_return", method="min(daily_returns)",
        sample_count=n, window=window,
        na_reason=None if returns else "insufficient_history",
    )
    metrics["best_day"] = _metric(
        max(returns) if returns else None, unit="daily_return", method="max(daily_returns)",
        sample_count=n, window=window,
        na_reason=None if returns else "insufficient_history",
    )
    metrics["hit_rate"] = _metric(
        (sum(1 for r in returns if r > 0) / n) if n else None,
        unit="ratio", method="count(r>0)/n",
        sample_count=n, window=window,
        na_reason=None if n else "insufficient_history",
    )

    if n >= MIN_RETURNS_FOR_VOL and statistics.pstdev(returns) > 1e-16:
        sharpe = sharpe_statistics(returns, TRADING_DAYS_PER_YEAR)
        rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
        excess = [r - rf_daily for r in returns]
        sharpe_annual = statistics.mean(excess) / statistics.pstdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
        metrics["sharpe"] = _metric(
            sharpe_annual, unit="ratio_per_year",
            method="mean(r-rf)/pstdev(r)*sqrt(252) with Lo-2002 SE",
            sample_count=n, window=window,
        )
        metrics["sharpe_t_stat"] = _metric(
            sharpe.get("sharpe_t_stat"), unit="t_stat",
            method="Lo-2002 autocorrelation-adjusted t-stat",
            sample_count=n, window=window,
        )
        metrics["sharpe_se_annual"] = _metric(
            sharpe.get("sharpe_se_annual"), unit="ratio_per_year",
            method="Lo-2002 autocorrelation-adjusted SE",
            sample_count=n, window=window,
        )
        metrics["autocorrelation_factor"] = _metric(
            sharpe.get("autocorrelation_factor"), unit="ratio",
            method="Bartlett-kernel variance inflation factor",
            sample_count=n, window=window,
        )
    else:
        for name in ("sharpe", "sharpe_t_stat", "sharpe_se_annual", "autocorrelation_factor"):
            metrics[name] = _metric(
                None, unit="ratio", method="n/a", sample_count=n, window=window,
                na_reason="insufficient_history_min_60_returns",
            )

    mdd = metrics["max_drawdown"]["value"]
    cagr_value = metrics["cagr"]["value"]
    metrics["calmar"] = _metric(
        (cagr_value / abs(mdd)) if (cagr_value is not None and mdd not in (None, 0)) else None,
        unit="ratio", method="cagr/abs(max_drawdown)",
        sample_count=n, window=window,
        na_reason=None if (cagr_value is not None and mdd not in (None, 0)) else "insufficient_history_or_zero_drawdown",
    )
    return metrics


def risk_band(metrics: Dict[str, Dict[str, object]]) -> Dict[str, object]:
    """Map observed market risk to a *diagnostic* band, not an official rating.

    The bank product's official risk level (R1-R5) stays authoritative for
    suitability.  This band exists to catch mapping errors, e.g. an "R2" bond
    fund whose observed volatility behaves like R4.
    """
    vol = metrics.get("annual_volatility", {}).get("value")
    mdd = metrics.get("max_drawdown", {}).get("value")
    if vol is None:
        return {
            "band": None,
            "na_reason": "insufficient_history",
            "basis": "annual_volatility thresholds; not an official rating",
        }
    thresholds = [(0.015, "R1"), (0.030, "R2"), (0.060, "R3"), (0.120, "R4")]
    band = "R5"
    for limit, label in thresholds:
        if vol < limit:
            band = label
            break
    # A deep drawdown with low vol is still a warning sign; bump one level.
    if mdd is not None and mdd < -0.15:
        order = ["R1", "R2", "R3", "R4", "R5"]
        idx = min(len(order) - 1, order.index(band) + 1)
        band = order[idx]
    return {
        "band": band,
        "basis": "annual_volatility thresholds + drawdown bump; not an official rating",
        "thresholds": {"R1": "<1.5%", "R2": "<3%", "R3": "<6%", "R4": "<12%", "R5": ">=12%"},
    }