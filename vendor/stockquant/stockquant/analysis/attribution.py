"""Factor-return construction and style/exposure attribution.

Two pieces, both dependency-free:

* :func:`factor_long_short_series` builds a long-short (top-minus-bottom
  quintile) return series for each factor, i.e. a tradable proxy for that
  factor's return.
* :func:`attribute_returns` regresses a portfolio's period returns on those
  factor returns to report style exposures, an intercept (residual alpha) and
  an R-squared.  This is the simple Barra-style decomposition the project was
  missing: it answers "how much of the P&L is just style beta?".
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.analysis.ic import _forward_return, _rebalance_due
from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec


def factor_long_short_series(
    market: MarketData,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    factors: Optional[Sequence[FactorSpec]] = None,
    min_market_cap_quantile: float = 0.0,
) -> Dict[str, List[Tuple[str, float]]]:
    """Per-factor long-short return series keyed by signal date.

    Long-short = mean forward return of the top quintile minus the bottom
    quintile of the factor score.
    """
    fset = tuple(factors) if factors is not None else DEFAULT_FACTORS
    engine = FactorEngine(
        fset,
        min_history=min_history,
        sector_neutral=sector_neutral,
        min_market_cap_quantile=min_market_cap_quantile,
    )
    dates = list(market.dates)
    out: Dict[str, List[Tuple[str, float]]] = {f.name: [] for f in fset}
    for i in range(0, max(0, len(dates) - horizon)):
        cur = dates[i]
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or not _rebalance_due(cur, nxt, rebalance):
            continue
        future = dates[i + horizon]
        components, diag = engine.components(market, cur)
        if diag.scored_count < 10:
            continue
        for spec in fset:
            pairs: List[Tuple[float, float]] = []
            for symbol, z in components.get(spec.name, {}).items():
                fr = _forward_return(market, symbol, cur, future)
                if fr is not None:
                    pairs.append((z, fr))
            if len(pairs) < 10:
                continue
            pairs.sort(key=lambda x: x[0])
            k = max(1, len(pairs) // 5)
            bottom = sum(v for _, v in pairs[:k]) / k
            top = sum(v for _, v in pairs[-k:]) / k
            out[spec.name].append((cur, top - bottom))
    return out


def equal_weight_forward_return(market: MarketData, date: str, horizon: int) -> Optional[float]:
    """Equal-weight (universe) forward return over ``horizon`` trading days."""
    try:
        idx = market.dates.index(date)
    except ValueError:
        return None
    if idx + horizon >= len(market.dates):
        return None
    future = market.dates[idx + horizon]
    rets = []
    for symbol, bar in market.snapshot(date).items():
        fut = market.bar(symbol, future)
        if fut is None or bar.close <= 0 or fut.close <= 0 or bar.suspended:
            continue
        rets.append(fut.close / bar.close - 1.0)
    return sum(rets) / len(rets) if rets else None


def beta_neutral_alpha(
    portfolio_returns: Sequence[float],
    market_returns: Sequence[float],
    periods_per_year: int = 12,
    method: str = "ols",
) -> Dict[str, object]:
    """Regress portfolio returns on the market and report the beta-hedged result.

    A low-volatility portfolio typically carries a low market beta, so its raw
    alpha partly reflects *not taking* market risk. Hedging beta isolates the
    part that is not explained by market exposure (Frazzini & Pedersen, 2014).
    """
    n = min(len(portfolio_returns), len(market_returns))
    if n < 4:
        return {"beta": 0.0, "alpha_period": 0.0, "alpha_annual": 0.0,
                "hedged_sharpe_annual": 0.0, "hedged_t_stat": 0.0, "observations": float(max(0, n))}
    p = [float(v) for v in portfolio_returns[-n:]]
    m = [float(v) for v in market_returns[-n:]]
    mp, mm = statistics.mean(p), statistics.mean(m)
    var_m = sum((v - mm) ** 2 for v in m) / (n - 1) if n > 1 else 0.0
    cov = sum((a - mp) * (b - mm) for a, b in zip(p, m)) / (n - 1) if n > 1 else 0.0
    if method == "kalman":
        from stockquant.analysis.stochastic import kalman_beta_path
        # Use the beta known *before* each period (path[t-1]) to avoid look-ahead.
        path = kalman_beta_path(p, m)
        betas = [path[0]] + path[:-1]
        hedged = [a - bt * b for a, b, bt in zip(p, m, betas)]
        beta = statistics.mean(betas) if betas else 0.0
        alpha = statistics.mean(hedged)
    else:
        beta = cov / var_m if var_m > 1e-16 else 0.0
        alpha = mp - beta * mm
        hedged = [a - beta * b for a, b in zip(p, m)]
    try:
        from stockquant.backtest.metrics import sharpe_statistics
        stats = sharpe_statistics(hedged, periods_per_year=periods_per_year)
    except Exception:
        stats = {}
    return {
        "beta": beta,
        "alpha_period": alpha,
        "alpha_annual": alpha * periods_per_year,
        "hedged_returns": hedged,
        "hedged_sharpe_annual": float(stats.get("sharpe_annual", 0.0)),
        "hedged_t_stat": float(stats.get("sharpe_t_stat", 0.0)),
        "hedged_t_stat_iid": float(stats.get("sharpe_t_stat_iid", 0.0)),
        "autocorrelation_factor": float(stats.get("autocorrelation_factor", 1.0)),
        "observations": float(n),
    }


def _solve(matrix: List[List[float]], rhs: List[float]) -> Optional[List[float]]:
    """Solve a small dense linear system with Gaussian elimination."""
    n = len(matrix)
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                aug[r][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def attribute_returns(
    portfolio_returns: Sequence[float],
    factor_returns: Dict[str, Sequence[float]],
    periods_per_year: int = 12,
) -> Dict[str, object]:
    """OLS regression of portfolio returns on factor returns.

    Returns ``{alpha_period, alpha_annual, exposures, r_squared, observations}``.
    """
    names = [name for name, series in factor_returns.items() if len(series) >= 2]
    n = len(portfolio_returns)
    for name in names:
        n = min(n, len(factor_returns[name]))
    if n < len(names) + 2 or not names:
        return {
            "alpha_period": 0.0,
            "alpha_annual": 0.0,
            "exposures": {name: 0.0 for name in factor_returns},
            "r_squared": 0.0,
            "observations": float(n if names else 0),
        }
    y = [float(v) for v in portfolio_returns[-n:]]
    columns = [[float(v) for v in factor_returns[name][-n:]] for name in names]
    # Design matrix with intercept.
    x = [[1.0] + [columns[j][i] for j in range(len(names))] for i in range(n)]
    p = len(names) + 1
    xtx = [[sum(x[i][a] * x[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    xty = [sum(x[i][a] * y[i] for i in range(n)) for a in range(p)]
    beta = _solve(xtx, xty)
    if beta is None:
        beta = [0.0] * p
    fitted = [sum(beta[a] * x[i][a] for a in range(p)) for i in range(n)]
    mean_y = statistics.mean(y)
    ss_res = sum((y[i] - fitted[i]) ** 2 for i in range(n))
    ss_tot = sum((v - mean_y) ** 2 for v in y)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-16 else 0.0
    alpha_period = beta[0]
    return {
        "alpha_period": alpha_period,
        "alpha_annual": alpha_period * periods_per_year,
        "exposures": {name: beta[j + 1] for j, name in enumerate(names)},
        "r_squared": r2,
        "observations": float(n),
    }


def style_attribution(
    market: MarketData,
    portfolio_returns_by_date: Dict[str, float],
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    min_market_cap_quantile: float = 0.0,
    factors: Optional[Sequence[FactorSpec]] = None,
) -> Dict[str, object]:
    """Regress per-rebalance portfolio returns on the factor long-short series."""
    series = factor_long_short_series(
        market,
        min_history=min_history,
        horizon=horizon,
        rebalance=rebalance,
        sector_neutral=sector_neutral,
        factors=factors,
        min_market_cap_quantile=min_market_cap_quantile,
    )
    # Align on dates common to the portfolio and every *non-empty* factor
    # series.  Factors with no observations (e.g. all-None fundamentals) are
    # dropped rather than wiping out the whole alignment.
    nonempty = {name: pairs for name, pairs in series.items() if pairs}
    common = set(portfolio_returns_by_date)
    for pairs in nonempty.values():
        common &= {d for d, _ in pairs}
    ordered = sorted(common)
    port = [float(portfolio_returns_by_date[d]) for d in ordered]
    factor_map = {}
    for name, pairs in nonempty.items():
        mapping = dict(pairs)
        if all(d in mapping for d in ordered) and ordered:
            factor_map[name] = [mapping[d] for d in ordered]

    # Market (equal-weight universe) is a required factor for beta attribution.
    market_map = {d: equal_weight_forward_return(market, d, horizon) for d in ordered}
    market_series = [market_map[d] for d in ordered if market_map.get(d) is not None]
    has_market = len(market_series) == len(ordered) and ordered

    periods = 52 if rebalance == "weekly" else 252 if rebalance == "daily" else 12
    factor_map_with_market = dict(factor_map)
    if has_market:
        factor_map_with_market["market"] = market_series
    result = attribute_returns(port, factor_map_with_market, periods_per_year=periods)
    result["dates"] = ordered
    if has_market:
        result["beta_neutral"] = beta_neutral_alpha(port, market_series, periods_per_year=periods)
    return result
