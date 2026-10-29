"""Position sizing: Kelly criterion and volatility targeting.

Kelly (``f* = Sigma^-1 mu``) maximises the expected log-growth of wealth, but it
is brutally sensitive to estimation error in ``mu``.  The literature (MacLean,
Thorp & Ziemba; and the "half-Kelly" practice) recommends a *fractional* Kelly,
so ``fraction`` defaults to 0.5 and the result is long-only and capped.

Also exposes a size-per-name volatility target and a simple GARCH(1,1)
volatility forecast (used by the risk overlay).
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence

from stockquant.domain import MarketData
from stockquant.portfolio.optimizer import estimate_covariance, estimate_scenarios

__all__ = ["kelly_weights", "fractional_kelly_scale", "garch11_volatility"]


def _zscore(values: Sequence[float]) -> List[float]:
    vals = list(values)
    if not vals:
        return []
    mean = statistics.mean(vals)
    std = statistics.pstdev(vals)
    if std <= 1e-12:
        return [0.0 for _ in vals]
    return [(v - mean) / std for v in vals]


def kelly_weights(
    market: MarketData,
    scores: Dict[str, float],
    symbols: Sequence[str],
    date: str,
    fraction: float = 0.5,
    alpha_scale: float = 0.05,
    cap: float = 0.2,
    covariance_lookback: int = 60,
    investable: float = 1.0,
    min_weight: float = 0.002,
) -> Dict[str, float]:
    """Long-only *fractional* Kelly weights.  Falls back to ``{}`` if numpy is missing.

    ``mu = Sigma^-1 z(score)`` gives a direction, but its *scale* is not
    identified: ``alpha_scale`` is an arbitrary score-to-return mapping, so
    ``sum(mu)`` is not a meaningful leverage number.  The previous implementation
    multiplied by ``fraction`` and then normalised the vector to the full budget,
    which cancelled ``fraction`` exactly -- ``fraction=0.1`` and ``fraction=1.0``
    produced the same book (verified numerically), i.e. "half Kelly" was a no-op.

    Here the direction is normalised first and ``fraction`` controls the share of
    investable capital that is actually deployed; the remainder stays in cash.
    ``fraction=1.0`` reproduces the old fully-invested book, ``fraction=0.5`` puts
    half the book in cash.  Per-name weights are then clipped at ``cap``; a clip
    holds the excess as cash instead of re-scaling the others (which would undo
    the fraction again).
    """
    try:
        import numpy as np
    except Exception:
        return {}
    kept = [s for s in symbols if s in scores]
    if len(kept) < 2:
        return {}
    cov, cov_kept = estimate_covariance(market, kept, date, lookback=covariance_lookback)
    if cov is None or len(cov_kept) < 2:
        return {}
    score_kept = [scores[s] for s in cov_kept]
    mu = np.asarray(_zscore(score_kept)) * alpha_scale  # per-period-ish expected return
    sigma = np.asarray(cov) / 252.0  # convert annualized cov back to per-period
    try:
        raw = np.linalg.solve(sigma, mu)
    except np.linalg.LinAlgError:
        raw = np.linalg.pinv(sigma) @ mu
    raw = np.clip(raw, 0.0, None)
    total = float(raw.sum())
    if total <= 1e-12:
        return {}
    direction = raw / total
    deployable = max(0.0, min(investable, 1.0))
    gross = max(0.0, min(1.0, float(fraction))) * deployable
    weights = np.clip(direction * gross, 0.0, cap)
    return {cov_kept[i]: float(weights[i]) for i in range(len(cov_kept)) if weights[i] >= min_weight}


def fractional_kelly_scale(
    expected_return: float,
    variance: float,
    fraction: float = 0.5,
    max_scale: float = 1.0,
    min_scale: float = 0.0,
) -> float:
    """Total-exposure multiplier from the continuous Kelly fraction.

    ``f* = mu / sigma^2`` (per period).  We take ``fraction`` of it, clip to
    ``[min_scale, max_scale]`` and treat a non-positive edge as zero exposure.
    """
    if variance <= 1e-12 or expected_return <= 0:
        return max(0.0, min_scale)
    f = fraction * (expected_return / variance)
    return max(min_scale, min(max_scale, f))


def garch11_volatility(
    returns: Sequence[float],
    omega: Optional[float] = None,
    alpha: float = 0.08,
    beta: float = 0.90,
    periods_per_year: int = 252,
) -> float:
    """Annualized GARCH(1,1) one-step volatility forecast.

    ``sigma_t^2 = omega + alpha * e_{t-1}^2 + beta * sigma_{t-1}^2`` with the
    long-run variance set to the sample variance when ``omega`` is None
    (``omega = var * (1 - alpha - beta)``).  Captures volatility clustering, so it
    reacts faster than a flat 60-day realised vol when vol regimes shift.
    """
    rets = [float(r) for r in returns if math.isfinite(float(r))]
    if len(rets) < 5:
        return 0.0
    var = statistics.pvariance(rets)
    if var <= 1e-18:
        return 0.0
    if omega is None:
        omega = var * max(0.0, 1.0 - alpha - beta)
    sigma2 = var
    for r in rets:
        sigma2 = omega + alpha * (r * r) + beta * sigma2
    return math.sqrt(max(sigma2, 0.0) * periods_per_year)
