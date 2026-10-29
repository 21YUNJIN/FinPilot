"""Experimental GTJA Alpha191 distillation.

This is deliberately **not** a 191-factor production model.  The first batch
contains only price/volume factors whose formulas can be reproduced from the
project's existing daily OHLCV bars without inventing missing fields.  The
full 191-factor source contains many cross-sectional RANK/CORR formulas and
requires a panel operator engine; those belong in a separate research layer.

Source references (checked 2026-10-22):

* ``yupoet/aurumq-rl`` — MIT-licensed GTJA191 implementation, tests and
  reference data: https://github.com/yupoet/aurumq-rl
* ``JustinF8/qlib-factor-zoo`` — MIT-licensed Qlib expression loader:
  https://github.com/JustinF8/qlib-factor-zoo
* ``veithly/QuantFactor`` — Apache-2.0 Alpha191 reference:
  https://github.com/veithly/QuantFactor

The implementations below are intentionally small, pure-Python translations
for research screening.  They are not wired into ``DEFAULT_FACTORS`` or
``EXTENDED_FACTORS``; adding any of them to production requires the same trial
registry / DSR / walk-forward gates as any other candidate.
"""
from __future__ import annotations

import math
import statistics
from typing import Callable, List, Optional, Sequence, Tuple

from stockquant.domain import Bar
from stockquant.factors.engine import FactorSpec

_EPS = 1e-12


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return sum(vals) / len(vals) if vals else None


def _std(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return statistics.pstdev(vals) if len(vals) >= 2 else None


def _safe_div(a: float, b: float) -> Optional[float]:
    if not math.isfinite(a) or not math.isfinite(b) or abs(b) <= _EPS:
        return None
    return a / b


def _corr(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    if mx is None or my is None:
        return None
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    vx = sum(x * x for x in dx)
    vy = sum(y * y for y in dy)
    if vx <= _EPS or vy <= _EPS:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / math.sqrt(vx * vy)


def _gtja_sma(values: Sequence[float], n: int, m: int) -> Optional[float]:
    """GTJA recursive SMA: y_t = (m*x_t + (n-m)*y_{t-1}) / n."""
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals or n <= 0 or m <= 0 or m > n:
        return None
    alpha = m / n
    out = vals[0]
    for value in vals[1:]:
        out = alpha * value + (1.0 - alpha) * out
    return out


def _bars_ok(bars: Sequence[Bar], need: int, *, volume: bool = False) -> bool:
    if len(bars) < need:
        return False
    for b in bars[-need:]:
        if b.close <= 0 or b.high <= 0 or b.low <= 0:
            return False
        if volume and b.volume <= 0:
            return False
    return True


def _gtja_003(bars: List[Bar]) -> Optional[float]:
    """6d close-vs-extreme conditional flow, scaled by mean close."""
    if not _bars_ok(bars, 7):
        return None
    window = bars[-7:]
    total = 0.0
    for prev, cur in zip(window, window[1:]):
        if cur.close == prev.close:
            continue
        if cur.close > prev.close:
            total += cur.close - min(cur.low, prev.close)
        else:
            total += cur.close - max(cur.high, prev.close)
    scale = _mean([b.close for b in window])
    return _safe_div(total, scale) if scale else None


def _gtja_009(bars: List[Bar]) -> Optional[float]:
    """EWMA(7,2) of mid-price acceleration weighted by range / volume."""
    if not _bars_ok(bars, 10, volume=True):
        return None
    xs: List[float] = []
    for prev, cur in zip(bars[-8:-1], bars[-7:]):
        mid = (cur.high + cur.low) / 2.0
        prev_mid = (prev.high + prev.low) / 2.0
        xs.append((mid - prev_mid) * (cur.high - cur.low) / cur.volume)
    sma = _gtja_sma(xs, 7, 2)
    scale = _mean([b.close for b in bars[-7:]])
    return _safe_div(sma, scale) if sma is not None and scale else None


def _gtja_011(bars: List[Bar]) -> Optional[float]:
    """6d range-position weighted by volume, scaled to price/volume units."""
    if not _bars_ok(bars, 7, volume=True):
        return None
    window = bars[-7:]
    total = 0.0
    for b in window[1:]:
        rng = b.high - b.low
        if rng <= 0:
            continue
        total += ((b.close - b.low) - (b.high - b.close)) / rng * b.volume
    scale = _mean([b.close * b.volume for b in window])
    return _safe_div(total, scale) if scale else None


def _gtja_023(bars: List[Bar]) -> Optional[float]:
    """Up-day STD share over 20d, smoothed with GTJA SMA(20,1)."""
    if not _bars_ok(bars, 40):
        return None
    window = bars[-40:]
    std20 = [_std([b.close for b in window[i - 19:i + 1]]) for i in range(20, len(window))]
    up = [std20[i - 20] if window[i].close > window[i - 1].close else 0.0 for i in range(20, len(window))]
    down = [std20[i - 20] if window[i].close <= window[i - 1].close else 0.0 for i in range(20, len(window))]
    up_s = _gtja_sma(up, 20, 1)
    down_s = _gtja_sma(down, 20, 1)
    if up_s is None or down_s is None:
        return None
    return _safe_div(up_s * 100.0, up_s + down_s)


def _gtja_040(bars: List[Bar]) -> Optional[float]:
    """26d up-volume / down-volume ratio, log-compressed."""
    if not _bars_ok(bars, 27, volume=True):
        return None
    window = bars[-27:]
    up = sum(cur.volume for prev, cur in zip(window, window[1:]) if cur.close > prev.close)
    down = sum(cur.volume for prev, cur in zip(window, window[1:]) if cur.close <= prev.close)
    ratio = _safe_div(up, down)
    if ratio is None:
        return None
    return math.log1p(max(0.0, ratio))


def _gtja_042(bars: List[Bar]) -> Optional[float]:
    """-std(H,10) * corr(H,V,10), normalised by mean high."""
    if not _bars_ok(bars, 10, volume=True):
        return None
    window = bars[-10:]
    highs = [b.high for b in window]
    vols = [b.volume for b in window]
    sd = _std(highs)
    corr = _corr(highs, vols)
    if sd is None or corr is None:
        return None
    return -sd / (statistics.mean(highs) or 1.0) * corr


def _gtja_049(bars: List[Bar]) -> Optional[float]:
    """Down-day range share over 12d."""
    if not _bars_ok(bars, 13):
        return None
    window = bars[-13:]
    total = sum(b.high - b.low for b in window[1:])
    down = sum(b.high - b.low for prev, b in zip(window, window[1:]) if b.close < prev.close)
    return _safe_div(down, total)


def _gtja_076(bars: List[Bar]) -> Optional[float]:
    """Dispersion of |return|/volume over 20d."""
    if not _bars_ok(bars, 21, volume=True):
        return None
    vals = []
    for prev, cur in zip(bars[-21:-1], bars[-20:]):
        r = cur.close / prev.close - 1.0
        vals.append(abs(r) / cur.volume)
    sd, mean = _std(vals), _mean(vals)
    return _safe_div(sd, mean) if sd is not None and mean is not None else None


def _gtja_079(bars: List[Bar]) -> Optional[float]:
    """12-day RSI."""
    if not _bars_ok(bars, 13):
        return None
    window = bars[-13:]
    gains = sum(max(0.0, cur.close - prev.close) for prev, cur in zip(window, window[1:]))
    losses = sum(max(0.0, prev.close - cur.close) for prev, cur in zip(window, window[1:]))
    denom = gains + losses
    return 100.0 * gains / denom if denom > _EPS else 50.0


def _gtja_095(bars: List[Bar]) -> Optional[float]:
    """20-day coefficient of variation of traded amount."""
    if not _bars_ok(bars, 20, volume=True):
        return None
    amounts = [b.close * b.volume for b in bars[-20:]]
    sd, mean = _std(amounts), _mean(amounts)
    return _safe_div(sd, mean) if sd is not None and mean is not None else None


def _gtja_097(bars: List[Bar]) -> Optional[float]:
    """10-day coefficient of variation of volume."""
    if not _bars_ok(bars, 10, volume=True):
        return None
    vols = [b.volume for b in bars[-10:]]
    sd, mean = _std(vols), _mean(vols)
    return _safe_div(sd, mean) if sd is not None and mean is not None else None


def _gtja_103(bars: List[Bar]) -> Optional[float]:
    """Recency of the 20-day low: 20 when today is the low, 1 at oldest."""
    if not _bars_ok(bars, 20):
        return None
    lows = [b.low for b in bars[-20:]]
    idx = min(range(len(lows)), key=lambda i: lows[i])
    return float(20 - idx)


def _gtja_112(bars: List[Bar]) -> Optional[float]:
    """12-day Chande momentum oscillator."""
    if not _bars_ok(bars, 13):
        return None
    window = bars[-13:]
    up = sum(max(0.0, cur.close - prev.close) for prev, cur in zip(window, window[1:]))
    down = sum(max(0.0, prev.close - cur.close) for prev, cur in zip(window, window[1:]))
    denom = up + down
    return 100.0 * (up - down) / denom if denom > _EPS else 0.0


def _gtja_116(bars: List[Bar]) -> Optional[float]:
    """20-day rolling OLS slope of close, scaled by mean close."""
    if not _bars_ok(bars, 20):
        return None
    closes = [b.close for b in bars[-20:]]
    n = len(closes)
    x_mean = (n - 1) / 2.0
    y_mean = statistics.mean(closes)
    denom = sum((i - x_mean) ** 2 for i in range(n))
    if denom <= _EPS or y_mean <= _EPS:
        return None
    slope = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(closes)) / denom
    return slope / y_mean


def _gtja_127(bars: List[Bar]) -> Optional[float]:
    """12-day RMS drawdown from the rolling high."""
    if not _bars_ok(bars, 25):
        return None
    vals = []
    for i in range(12, len(bars[-25:])):
        window = bars[-25:][i - 11:i + 1]
        high = max(b.high for b in window)
        if high > 0:
            vals.append((100.0 * (window[-1].close - high) / high) ** 2)
    return math.sqrt(statistics.mean(vals)) if vals else None


def _gtja_188(bars: List[Bar]) -> Optional[float]:
    """Range deviation from GTJA SMA(H-L, 11, 2)."""
    if not _bars_ok(bars, 22):
        return None
    ranges = [b.high - b.low for b in bars[-11:]]
    avg = _gtja_sma(ranges, 11, 2)
    if avg is None or avg <= _EPS:
        return None
    return (ranges[-1] - avg) / avg * 100.0


def _gtja_189(bars: List[Bar]) -> Optional[float]:
    """6-day mean absolute deviation from MA6, scaled by mean close."""
    if not _bars_ok(bars, 11):
        return None
    closes = [b.close for b in bars[-11:]]
    ma6 = [_mean(closes[i - 5:i + 1]) for i in range(5, len(closes))]
    vals = [abs(closes[i + 5] - ma6[i]) for i in range(len(ma6)) if ma6[i] is not None]
    if not vals:
        return None
    mean_close = _mean(closes[-6:])
    if mean_close is None:
        return None
    return _safe_div(statistics.mean(vals), mean_close)


def _spec(name: str, fn: Callable[[List[Bar]], Optional[float]], description: str) -> FactorSpec:
    return FactorSpec(name=name, weight=1.0,
                      fn=lambda market, symbol, bars, _fn=fn: _fn(bars),
                      description=description)


GTJA191_CANDIDATES: Tuple[FactorSpec, ...] = (
    _spec("gtja_003", _gtja_003, "6d conditional close-vs-extreme flow"),
    _spec("gtja_009", _gtja_009, "EWMA(7,2) mid-price acceleration / volume"),
    _spec("gtja_011", _gtja_011, "6d range-position weighted by volume"),
    _spec("gtja_023", _gtja_023, "20d up-day STD share"),
    _spec("gtja_040", _gtja_040, "26d up/down volume ratio"),
    _spec("gtja_042", _gtja_042, "-std(high,10) * corr(high,volume,10)"),
    _spec("gtja_049", _gtja_049, "12d down-day range share"),
    _spec("gtja_076", _gtja_076, "20d dispersion of |return|/volume"),
    _spec("gtja_079", _gtja_079, "12d RSI"),
    _spec("gtja_095", _gtja_095, "20d amount coefficient of variation"),
    _spec("gtja_097", _gtja_097, "10d volume coefficient of variation"),
    _spec("gtja_103", _gtja_103, "20d recency of low"),
    _spec("gtja_112", _gtja_112, "12d Chande momentum oscillator"),
    _spec("gtja_116", _gtja_116, "20d rolling OLS close slope"),
    _spec("gtja_127", _gtja_127, "12d RMS drawdown from rolling high"),
    _spec("gtja_188", _gtja_188, "range deviation from GTJA SMA(11,2)"),
    _spec("gtja_189", _gtja_189, "6d mean absolute deviation from MA6"),
)


def candidate_names() -> Tuple[str, ...]:
    return tuple(spec.name for spec in GTJA191_CANDIDATES)
