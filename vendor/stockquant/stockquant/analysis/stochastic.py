"""Stochastic models: Kalman filter (state estimation) and Ornstein-Uhlenbeck.

Two well-known continuous-time / state-space tools that a factor system can
actually use:

* **Kalman filter** - estimate a latent state (here a *time-varying* market
  beta, or a smoothed trend) from noisy observations.  A static OLS beta is a
  single number for the whole window; the Kalman beta adapts, so beta
  neutralisation does not average over regime changes.
* **Ornstein-Uhlenbeck (OU)** - mean-reverting process
  ``dx = theta (mu - x) dt + sigma dW``.  Fitted discretely as an AR(1), it
  gives a mean-reversion speed and half-life, and a standardised deviation from
  the long-run mean that can serve as a statistical-arbitrage / reversal signal.

Pure-Python (no numpy required) so the research path stays dependency-light.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["kalman_filter", "kalman_beta", "kalman_beta_path", "ou_fit", "ou_zscore"]


def kalman_filter(
    observations: Sequence[float],
    process_var: float = 1e-4,
    measurement_var: float = 1e-2,
    initial: Optional[float] = None,
) -> List[float]:
    """1-D Kalman filter for a slowly-varying latent level.

    State model ``x_t = x_{t-1} + w``, ``w ~ N(0, process_var)``;
    observation ``z_t = x_t + v``, ``v ~ N(0, measurement_var)``.
    """
    obs = [float(x) for x in observations if x is not None and math.isfinite(float(x))]
    if not obs:
        return []
    x = float(initial) if initial is not None else obs[0]
    p = 1.0
    out: List[float] = []
    for z in obs:
        # Predict
        p = p + process_var
        # Update
        k = p / (p + measurement_var)
        x = x + k * (z - x)
        p = (1.0 - k) * p
        out.append(x)
    return out


def kalman_beta(
    asset_returns: Sequence[float],
    market_returns: Sequence[float],
    process_var: float = 1e-4,
    measurement_var: float = 1e-2,
) -> float:
    """Time-varying beta via a two-state Kalman filter [alpha, beta].

    Observation ``r_asset = alpha + beta * r_market + noise``.  Returns the
    final (most recent) beta estimate.  Falls back to the OLS slope if the
    inputs are degenerate.
    """
    pairs = [
        (float(a), float(m))
        for a, m in zip(asset_returns, market_returns)
        if a is not None and m is not None and math.isfinite(float(a)) and math.isfinite(float(m))
    ]
    if len(pairs) < 5:
        return _ols_beta([a for a, _ in pairs], [m for _, m in pairs])
    state = [0.0, 1.0]           # [alpha, beta]
    cov = [[1.0, 0.0], [0.0, 1.0]]  # state covariance
    q = process_var
    r = measurement_var
    for a, m in pairs:
        # Predict (random walk on both states)
        cov[0][0] += q
        cov[1][1] += q
        # Observation: H = [1, m]
        y = a - (state[0] + state[1] * m)
        s = cov[0][0] + m * (cov[0][1] + cov[1][0]) + m * m * cov[1][1] + r
        if s <= 1e-18:
            continue
        k0 = (cov[0][0] + m * cov[0][1]) / s
        k1 = (cov[1][0] + m * cov[1][1]) / s
        state[0] += k0 * y
        state[1] += k1 * y
        # Covariance update: P = (I - K H) P
        c00, c01 = cov[0][0], cov[0][1]
        c10, c11 = cov[1][0], cov[1][1]
        cov[0][0] = c00 - k0 * (c00 + m * c10)
        cov[0][1] = c01 - k0 * (c01 + m * c11)
        cov[1][0] = c10 - k1 * (c00 + m * c10)
        cov[1][1] = c11 - k1 * (c01 + m * c11)
    return float(state[1])


def kalman_beta_path(
    asset_returns: Sequence[float],
    market_returns: Sequence[float],
    process_var: float = 1e-4,
    measurement_var: float = 1e-2,
) -> List[float]:
    """Return the *sequential* beta estimate after each observation.

    ``path[t]`` is the beta known using observations up to and including ``t``
    (no look-ahead), so a hedged series can use ``path[t-1]`` for period ``t``.
    """
    pairs = [
        (float(a), float(m))
        for a, m in zip(asset_returns, market_returns)
        if a is not None and m is not None and math.isfinite(float(a)) and math.isfinite(float(m))
    ]
    if len(pairs) < 2:
        return [0.0 for _ in pairs]
    state = [0.0, 1.0]
    cov = [[1.0, 0.0], [0.0, 1.0]]
    q, r = process_var, measurement_var
    out: List[float] = []
    for a, m in pairs:
        cov[0][0] += q
        cov[1][1] += q
        y = a - (state[0] + state[1] * m)
        s = cov[0][0] + m * (cov[0][1] + cov[1][0]) + m * m * cov[1][1] + r
        if s > 1e-18:
            k0 = (cov[0][0] + m * cov[0][1]) / s
            k1 = (cov[1][0] + m * cov[1][1]) / s
            state[0] += k0 * y
            state[1] += k1 * y
            c00, c01, c10, c11 = cov[0][0], cov[0][1], cov[1][0], cov[1][1]
            cov[0][0] = c00 - k0 * (c00 + m * c10)
            cov[0][1] = c01 - k0 * (c01 + m * c11)
            cov[1][0] = c10 - k1 * (c00 + m * c10)
            cov[1][1] = c11 - k1 * (c01 + m * c11)
        out.append(float(state[1]))
    return out


def _ols_beta(y: Sequence[float], x: Sequence[float]) -> float:
    n = len(y)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    var = sum((xi - mx) ** 2 for xi in x)
    return cov / var if var > 1e-16 else 0.0


def ou_fit(values: Sequence[float]) -> Dict[str, float]:
    """Fit a discrete OU / AR(1) process to a level series.

    ``x_t = a + b x_{t-1} + e`` -> mean ``mu = a/(1-b)``, mean-reversion speed
    ``theta = -ln(b)``, half-life ``ln(2)/theta``.  ``b >= 1`` means no
    mean reversion (returns theta=0, half_life=inf).
    """
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(vals) < 10:
        return {"mean": statistics.mean(vals) if vals else 0.0, "theta": 0.0,
                "sigma": 0.0, "half_life": float("inf"), "b": 1.0}
    x_prev = vals[:-1]
    x_cur = vals[1:]
    n = len(x_prev)
    mx = sum(x_prev) / n
    my = sum(x_cur) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x_prev, x_cur))
    var = sum((a - mx) ** 2 for a in x_prev)
    b = cov / var if var > 1e-16 else 1.0
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(x_prev, x_cur)]
    sigma = statistics.pstdev(resid) if len(resid) > 1 else 0.0
    if 0.0 < b < 1.0:
        theta = -math.log(b)
        half_life = math.log(2.0) / theta
    else:
        theta = 0.0
        half_life = float("inf")
    mean = a / (1.0 - b) if abs(1.0 - b) > 1e-12 else statistics.mean(vals)
    return {"mean": mean, "theta": theta, "sigma": sigma, "half_life": half_life, "b": b}


def ou_zscore(values: Sequence[float]) -> Optional[float]:
    """Standardised deviation of the latest level from the OU long-run mean.

    A large positive value = the level is stretched above its mean-reverting
    equilibrium (a *negative* expected-drift / reversal signal).
    """
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(vals) < 20:
        return None
    fit = ou_fit(vals)
    resid_std = statistics.pstdev(vals)
    if resid_std <= 1e-12:
        return None
    return (vals[-1] - fit["mean"]) / resid_std
