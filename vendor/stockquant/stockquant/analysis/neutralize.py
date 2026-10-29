"""Label neutralization for cross-sectional return prediction.

A raw forward return still embeds industry, market-beta and (in A-shares)
size/style components. Training a model on the raw cross-section makes it fit
those exposures rather than stock-selection skill. This module strips the
industry mean and the market-beta component from the label.

Three modes, from weakest to strongest:

* ``mean``          - remove the equal-weight universe mean (legacy behaviour)
* ``industry``      - remove the sector mean
* ``industry_beta`` - remove the sector mean, then the beta * market component
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Sequence

from stockquant.domain import MarketData
from stockquant.factors import basic

MODE_MEAN = "mean"
MODE_INDUSTRY = "industry"
MODE_INDUSTRY_BETA = "industry_beta"


def estimate_beta(market: MarketData, symbol: str, date: str, lookback: int = 60, market_returns: Optional[Sequence[float]] = None) -> float:
    """Single-index beta of ``symbol`` vs the equal-weight universe."""
    if market_returns is None:
        market_returns = equal_weight_market_returns(market, date, lookback)
    stock_rets = basic.daily_returns(market.history(symbol, date, lookback=lookback + 1))
    n = min(len(market_returns), len(stock_rets))
    if n < 20:
        return 1.0
    x = list(market_returns[-n:])
    y = list(stock_rets[-n:])
    mx, my = statistics.mean(x), statistics.mean(y)
    var = sum((v - mx) ** 2 for v in x)
    if var <= 1e-18:
        return 1.0
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return cov / var


def equal_weight_market_returns(market: MarketData, date: str, lookback: int = 60) -> List[float]:
    dates = [d for d in market.dates if d <= date][-(lookback + 1):]
    series: List[float] = []
    for prev, cur in zip(dates, dates[1:]):
        prev_bars = market.snapshot(prev)
        cur_bars = market.snapshot(cur)
        rets = []
        for symbol, a in prev_bars.items():
            b = cur_bars.get(symbol)
            if b and a.close > 0 and b.close > 0 and not a.suspended and not b.suspended:
                rets.append(b.close / a.close - 1.0)
        if rets:
            series.append(sum(rets) / len(rets))
    return series


def market_forward_return(market: MarketData, date: str, future: str) -> Optional[float]:
    rets = []
    for symbol, bar in market.snapshot(date).items():
        fut = market.bar(symbol, future)
        if fut is None or bar.close <= 0 or fut.close <= 0 or bar.suspended:
            continue
        rets.append(fut.close / bar.close - 1.0)
    return sum(rets) / len(rets) if rets else None


def neutralized_labels(
    market: MarketData,
    date: str,
    future: str,
    symbols: Sequence[str],
    forward_returns: Dict[str, float],
    mode: str = MODE_INDUSTRY_BETA,
) -> Dict[str, float]:
    """Return neutralized forward returns for the given symbols."""
    if not forward_returns:
        return {}
    out = dict(forward_returns)

    if mode in (MODE_INDUSTRY, MODE_INDUSTRY_BETA):
        by_sector: Dict[str, List[float]] = {}
        for symbol, value in out.items():
            bar = market.bar(symbol, date)
            sector = (bar.sector if bar and bar.sector else "Unknown")
            by_sector.setdefault(sector, []).append(value)
        sector_mean = {k: statistics.mean(v) for k, v in by_sector.items()}
        for symbol in list(out):
            bar = market.bar(symbol, date)
            sector = (bar.sector if bar and bar.sector else "Unknown")
            out[symbol] = out[symbol] - sector_mean.get(sector, 0.0)

    if mode == MODE_INDUSTRY_BETA:
        mkt = market_forward_return(market, date, future)
        if mkt is not None:
            cached = equal_weight_market_returns(market, date, 60)
            for symbol in list(out):
                beta = estimate_beta(market, symbol, date, market_returns=cached)
                out[symbol] = out[symbol] - beta * mkt

    if mode == MODE_MEAN:
        mean = statistics.mean(out.values())
        out = {s: v - mean for s, v in out.items()}
    return out
