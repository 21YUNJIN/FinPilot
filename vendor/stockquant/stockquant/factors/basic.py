from __future__ import annotations

import math
import statistics
from typing import Dict, Iterable, List, Optional

from stockquant.domain import Bar


def daily_returns(bars: List[Bar]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(bars, bars[1:]):
        if prev.close > 0 and cur.close > 0:
            out.append(cur.close / prev.close - 1.0)
    return out


def returns_by_date(bars: List[Bar]) -> Dict[str, float]:
    """Simple returns keyed by the session they were realized on.

    Callers that must combine two series (stock vs market, stock vs beta proxy)
    need identity alignment: two windows of equal length can still cover
    different sessions when one series has halts or missing rows, and pairing
    them positionally fabricates correlations.
    """
    out: Dict[str, float] = {}
    # A halt/zero-volume row is neither a 0% return nor a lost return: it is a
    # *gap*.  The move from the last traded price to the next traded price is
    # attributed to the session trading resumes, which keeps the series aligned
    # with realizable dates and free of fabricated zeros.
    last_fresh: Optional[Bar] = None
    for bar in bars:
        if not bar.quote_fresh() or bar.close <= 0:
            continue
        if last_fresh is not None and last_fresh.close > 0:
            out[bar.date] = bar.close / last_fresh.close - 1.0
        last_fresh = bar
    return out


def momentum(bars: List[Bar], lookback: int = 60, skip: int = 5) -> Optional[float]:
    needed = lookback + skip + 1
    if len(bars) < needed:
        return None
    end_idx = len(bars) - 1 - skip
    start_idx = end_idx - lookback
    if start_idx < 0 or bars[start_idx].close <= 0:
        return None
    return bars[end_idx].close / bars[start_idx].close - 1.0


def reversal(bars: List[Bar], lookback: int = 5) -> Optional[float]:
    if len(bars) <= lookback:
        return None
    prev = bars[-1 - lookback].close
    if prev <= 0:
        return None
    return -(bars[-1].close / prev - 1.0)


def low_volatility(bars: List[Bar], lookback: int = 60) -> Optional[float]:
    rets = daily_returns(bars[-(lookback + 1):])
    if len(rets) < max(20, lookback // 2):
        return None
    return -statistics.pstdev(rets) * math.sqrt(252.0)


def liquidity(bars: List[Bar], lookback: int = 20) -> Optional[float]:
    window = bars[-lookback:]
    if len(window) < max(5, lookback // 2):
        return None
    values = [max(0.0, b.close * b.volume) for b in window if b.volume > 0]
    if not values:
        return None
    return math.log1p(sum(values) / len(values))


def earnings_yield(bar: Bar) -> Optional[float]:
    if bar.pe is not None and bar.pe > 0:
        return 1.0 / bar.pe
    if bar.raw_close > 0 and bar.eps is not None and bar.eps > 0:
        return bar.eps / bar.raw_close
    return None


def book_yield(bar: Bar) -> Optional[float]:
    if bar.pb is not None and bar.pb > 0:
        return 1.0 / bar.pb
    if bar.raw_close > 0 and bar.bps is not None and bar.bps > 0:
        return bar.bps / bar.raw_close
    return None


def growth(bar: Bar) -> Optional[float]:
    values = [v for v in (bar.revenue_growth, bar.profit_growth) if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def gross_margin(bar: Bar) -> Optional[float]:
    return bar.gross_margin


def forecast_score(bar: Bar) -> Optional[float]:
    if bar.forecast_growth is None and bar.forecast_positive is None:
        return None
    growth = bar.forecast_growth if bar.forecast_growth is not None else 0.0
    signal = bar.forecast_positive if bar.forecast_positive is not None else 0.0
    return signal + 0.05 * max(-5.0, min(5.0, growth))


def holder_concentration(bar: Bar) -> Optional[float]:
    return bar.holder_change


def quality(bar: Bar) -> Optional[float]:
    if bar.roe is None:
        return None
    return bar.roe


def size(bar: Bar) -> Optional[float]:
    if bar.market_cap <= 0:
        return None
    # Small-cap tilt. In production, explicitly treat this as a style exposure.
    return -math.log(bar.market_cap)


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def value_score(bar: Bar) -> Optional[float]:
    ey = earnings_yield(bar)
    by = book_yield(bar)
    return _mean([ey, by])


def amihud_illiquidity(bars: List[Bar], lookback: int = 20) -> Optional[float]:
    """Amihud (2002) illiquidity: mean of |daily return| / dollar volume over the
    window, scaled by 1e8.

    Higher == *less* liquid (costlier to trade).  Factor specs that want
    "liquid ranks better" must negate this value -- see
    ``factors.engine._factor_amihud``.
    """
    window = bars[-lookback:]
    if len(window) < max(5, lookback // 2):
        return None
    values: List[float] = []
    for prev, cur in zip(window, window[1:]):
        if prev.close <= 0 or cur.close <= 0 or cur.volume <= 0:
            continue
        ret = cur.close / prev.close - 1.0
        dollar = cur.close * cur.volume
        if dollar > 0:
            values.append(abs(ret) / dollar * 1e8)
    if not values:
        return None
    return sum(values) / len(values)
