"""Covariance-aware portfolio construction.

The classic heuristic in :mod:`stockquant.portfolio.constructor` ranks by score
and caps weights, but it is blind to the *joint* risk of the names it picks.  Two
defensive stocks and two 0.95-correlated tech names both look "diversified" to it.

This module adds a mean-variance overlay:

    max_w  mu' w - (gamma / 2) w' Sigma w
    s.t.   sum(w) = investable, 0 <= w_i <= max_weight,
           sector caps, optional one-way turnover budget

``Sigma`` is a *shrunk* sample covariance (Ledoit-Wolf style shrinkage toward the
diagonal) because the sample covariance of ~60 daily returns for ~30 names is
ill-conditioned and would otherwise produce wild weights.  The alpha ``mu`` is a
small tilt derived from the cross-sectional factor score, so the optimizer is a
risk allocator, not a return forecaster.

Return alignment
----------------
Every return series produced here is keyed by the session it was realized on, and
matrices are built on the *intersection* of those sessions.  The previous version
padded halted/missing days with ``0.0`` and then truncated series to a common
length by position, which made suspended names look low-risk, manufactured
correlations between names whose gaps did not line up, and understated CVaR tail
scenarios.

Everything degrades gracefully: if numpy is unavailable, callers fall back to the
score-weighted heuristic.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.domain import Bar, MarketData

__all__ = [
    "estimate_covariance",
    "estimate_scenarios",
    "estimate_betas",
    "optimize_target_weights",
    "project_budget_box",
    "portfolio_cvar",
    "returns_by_date",
]


def returns_by_date(market: MarketData, symbol: str, date: str, lookback: int) -> Dict[str, float]:
    """Simple returns keyed by realized session, gaps preserved.

    Days where the price grid has no usable print (halt, suspension, missing row)
    are *absent* rather than zero: a halt is not a 0% return, and pretending it is
    understates risk for exactly the names a risk model needs to be careful about.
    """
    bars = market.history(symbol, date, lookback=lookback + 1)
    out: Dict[str, float] = {}
    last_fresh: Optional[Bar] = None
    for bar in bars:
        if not bar.quote_fresh() or bar.close <= 0:
            continue
        if last_fresh is not None and last_fresh.close > 0:
            out[bar.date] = bar.close / last_fresh.close - 1.0
        last_fresh = bar
    return out


def _daily_returns_series(market: MarketData, symbol: str, date: str, lookback: int) -> List[float]:
    """Back-compat wrapper: aligned returns as a plain list (no zero padding)."""
    series = returns_by_date(market, symbol, date, lookback)
    return [series[d] for d in sorted(series)]


def _aligned_matrix(
    market: MarketData,
    symbols: Sequence[str],
    date: str,
    lookback: int,
    min_obs: int,
    min_overlap: float = 0.5,
):
    """(dates, symbols, matrix[n_assets, T]) on the common session intersection.

    Returns ``(None, [], None)`` when the overlap is too thin to estimate risk.
    """
    try:
        import numpy as np
    except Exception:
        return None, [], None
    series: Dict[str, Dict[str, float]] = {}
    keep: List[str] = []
    for symbol in symbols:
        if not symbol:
            continue
        s = returns_by_date(market, symbol, date, lookback)
        if len(s) >= min_obs:
            series[symbol] = s
            keep.append(symbol)
    if len(keep) < 2:
        return None, [], None
    common = set(series[keep[0]])
    for symbol in keep[1:]:
        common &= set(series[symbol])
    dates = sorted(common)
    # Demand that the shared window is a usable fraction of what we asked for:
    # two names that only overlap for a handful of sessions cannot inform a
    # covariance estimate no matter how many observations each has alone.
    if len(dates) < max(min_obs, int(min_overlap * lookback)):
        return None, [], None
    mat = np.asarray([[series[s][d] for d in dates] for s in keep], dtype=float)
    return dates, keep, mat


def estimate_covariance(
    market: MarketData,
    symbols: Sequence[str],
    date: str,
    lookback: int = 60,
    shrinkage: float = 0.35,
    periods_per_year: int = 252,
    min_obs: int = 20,
    min_overlap: float = 0.5,
):
    """Return (annualized covariance matrix, usable symbols) or (None, []).

    Shrinks the sample covariance toward its diagonal by ``shrinkage`` in [0,1]
    (0 = raw sample, 1 = diagonal-only).  Returns a plain list-of-lists so the
    caller does not need numpy until it actually optimizes.
    """
    dates, keep, mat = _aligned_matrix(
        market, symbols, date, lookback, min_obs=min_obs, min_overlap=min_overlap
    )
    if mat is None:
        return None, []
    import numpy as np

    width = mat.shape[1]
    mat = mat - mat.mean(axis=1, keepdims=True)
    n = mat.shape[0]
    sample = (mat @ mat.T) / max(1, width - 1)
    diag = np.diag(np.diag(sample))
    cov = (1.0 - shrinkage) * sample + shrinkage * diag
    cov = cov * periods_per_year
    # Numerical hygiene: never let a zero variance through.
    cov = cov + np.eye(n) * 1e-8
    return cov, keep


def estimate_scenarios(
    market: MarketData,
    symbols: Sequence[str],
    date: str,
    lookback: int = 120,
    min_obs: int = 40,
    min_overlap: float = 0.33,
):
    """Return (T, n) array of daily return scenarios over ``lookback`` days.

    Used for a historical (Rockafellar-Uryasev style) CVaR tail-risk penalty so
    the optimizer does not ignore the joint left tail of the selected names.
    Rows are joint sessions only: a scenario that would pair one name's crash with
    another name's halt is not a scenario that can happen.
    """
    dates, keep, mat = _aligned_matrix(
        market, symbols, date, lookback, min_obs=min_obs, min_overlap=min_overlap
    )
    if mat is None:
        return None, []
    return mat.T, keep  # (T, n)


def estimate_betas(
    market: MarketData,
    symbols: Sequence[str],
    date: str,
    lookback: int = 120,
) -> Dict[str, float]:
    """Single-index betas of each symbol vs the equal-weight universe.

    Both sides are aligned by session before the regression: the old version
    matched the last ``n`` observations of two independently gap-filtered series.
    """
    dates = [d for d in market.dates if d <= date][-(lookback + 1):]
    mkt: Dict[str, float] = {}
    for prev, cur in zip(dates, dates[1:]):
        pb = market.snapshot(prev)
        cb = market.snapshot(cur)
        daily = [
            cb[s].close / pb[s].close - 1.0
            for s in pb
            if s in cb and pb[s].close > 0 and cb[s].close > 0 and pb[s].quote_fresh() and cb[s].quote_fresh()
        ]
        if daily:
            mkt[cur] = sum(daily) / len(daily)
    if len(mkt) < 20:
        return {}
    mm = statistics.mean(mkt.values())
    var_m = sum((x - mm) ** 2 for x in mkt.values())
    if var_m <= 1e-18:
        return {}
    out: Dict[str, float] = {}
    for symbol in symbols:
        r = returns_by_date(market, symbol, date, len(mkt))
        common = sorted(set(r) & set(mkt))
        if len(common) < 20:
            continue
        x = [mkt[d] for d in common]
        y = [r[d] for d in common]
        my = statistics.mean(y)
        cov = sum((a - mm) * (b - my) for a, b in zip(x, y))
        out[symbol] = cov / var_m
    return out


def portfolio_cvar(scenarios, weights, alpha: float = 0.05) -> float:
    """Historical CVaR (expected shortfall) of the portfolio at level ``alpha``.

    Returns a *negative* number (a return), i.e. the mean return over the worst
    ``alpha`` fraction of scenarios.
    """
    import numpy as np
    w = np.asarray(weights, dtype=float)
    port = np.asarray(scenarios) @ w
    if port.size == 0:
        return 0.0
    k = max(1, int(math.ceil(alpha * port.size)))
    worst = np.partition(port, k - 1)[:k]
    return float(worst.mean())


def project_budget_box(values: Sequence[float], budget: float, cap: float) -> List[float]:
    """Euclidean projection onto {w : sum(w)=budget, 0<=w_i<=cap}.

    Solved by bisection on the shift ``t`` with ``w_i = clip(v_i + t, 0, cap)``;
    ``sum(clip(v+t))`` is monotone in ``t``.
    """
    if budget <= 0:
        return [0.0 for _ in values]
    n = len(values)
    if cap * n < budget - 1e-12:
        return [cap for _ in values]
    lo = min(values) - budget - cap
    hi = max(values) + budget + cap
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        total = sum(min(cap, max(0.0, v + mid)) for v in values)
        if total > budget:
            hi = mid
        else:
            lo = mid
    t = 0.5 * (lo + hi)
    return [min(cap, max(0.0, v + t)) for v in values]


def _sector_caps(
    weights: Dict[str, float],
    sector_of: Dict[str, str],
    cap: float,
) -> Dict[str, float]:
    """Scale down any sector over ``cap`` and hold the result (no redistribution
    that could re-break the cap).  Mirrors the constructor's conservative rule."""
    for _ in range(40):
        totals: Dict[str, float] = {}
        for s, w in weights.items():
            totals[sector_of.get(s, "Unknown")] = totals.get(sector_of.get(s, "Unknown"), 0.0) + w
        over = {sec: v for sec, v in totals.items() if v > cap + 1e-12}
        if not over:
            break
        for sec, tot in over.items():
            scale = cap / tot
            for s in list(weights):
                if sector_of.get(s, "Unknown") == sec:
                    weights[s] *= scale
    return weights


def _turnover_cap(
    weights: Dict[str, float],
    current_weights: Optional[Dict[str, float]],
    budget: float,
) -> Dict[str, float]:
    """Cap **one-way** turnover: ``0.5 * sum|target - current|``.

    The basis is stated in the parameter name because mixing it up with the
    traded-notional (round-trip) definition has silently doubled or halved a
    turnover budget before.
    """
    if not current_weights or budget >= 1.0:
        return weights
    cur = {s: max(0.0, float(w)) for s, w in current_weights.items()}
    symbols = set(weights) | set(cur)
    move = {s: weights.get(s, 0.0) - cur.get(s, 0.0) for s in symbols}
    raw = 0.5 * sum(abs(v) for v in move.values())
    if raw <= budget + 1e-12:
        return weights
    scale = budget / raw
    out = {s: max(0.0, cur.get(s, 0.0) + scale * move[s]) for s in symbols}
    return {s: w for s, w in out.items() if w >= 5e-4}


def _trim_to_max_positions(weights: Dict[str, float], max_positions: int, investable: float, cap: float):
    """Keep the ``max_positions`` largest weights and redeploy the freed budget.

    Screening Top-N *before* the risk model (the old behaviour) meant the
    optimizer could never swap a high-score/high-variance name for a slightly
    lower-score name that hedges the rest of the book.  The candidate pool is now
    wider, so the final count has to be enforced after the optimisation.
    """
    if max_positions <= 0 or len(weights) <= max_positions:
        return weights
    kept = dict(sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:max_positions])
    total = sum(kept.values())
    if total <= 0:
        return {}
    kept = {s: v * investable / total for s, v in kept.items()}
    return {s: min(v, cap) for s, v in kept.items()}


def optimize_target_weights(
    market: MarketData,
    scores: Dict[str, float],
    bars: Dict[str, Bar],
    date: str,
    constraints,
    current_weights: Optional[Dict[str, float]] = None,
    alpha_scale: float = 0.05,
    risk_aversion: float = 3.0,
    covariance_lookback: int = 60,
    shrinkage: float = 0.35,
    max_iter: int = 300,
    learning_rate: float = 0.15,
    cvar_alpha: Optional[float] = None,
    cvar_lambda: float = 0.0,
    cvar_lookback: int = 120,
    betas: Optional[Dict[str, float]] = None,
    target_beta: Optional[float] = None,
    beta_lambda: float = 0.0,
    candidate_multiplier: float = 1.0,
) -> Dict[str, float]:
    """Mean-variance weights with box / sector / turnover constraints.

    ``alpha_scale`` is the *annualized* expected return assigned to a one-sigma
    score tilt, so the risk-aversion has a consistent meaning.  ``candidate_multiplier``
    (default 1.0 = the historical Top-N screen) controls how many score-ranked
    names enter the risk model before ``max_positions`` is enforced on the
    resulting weights.  Setting it >1 lets a lower-score but diversifying name be
    selected; on the CSI300-30 sample 3.0 measured *worse* (CAGR 2.9% vs 7.0%),
    so treat the wider pool as an open, trial-counted experiment.

    Falls back to the score-weighted heuristic when numpy is missing or the
    covariance cannot be estimated.
    """
    from stockquant.portfolio.constructor import build_target_weights

    try:
        import numpy as np
    except Exception:
        return build_target_weights(scores, bars, constraints, current_weights=current_weights)

    # 1) Eligible universe = same rule as the heuristic (tradable, score floor),
    #    widened so the risk model has something to choose between.
    eligible = [
        s for s, sc in scores.items()
        if bars.get(s) is not None and bars[s].tradable() and sc >= constraints.min_score
    ]
    eligible.sort(key=lambda s: scores[s], reverse=True)
    pool = max(1, constraints.max_positions)
    if candidate_multiplier and candidate_multiplier > 1.0:
        pool = max(pool, int(round(constraints.max_positions * float(candidate_multiplier))))
    eligible = eligible[:pool]
    if not eligible:
        return {}

    cov, kept = estimate_covariance(
        market, eligible, date, lookback=covariance_lookback, shrinkage=shrinkage
    )
    if cov is None or len(kept) < 2:
        return build_target_weights(scores, bars, constraints, current_weights=current_weights)

    vals = [scores[s] for s in kept]
    mean = statistics.mean(vals)
    std = statistics.pstdev(vals)
    if std <= 1e-12:
        mu = np.zeros(len(kept))
    else:
        mu = np.asarray([(v - mean) / std for v in vals]) * alpha_scale
    sigma = np.asarray(cov)

    beta_vec = None
    if betas and target_beta is not None and beta_lambda > 0:
        bvals = [float(betas.get(s, 0.0)) for s in kept]
        if any(abs(v) > 1e-9 for v in bvals):
            beta_vec = np.asarray(bvals)

    # Optional historical CVaR tail penalty (Rockafellar-Uryasev, scenario form).
    scen = None
    if cvar_alpha and cvar_lambda > 0.0:
        scen_full, scen_kept = estimate_scenarios(market, kept, date, lookback=cvar_lookback)
        if scen_full is not None:
            order = [scen_kept.index(s) for s in kept if s in scen_kept]
            if order and len(order) == len(kept):
                scen = scen_full

    investable = max(0.0, 1.0 - constraints.cash_buffer)
    n = len(kept)
    cap = float(constraints.max_weight)
    # Only enforce sector caps when the dataset actually carries sector labels;
    # otherwise a 35% cap on a single "Unknown" bucket would silently force the
    # strategy into ~65% cash.
    sector_of = {s: bars[s].sector for s in kept}
    use_sector_caps = (
        constraints.sector_max_weight < 1.0
        and any(sec and sec != "Unknown" for sec in sector_of.values())
    )
    w = np.asarray(project_budget_box([investable / n] * n, investable, cap))

    for _ in range(max_iter):
        grad = mu - risk_aversion * (sigma @ w)
        if scen is not None:
            # Subgradient of -CVaR = mean of the worst-alpha scenario returns.
            port = scen @ w
            k = max(1, int(math.ceil(cvar_alpha * port.shape[0])))
            worst_idx = np.argpartition(port, k - 1)[:k]
            tail = scen[worst_idx].mean(axis=0)
            grad = grad + cvar_lambda * tail
        w = w + learning_rate * grad
        w = np.asarray(project_budget_box(list(w), investable, cap))
        # Optional per-iteration sector pull-down keeps the fixed point feasible.
        if use_sector_caps:
            wd = {kept[i]: float(w[i]) for i in range(n)}
            wd = _sector_caps(wd, sector_of, float(constraints.sector_max_weight))
            w = np.asarray([wd.get(kept[i], 0.0) for i in range(n)])

    weights = {kept[i]: float(w[i]) for i in range(n) if w[i] >= 0.002}

    # Stable beta targeting: adjust the weights by a zero-sum tilt along the
    # (centered) beta vector so sum(w_i * beta_i) hits the target. A gradient
    # penalty is numerically unstable here (the beta gradient dwarfs the
    # mean-variance gradient), so we project once instead.
    if beta_vec is not None and target_beta is not None:
        wv = {s: weights.get(s, 0.0) for s in kept}
        b = np.asarray([float(betas.get(s, 0.0)) for s in kept])
        cur = np.asarray([wv.get(s, 0.0) for s in kept])
        bc = b - b.mean()
        denom = float(bc @ bc)
        target_scaled = float(target_beta) * max(1e-9, cur.sum())
        if denom > 1e-12:
            delta = (target_scaled - float(b @ cur)) / denom
            adj = np.clip(cur + delta * bc, 0.0, cap)
            tot = float(adj.sum())
            if tot > 0:
                adj = adj * (cur.sum() / tot)
            weights = {kept[i]: float(adj[i]) for i in range(n) if adj[i] >= 0.002}

    weights = _trim_to_max_positions(weights, constraints.max_positions, investable, cap)

    if use_sector_caps:
        weights = _sector_caps(weights, sector_of, float(constraints.sector_max_weight))
    weights = _turnover_cap(weights, current_weights, float(constraints.max_turnover))
    total = sum(weights.values())
    if total > investable > 0:
        weights = {s: v * investable / total for s, v in weights.items()}
    return weights
