"""Small pure-Python panel operator layer for GTJA191 research.

The existing :class:`FactorEngine` evaluates one symbol at a time.  That is
enough for simple price factors, but not for GTJA formulas containing
cross-sectional ``RANK`` followed by another time-series operator.  This module
is a deliberately independent research layer:

    daily panel -> rank/corr/cov/delay -> panel factor -> IC scan

It is not wired into production defaults.  All values are returned as
``{date: {symbol: value}}`` so the existing analysis scripts can consume them.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from stockquant.domain import MarketData


Panel = Dict[str, Dict[str, float]]


def _finite(v: object) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def panel_values(market: MarketData, field: str, dates: Optional[Iterable[str]] = None) -> Panel:
    """Extract a numeric OHLCV field into a date -> symbol -> value panel."""
    selected = set(dates) if dates is not None else None
    out: Panel = {}
    for date in market.dates:
        if selected is not None and date not in selected:
            continue
        row: Dict[str, float] = {}
        for symbol, bar in market.snapshot(date, apply_membership=True).items():
            if field == "amount":
                raw = bar.close * bar.volume
            elif field == "intraday_return":
                raw = (bar.close - bar.open) / bar.open if bar.open > 0 else None
            else:
                raw = getattr(bar, field, None)
            value = _finite(raw)
            if value is not None:
                row[symbol] = value
        if row:
            out[date] = row
    return out


def cross_sectional_rank(panel: Panel, *, reverse: bool = False) -> Panel:
    """Percentile rank within each date; ties receive the average rank."""
    out: Panel = {}
    for date, row in panel.items():
        items = sorted(row.items(), key=lambda kv: kv[1])
        n = len(items)
        if n == 0:
            continue
        ranked: Dict[str, float] = {}
        i = 0
        while i < n:
            j = i
            while j + 1 < n and items[j + 1][1] == items[i][1]:
                j += 1
            avg = (i + j) / 2.0
            pct = avg / (n - 1) if n > 1 else 0.5
            if reverse:
                pct = 1.0 - pct
            for k in range(i, j + 1):
                ranked[items[k][0]] = pct
            i = j + 1
        out[date] = ranked
    return out


def delay(panel: Panel, periods: int = 1) -> Panel:
    """Shift each symbol's time series by ``periods`` sessions."""
    if periods <= 0:
        return {d: dict(v) for d, v in panel.items()}
    by_symbol: Dict[str, List[Tuple[str, float]]] = {}
    for date in sorted(panel):
        for symbol, value in panel[date].items():
            by_symbol.setdefault(symbol, []).append((date, value))
    out: Panel = {d: {} for d in panel}
    for symbol, series in by_symbol.items():
        for i, (date, _value) in enumerate(series):
            if i >= periods:
                out[date][symbol] = series[i - periods][1]
    return out


def delta(panel: Panel, periods: int = 1) -> Panel:
    shifted = delay(panel, periods)
    out: Panel = {}
    for date, row in panel.items():
        vals = {}
        for symbol, value in row.items():
            if symbol in shifted.get(date, {}):
                vals[symbol] = value - shifted[date][symbol]
        if vals:
            out[date] = vals
    return out


def _aligned_corr(xs: Mapping[str, float], ys: Mapping[str, float]) -> Optional[float]:
    common = sorted(set(xs) & set(ys))
    if len(common) < 3:
        return None
    x = [xs[s] for s in common]
    y = [ys[s] for s in common]
    mx, my = statistics.mean(x), statistics.mean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    vx = sum(v * v for v in dx)
    vy = sum(v * v for v in dy)
    if vx <= 1e-16 or vy <= 1e-16:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / math.sqrt(vx * vy)


def rolling_corr(left: Panel, right: Panel, window: int) -> Panel:
    """Rolling pairwise correlation per symbol over aligned dates."""
    if window < 2:
        raise ValueError("window must be >= 2")
    dates = sorted(set(left) | set(right))
    symbols = sorted({s for row in left.values() for s in row} & {s for row in right.values() for s in row})
    out: Panel = {}
    for symbol in symbols:
        lseries = [(d, left[d][symbol]) for d in dates if symbol in left.get(d, {})]
        rmap = {d: right[d][symbol] for d in dates if symbol in right.get(d, {})}
        lmap = dict(lseries)
        for i, (date, _) in enumerate(lseries):
            if i + 1 < window:
                continue
            lwin = {d: lmap[d] for d, _ in lseries[i - window + 1:i + 1] if d in rmap}
            rwin = {d: rmap[d] for d in lwin}
            corr = _aligned_corr(lwin, rwin)
            if corr is not None:
                out.setdefault(date, {})[symbol] = corr
    return out


def rolling_cov(left: Panel, right: Panel, window: int) -> Panel:
    """Rolling pairwise sample covariance per symbol over aligned dates."""
    if window < 2:
        raise ValueError("window must be >= 2")
    dates = sorted(set(left) | set(right))
    symbols = sorted({s for row in left.values() for s in row} & {s for row in right.values() for s in row})
    out: Panel = {}
    for symbol in symbols:
        lmap = {d: left[d][symbol] for d in dates if symbol in left.get(d, {})}
        rmap = {d: right[d][symbol] for d in dates if symbol in right.get(d, {})}
        common = sorted(set(lmap) & set(rmap))
        for i in range(window - 1, len(common)):
            dates_win = common[i - window + 1:i + 1]
            x = [lmap[d] for d in dates_win]
            y = [rmap[d] for d in dates_win]
            mx, my = statistics.mean(x), statistics.mean(y)
            cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / (window - 1)
            if math.isfinite(cov):
                out.setdefault(common[i], {})[symbol] = cov
    return out


def subtract(left: Panel, right: Panel) -> Panel:
    out: Panel = {}
    for date in set(left) & set(right):
        row = {s: left[date][s] - right[date][s] for s in set(left[date]) & set(right[date])}
        if row:
            out[date] = row
    return out


def add(left: Panel, right: Panel) -> Panel:
    out: Panel = {}
    for date in set(left) & set(right):
        row = {s: left[date][s] + right[date][s] for s in set(left[date]) & set(right[date])}
        if row:
            out[date] = row
    return out


@dataclass(frozen=True)
class PanelFactorSpec:
    name: str
    description: str
    fn: Callable[[MarketData], Panel]


def gtja_185(market: MarketData) -> Panel:
    close = panel_values(market, "close")
    open_ = panel_values(market, "open")
    raw: Panel = {}
    for date in set(close) & set(open_):
        row = {}
        for symbol in set(close[date]) & set(open_[date]):
            c = close[date][symbol]
            o = open_[date][symbol]
            if c > 0:
                row[symbol] = -(1.0 - o / c) ** 2
        if row:
            raw[date] = row
    return cross_sectional_rank(raw)


def gtja_184(market: MarketData) -> Panel:
    close = panel_values(market, "close")
    open_ = panel_values(market, "open")
    oc = subtract(open_, close)
    corr = rolling_corr(delay(oc, 1), close, 200)
    ranked_corr = cross_sectional_rank(corr)
    ranked_oc = cross_sectional_rank(oc)
    return add(ranked_corr, ranked_oc)


def gtja_083(market: MarketData) -> Panel:
    high = panel_values(market, "high")
    volume = panel_values(market, "volume")
    cov = rolling_cov(cross_sectional_rank(high), cross_sectional_rank(volume), 5)
    neg = {d: {s: -v for s, v in row.items()} for d, row in cov.items()}
    return cross_sectional_rank(neg)


def gtja_099(market: MarketData) -> Panel:
    close = panel_values(market, "close")
    volume = panel_values(market, "volume")
    cov = rolling_cov(cross_sectional_rank(close), cross_sectional_rank(volume), 5)
    neg = {d: {s: -v for s, v in row.items()} for d, row in cov.items()}
    return cross_sectional_rank(neg)


def gtja_105(market: MarketData) -> Panel:
    open_ = panel_values(market, "open")
    volume = panel_values(market, "volume")
    return rolling_corr(cross_sectional_rank(open_), cross_sectional_rank(volume), 10)


PANEL_FACTORS: Tuple[PanelFactorSpec, ...] = (
    PanelFactorSpec("gtja_185", "Rank(-(1-open/close)^2)", gtja_185),
    PanelFactorSpec("gtja_184", "Rank(Corr(delay(O-C,1), C, 200)) + Rank(O-C)", gtja_184),
    PanelFactorSpec("gtja_083", "-Rank(Cov(Rank(H),Rank(V),5))", gtja_083),
    PanelFactorSpec("gtja_099", "-Rank(Cov(Rank(C),Rank(V),5))", gtja_099),
    PanelFactorSpec("gtja_105", "Corr(Rank(O),Rank(V),10)", gtja_105),
)


def compute_panel_factor(market: MarketData, spec: PanelFactorSpec) -> Panel:
    return spec.fn(market)


__all__ = ["Panel", "panel_values", "cross_sectional_rank", "delay", "delta",
           "rolling_corr", "rolling_cov", "subtract", "add", "PanelFactorSpec",
           "PANEL_FACTORS", "compute_panel_factor"]
