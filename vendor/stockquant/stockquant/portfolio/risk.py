from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.domain import MarketData


def estimate_universe_volatility(market: MarketData, date: str, lookback: int = 20) -> Optional[float]:
    """Annualized volatility of the equal-weight universe (a *market* estimate).

    This is not the volatility of the portfolio we actually hold; see
    :func:`portfolio_volatility` and :func:`target_volatility_scale`.
    """
    dates = [d for d in market.dates if d <= date]
    dates = dates[-(lookback + 1):]
    if len(dates) < 8:
        return None
    rets: List[float] = []
    for prev, cur in zip(dates, dates[1:]):
        prev_bar = market.snapshot(prev)
        cur_bar = market.snapshot(cur)
        daily: List[float] = []
        for sym, a in prev_bar.items():
            b = cur_bar.get(sym)
            if b and a.close > 0 and b.close > 0 and a.quote_fresh() and b.quote_fresh():
                daily.append(b.close / a.close - 1.0)
        if daily:
            rets.append(sum(daily) / len(daily))
    if len(rets) < 5:
        return None
    return statistics.pstdev(rets) * math.sqrt(252.0)


def _universe_return_series(market: MarketData, date: str, lookback: int) -> List[float]:
    dates = [d for d in market.dates if d <= date][-(lookback + 1):]
    rets: List[float] = []
    for prev, cur in zip(dates, dates[1:]):
        prev_bar = market.snapshot(prev)
        cur_bar = market.snapshot(cur)
        daily = []
        for sym, a in prev_bar.items():
            b = cur_bar.get(sym)
            if b and a.close > 0 and b.close > 0 and b.quote_fresh() and cur_bar[sym].quote_fresh():
                daily.append(b.close / a.close - 1.0)
        if daily:
            rets.append(sum(daily) / len(daily))
    return rets


def portfolio_returns_by_date(
    market: MarketData,
    date: str,
    weights: Dict[str, float],
    lookback: int,
) -> Dict[str, float]:
    """Realized portfolio return per session for a *fixed* weight vector.

    Cash (``1 - sum(w)``) earns 0, and a name without a usable print on a session
    simply does not contribute that day: a portfolio return series assembled from
    a halt is not a real return.
    """
    from stockquant.portfolio.optimizer import returns_by_date

    series = {
        s: returns_by_date(market, s, date, lookback)
        for s, w in weights.items()
        if abs(w) > 1e-12
    }
    sessions = set()
    for s in series:
        sessions |= set(series[s])
    sessions = sorted(sessions)
    out: Dict[str, float] = {}
    for session in sessions:
        total = 0.0
        for s, w in weights.items():
            r = series.get(s, {}).get(session)
            if r is not None:
                total += w * r
        out[session] = total
    return out


def portfolio_volatility(
    market: MarketData,
    date: str,
    weights: Dict[str, float],
    lookback: int = 60,
    shrinkage: float = 0.35,
) -> Optional[float]:
    """Annualized ``sqrt(w' Sigma w)`` of the *actual* target book.

    Returns ``None`` when fewer than two names carry a covariance estimate; a
    single-name book falls back to its own realized volatility.
    """
    held = {s: w for s, w in weights.items() if abs(w) > 1e-12}
    if not held:
        return None
    if len(held) == 1:
        from stockquant.portfolio.optimizer import returns_by_date
        symbol = next(iter(held))
        rets = list(returns_by_date(market, symbol, date, lookback).values())
        if len(rets) < 5:
            return None
        return statistics.pstdev(rets) * math.sqrt(252.0) * abs(held[symbol])
    from stockquant.portfolio.optimizer import estimate_covariance

    try:
        import numpy as np
    except Exception:
        return None
    cov, kept = estimate_covariance(
        market, list(held), date, lookback=lookback, shrinkage=shrinkage
    )
    if cov is None or len(kept) < 2:
        return None
    w = np.asarray([held[s] for s in kept], dtype=float)
    variance = float(w @ np.asarray(cov) @ w)
    if variance <= 0:
        return None
    return math.sqrt(variance)


def target_volatility_scale(
    market: MarketData,
    date: str,
    target_volatility: Optional[float],
    weights: Optional[Dict[str, float]] = None,
    lookback: int = 20,
    method: str = "realized",
    min_scale: float = 0.20,
    max_scale: float = 1.0,
) -> Tuple[float, float, str]:
    """Scale factor that pulls the *actual* book toward ``target_volatility``.

    Returns ``(scale, estimated_annualized_vol, source)`` where ``source`` is one
    of ``"portfolio"``, ``"universe"`` or ``"none"``.  The portfolio estimate uses
    the covariance of the names we are about to hold -- the old implementation
    scaled by the equal-weight *universe* volatility, so a concentrated book in
    one high-beta sector could run far above (or below) the configured target
    while the report still claimed "target 18%".
    """
    if not target_volatility or target_volatility <= 0:
        return 1.0, 0.0, "none"

    est: Optional[float] = None
    source = "none"
    if weights:
        if method == "garch":
            from stockquant.portfolio.sizing import garch11_volatility
            series = portfolio_returns_by_date(market, date, weights, max(lookback, 60))
            ordered = [series[d] for d in sorted(series)]
            est = garch11_volatility(ordered) if ordered else None
        else:
            est = portfolio_volatility(market, date, weights, lookback=max(lookback, 60))
        if est:
            source = "portfolio"

    if not est:
        if method == "garch":
            from stockquant.portfolio.sizing import garch11_volatility
            est = garch11_volatility(_universe_return_series(market, date, max(lookback, 60)))
        else:
            est = estimate_universe_volatility(market, date, lookback=lookback)
        source = "universe" if est else "none"

    if not est or est <= 1e-12:
        return 1.0, 0.0, "none"
    scale = max(min_scale, min(max_scale, target_volatility / est))
    return scale, float(est), source


def volatility_scale(
    market: MarketData,
    date: str,
    target_volatility: Optional[float],
    lookback: int = 20,
    method: str = "realized",
    target_weights: Optional[Dict[str, float]] = None,
) -> float:
    scale, _, _ = target_volatility_scale(
        market, date, target_volatility, weights=target_weights, lookback=lookback, method=method
    )
    return scale


def market_trend_scale(
    market: MarketData,
    date: str,
    lookback: int = 200,
    risk_off_exposure: float = 0.5,
) -> float:
    """Return a long-only exposure multiplier from an equal-weight trend filter."""
    dates = [d for d in market.dates if d <= date]
    if len(dates) < max(60, lookback // 2):
        return 1.0
    level = 100.0
    levels = []
    prev_date = None
    for cur in dates:
        if prev_date is None:
            levels.append(level)
        else:
            prev_bars = market.snapshot(prev_date)
            cur_bars = market.snapshot(cur)
            rets = []
            for symbol, a in prev_bars.items():
                b = cur_bars.get(symbol)
                if b and a.close > 0 and b.close > 0 and a.quote_fresh() and b.quote_fresh():
                    rets.append(b.close / a.close - 1.0)
            if rets:
                level *= 1.0 + sum(rets) / len(rets)
            levels.append(level)
        prev_date = cur
    window = levels[-lookback:] if len(levels) >= lookback else levels
    ma = sum(window) / len(window) if window else level
    return risk_off_exposure if level < ma else 1.0
