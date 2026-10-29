from __future__ import annotations

import dataclasses
import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from stockquant.domain import Bar, MarketData
from stockquant.factors import basic

logger = logging.getLogger(__name__)


class PITAvailabilityError(RuntimeError):
    """Raised when announcement data has no availability stamp (pit_policy='strict')."""


# Bar fields that originate from an *announcement* (annual/quarterly report,
# earnings forecast, shareholder-count update) rather than from the daily price
# grid.  They carry their own ``fundamentals_available_at`` stamp and must be
# gated by it, otherwise a report published after the close is used for that
# session's signal.
PIT_FUNDAMENTAL_FIELDS: Tuple[str, ...] = (
    "pe", "pb", "roe", "eps", "bps",
    "revenue_growth", "profit_growth", "gross_margin",
    "forecast_growth", "forecast_positive", "holder_change",
)


@dataclass(frozen=True)
class FactorSpec:
    name: str
    weight: float
    fn: Callable[[MarketData, str, List[Bar]], Optional[float]]
    description: str


def _factor_momentum(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.momentum(bars, lookback=60, skip=5)


def _factor_reversal(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.reversal(bars, lookback=5)


def _factor_low_vol(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.low_volatility(bars, lookback=60)


def _factor_liquidity(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.liquidity(bars, lookback=20)


def _factor_value(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.value_score(bars[-1])


def _factor_quality(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.quality(bars[-1])


def _factor_size(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.size(bars[-1])


def _factor_growth(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.growth(bars[-1])


def _factor_gross_margin(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.gross_margin(bars[-1])


def _factor_forecast(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.forecast_score(bars[-1])


def _factor_holder(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    return basic.holder_concentration(bars[-1])



def pit_usable(availability: str, session_date: str, session_close: str = "15:00:00") -> bool:
    """Is data stamped ``availability`` usable for a signal computed on ``session_date``?

    Two forms are accepted:

    * date only (``2024-04-30``): the stamp is a *publication date* without a
      time.  A-share periodic reports are routinely released after the close, so
      the conservative reading is that the data becomes usable on the **next**
      session: ``availability < session_date``.
    * date + time (``2024-04-30 11:00:00``): an explicit publication timestamp.
      It is usable on the same session only when it lands at or before the close.

    Anything blank is *not* usable: missing availability is exactly the case the
    old code treated as "assume it was known".
    """
    av = str(availability or "").strip()
    if not av:
        return False
    normalised = av.replace("T", " ")
    if " " in normalised:
        return normalised <= f"{session_date} {session_close}"
    return normalised < session_date


def gate_fundamentals(bar: "Bar", session_date: str, policy: str = "session") -> Tuple["Bar", bool]:
    """Return ``(bar, gated)`` with announcement fields removed when not yet known.

    ``policy="off"`` disables the check entirely (legacy behaviour).
    """
    if policy == "off":
        return bar, False
    if not any(getattr(bar, name, None) is not None for name in PIT_FUNDAMENTAL_FIELDS):
        return bar, False
    if pit_usable(bar.fundamentals_available_at, session_date):
        return bar, False
    return dataclasses.replace(
        bar, **{name: None for name in PIT_FUNDAMENTAL_FIELDS}
    ), True


_market_return_cache: Dict[Tuple[int, str, int], Dict[str, float]] = {}


def _equal_weight_market_returns(market: MarketData, date: str, lookback: int) -> Dict[str, float]:
    """Equal-weight market return keyed by the session the return was realized on.

    Keyed by date (instead of a bare list) so a stock's own return series can be
    aligned by *identity* rather than by array length: matching the last ``n``
    observations of two differently-gapped series pairs different sessions and
    corrupts the beta/IVOL estimate.
    """
    key = (id(market), date, lookback)
    cached = _market_return_cache.get(key)
    if cached is not None:
        return cached
    dates = [d for d in market.dates if d <= date][-(lookback + 1):]
    series: Dict[str, float] = {}
    for prev, cur in zip(dates, dates[1:]):
        prev_bars = market.snapshot(prev)
        cur_bars = market.snapshot(cur)
        rets = []
        for sym, a in prev_bars.items():
            b = cur_bars.get(sym)
            if b and a.close > 0 and b.close > 0 and a.quote_fresh() and b.quote_fresh():
                rets.append(b.close / a.close - 1.0)
        if rets:
            series[cur] = sum(rets) / len(rets)
    _market_return_cache[key] = series
    return series


def _factor_ivol(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    """Idiosyncratic volatility: annualized residual volatility of a single-index
    regression against the equal-weight universe.  Returned negative so that
    low-IVOL stocks rank high (consistent with the low-volatility factor)."""
    lookback = 60
    market_rets = _equal_weight_market_returns(market, bars[-1].date, lookback)
    stock_rets = basic.returns_by_date(bars[-(lookback + 1):])
    common = sorted(set(market_rets) & set(stock_rets))
    n = len(common)
    if n < 20:
        return None
    x = [market_rets[d] for d in common]
    y = [stock_rets[d] for d in common]
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / (n - 1) if n > 1 else 0.0
    varx = sum((a - mx) ** 2 for a in x) / (n - 1) if n > 1 else 0.0
    beta = cov / varx if varx > 1e-16 else 0.0
    resid = [b - beta * a for a, b in zip(x, y)]
    std = statistics.pstdev(resid) if len(resid) > 1 else 0.0
    return -std * math.sqrt(252.0)


def _factor_amihud(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    """Negative Amihud illiquidity.

    ``basic.amihud_illiquidity`` is *higher for less liquid* names.  The composite
    score treats larger values as better, so the raw value had the sign backwards
    and tilted the book toward the hardest names to trade (and, in the Chinese
    cross-section, toward the illiquidity/低流动性 premium's *worst* execution).
    Negating here makes the direction explicit and unit-testable.
    """
    value = basic.amihud_illiquidity(bars, lookback=20)
    return None if value is None else -value


def _factor_ou_reversal(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    """Ornstein-Uhlenbeck mean-reversion signal.

    Fit an OU/AR(1) process to log prices; return the *negative* standardised
    deviation from the long-run mean, so a level stretched above equilibrium
    ranks low (expected to revert)."""
    if len(bars) < 40:
        return None
    values = [math.log(b.close) for b in bars[-80:] if b.close > 0]
    if len(values) < 40:
        return None
    from stockquant.analysis.stochastic import ou_zscore
    z = ou_zscore(values)
    return None if z is None else -z


DEFAULT_FACTORS: Tuple[FactorSpec, ...] = (
    FactorSpec("value", 0.15, _factor_value, "Earnings yield + book yield"),
    FactorSpec("quality", 0.15, _factor_quality, "ROE"),
    FactorSpec("momentum", 0.20, _factor_momentum, "12-week momentum with a 1-week skip"),
    FactorSpec("low_vol", 0.12, _factor_low_vol, "Negative trailing 60-day realized volatility"),
    FactorSpec("reversal", 0.10, _factor_reversal, "Short-term reversal"),
    FactorSpec("liquidity", 0.05, _factor_liquidity, "Average traded value"),
    FactorSpec("growth", 0.18, _factor_growth, "Revenue and profit year-on-year growth"),
    FactorSpec("gross_margin", 0.05, _factor_gross_margin, "Gross margin"),
    FactorSpec("forecast", 0.10, _factor_forecast, "Earnings forecast direction and growth"),
    FactorSpec("holder", 0.05, _factor_holder, "Shareholder-count concentration change"),
)


# Candidate library (NOT part of the default model).  These are gated by the
# statistical-significance machinery in ``analysis.ic`` and must not be added to
# the production score without passing a multiple-testing-corrected bar.
def _factor_max_effect(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    """MAX effect (Bali, Cakici & Whitelaw 2011): stocks with high past extreme
    daily returns subsequently underperform (lottery demand). Returned negative
    so low-MAX names rank high."""
    if len(bars) < 22:
        return None
    rets = basic.daily_returns(bars[-22:])
    if not rets:
        return None
    return -max(rets)


def _factor_turnover(market: MarketData, symbol: str, bars: List[Bar]) -> Optional[float]:
    """Share turnover (volume / shares-outstanding), a Liu-Stambaugh-Yuan CH-3
    ingredient: low-turnover stocks earn a premium in China. Returned negative."""
    if len(bars) < 21:
        return None
    vals = []
    for b in bars[-20:]:
        if b.close > 0 and b.volume > 0 and b.market_cap > 0:
            shares_out = b.market_cap / b.close
            if shares_out > 0:
                vals.append(b.volume / shares_out)
    if len(vals) < 10:
        return None
    return -statistics.mean(vals)


EXTENDED_FACTORS: Tuple[FactorSpec, ...] = (
    FactorSpec("ou_reversal", 0.05, _factor_ou_reversal, "Neg OU deviation from long-run mean (stat-arb reversal)"),
    FactorSpec("max_effect", 0.05, _factor_max_effect, "Negative MAX (lottery demand)"),
    FactorSpec("turnover", 0.05, _factor_turnover, "Negative share turnover (low-turnover premium)"),
    FactorSpec("ivol", 0.10, _factor_ivol, "Negative idiosyncratic volatility (single-index residual)"),
    FactorSpec("amihud", 0.05, _factor_amihud, "Negative Amihud illiquidity (higher = more liquid)"),
    FactorSpec("size", 0.05, _factor_size, "Small-cap tilt (log market-cap)"),
)


def symmetric_orthogonalize(
    mappings: Dict[str, Dict[str, float]],
    factor_names: List[str],
) -> Dict[str, Dict[str, float]]:
    """Lowdin symmetric orthogonalization of the factor z-score matrix.

    Correlated factors (value / quality / growth overlap heavily) double-count
    the same exposure. Symmetric orthogonalization removes the redundancy while
    staying as close as possible to the original factors, and - unlike
    Gram-Schmidt - is order-independent. Requires numpy; returns the input
    unchanged when numpy is unavailable or there are too few complete cases.
    """
    try:
        import numpy as np
    except Exception:
        return mappings
    # Only consider factors that actually carry data; a factor with an empty
    # mapping (e.g. value when fundamentals are absent) would collapse the
    # complete-case intersection to nothing.
    names = [n for n in factor_names if mappings.get(n)]
    if len(names) < 2:
        return mappings
    # Complete cases = intersection over ALL factors. Using the first factor's
    # keys is wrong when that factor happens to be empty for the dataset
    # (e.g. value with no fundamentals), which silently disabled the step.
    common = None
    for name in names:
        keys = set(mappings[name].keys())
        common = keys if common is None else (common & keys)
    symbols = sorted(common or [])
    if len(symbols) < max(20, len(names) * 3):
        return mappings
    z = np.asarray([[mappings[n][s] for n in names] for s in symbols], dtype=float)
    # Drop zero-variance (constant) factors: they carry no information and make
    # the correlation matrix singular.
    keep = [j for j in range(z.shape[1]) if np.std(z[:, j]) > 1e-12]
    if len(keep) < 2:
        return mappings
    names = [names[j] for j in keep]
    z = z[:, keep]
    corr = np.corrcoef(z, rowvar=False)
    corr = np.nan_to_num(corr, nan=0.0, posinf=0.0, neginf=0.0)
    np.fill_diagonal(corr, 1.0)
    try:
        values, vectors = np.linalg.eigh(corr)
    except np.linalg.LinAlgError:
        return mappings
    values = np.clip(values, 1e-8, None)
    inv_sqrt = vectors @ np.diag(1.0 / np.sqrt(values)) @ vectors.T
    orth = z @ inv_sqrt
    out = {n: dict(mappings[n]) for n in names}
    for i, symbol in enumerate(symbols):
        for j, name in enumerate(names):
            out[name][symbol] = float(orth[i, j])
    return out


def _quantile(values: List[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def winsorized_zscore(values: List[float], lower_q: float = 0.05, upper_q: float = 0.95) -> List[float]:
    if not values:
        return []
    if len(values) == 1:
        return [0.0]
    ordered = sorted(values)
    def quantile(q: float) -> float:
        pos = q * (len(ordered) - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return ordered[lo]
        return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)
    lo = quantile(lower_q)
    hi = quantile(upper_q)
    clipped = [min(hi, max(lo, v)) for v in values]
    mean = sum(clipped) / len(clipped)
    variance = sum((v - mean) ** 2 for v in clipped) / len(clipped)
    std = math.sqrt(variance)
    if std <= 1e-12:
        return [0.0 for _ in clipped]
    return [(v - mean) / std for v in clipped]


@dataclass
class FactorDiagnostics:
    date: str
    universe_count: int
    scored_count: int
    missing_by_factor: Dict[str, int]
    sector_counts: Dict[str, int]
    #: Factor functions that raised.  These used to be swallowed by a bare
    #: ``except Exception`` and counted as "missing", which turned real code or
    #: schema bugs into invisible factor decay.
    error_by_factor: Dict[str, int] = field(default_factory=dict)
    #: Bars whose announcement fields were dropped because availability was
    #: unknown (policy="session") or because the report was not public yet.
    pit_gated: int = 0
    #: Rows that carried announcement fields with no availability stamp at all.
    pit_unverified: int = 0


class FactorEngine:
    def __init__(
        self,
        factors: Iterable[FactorSpec] = DEFAULT_FACTORS,
        min_history: int = 80,
        sector_neutral: bool = True,
        min_market_cap_quantile: float = 0.0,
        orthogonalize: bool = False,
        impute_missing: bool = False,
        pit_policy: str = "session",
        neutralize_by: str = "sector",
    ):
        self.factors = tuple(factors)
        self.min_history = min_history
        self.sector_neutral = sector_neutral
        self.min_market_cap_quantile = min_market_cap_quantile
        # P1-4: remove redundancy between correlated factors.
        self.orthogonalize = orthogonalize
        # P1-2: keep symbols with partial factor coverage (fill neutral 0)
        # instead of dropping them, which biases the universe toward
        # well-covered (usually larger) companies.
        self.impute_missing = impute_missing
        if pit_policy not in ("off", "session", "strict"):
            raise ValueError(f"unknown pit_policy: {pit_policy!r}")
        # P0-1: announcement data must be gated by its own availability stamp.
        #   "off"     - legacy behaviour, no gating (only counted).
        #   "session" - gate rows that declare availability; rows that declare
        #               nothing are used but reported as unverified.
        #   "strict"  - same as session, but *refuse* to run when announcement
        #               fields lack an availability stamp.
        self.pit_policy = pit_policy
        # How the cross-sectional neutralisation groups names:
        #   "sector"     - the ``sector`` label on each bar.  NOTE: for the vendor
        #                  files this is the *current* classification, back-filled
        #                  into history (no free historical industry series exists;
        #                  see docs/STOCK_LINE_REVIEW_2026-10-16.md §七).
        #   "cap_decile" - market-cap decile computed from that date's snapshot,
        #                  i.e. point-in-time clean and available for every name.
        #   "random:N[:seed]" - placebo diagnostic: N arbitrary groups.  If a random
        #                  grouping reproduces the sector-neutral uplift, the
        #                  uplift comes from the grouping mechanism, not from
        #                  industry information.
        #   "none"       - no neutralisation.
        self.neutralize_by = str(neutralize_by or "sector")
        #: Recent factor exceptions (bounded) for diagnostics/CLI output.
        self.error_log: List[str] = []

    def components(self, market: MarketData, date: str) -> Tuple[Dict[str, Dict[str, float]], FactorDiagnostics]:
        snapshot = market.snapshot(date)
        candidates: Dict[str, Dict[str, Optional[float]]] = {}
        sectors: Dict[str, str] = {}
        missing = {f.name: 0 for f in self.factors}
        errors = {f.name: 0 for f in self.factors}
        pit_gated = 0
        pit_unverified = 0

        market_caps: Dict[str, float] = {}
        for symbol, bar in snapshot.items():
            if not bar.tradable():
                continue
            bars = market.history(symbol, date, lookback=max(self.min_history, 120))
            if len(bars) < self.min_history:
                continue
            bars, gate_stats = self._gate_bars(bars, date)
            pit_gated += gate_stats["gated"]
            pit_unverified += gate_stats["unverified"]
            raw: Dict[str, Optional[float]] = {}
            valid = 0
            for spec in self.factors:
                try:
                    val = spec.fn(market, symbol, bars)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    errors[spec.name] += 1
                    self._record_error(date, symbol, spec.name, exc)
                    val = None
                if val is None or not math.isfinite(float(val)):
                    missing[spec.name] += 1
                    raw[spec.name] = None
                else:
                    valid += 1
                    raw[spec.name] = float(val)
            required = 1 if self.impute_missing else min(4, len(self.factors))
            if valid >= required:
                candidates[symbol] = raw
                sectors[symbol] = bar.sector
                market_caps[symbol] = bar.market_cap

        if self.pit_policy == "strict" and pit_unverified:
            raise PITAvailabilityError(
                f"{pit_unverified} bars on {date} carry announcement-derived fields "
                "without an availability stamp (fundamentals_available_at). "
                "Re-fetch the dataset with availability stamps, or run with pit_policy='session'/'off'."
            )

        if self.min_market_cap_quantile > 0.0 and market_caps:
            threshold = _quantile(list(market_caps.values()), self.min_market_cap_quantile)
            for symbol in [s for s in candidates if market_caps.get(s, 0.0) < threshold]:
                del candidates[symbol]
                del sectors[symbol]

        raw_z: Dict[str, Dict[str, float]] = {}
        for spec in self.factors:
            symbols = [s for s, vals in candidates.items() if vals.get(spec.name) is not None]
            zvals = winsorized_zscore([float(candidates[s][spec.name]) for s in symbols])
            raw_z[spec.name] = dict(zip(symbols, zvals))

        if self.impute_missing:
            # A missing factor is filled with the neutral value (0 in z space)
            # so the composite score is not silently renormalized on a
            # different factor subset for every stock.
            for spec in self.factors:
                mapping = raw_z.get(spec.name, {})
                for symbol in candidates:
                    mapping.setdefault(symbol, 0.0)
                raw_z[spec.name] = mapping

        if self.orthogonalize:
            raw_z = symmetric_orthogonalize(raw_z, [f.name for f in self.factors])

        if self.sector_neutral:
            groups = self._neutral_groups(candidates, sectors, market_caps)
            for factor, mapping in raw_z.items():
                by_group: Dict[str, List[Tuple[str, float]]] = {}
                for symbol, z in mapping.items():
                    by_group.setdefault(groups.get(symbol, "Unknown"), []).append((symbol, z))
                for items in by_group.values():
                    if len(items) < 2:
                        continue
                    mean = sum(z for _, z in items) / len(items)
                    for symbol, z in items:
                        mapping[symbol] = z - mean

        sector_counts: Dict[str, int] = {}
        for symbol in candidates:
            sec = sectors[symbol]
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        diag = FactorDiagnostics(
            date=date,
            universe_count=len(snapshot),
            scored_count=len(candidates),
            missing_by_factor=missing,
            sector_counts=sector_counts,
            error_by_factor={k: v for k, v in errors.items() if v},
            pit_gated=pit_gated,
            pit_unverified=pit_unverified,
        )
        return raw_z, diag

    def _neutral_groups(
        self,
        candidates: Dict[str, Dict[str, Optional[float]]],
        sectors: Dict[str, str],
        market_caps: Dict[str, float],
    ) -> Dict[str, str]:
        """Group label per symbol for the neutralisation step (see neutralize_by)."""
        mode = self.neutralize_by
        if mode in ("sector", "industry", ""):
            return {s: sectors.get(s, "Unknown") for s in candidates}
        if mode == "none":
            return {s: "ALL" for s in candidates}
        if mode.startswith("random"):
            # ``random:<buckets>`` or ``random:<buckets>:<seed>``
            parts = mode.split(":")
            buckets = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            seed = parts[2] if len(parts) > 2 else "0"
            import zlib
            return {
                s: f"g{zlib.crc32(f'{seed}:{s}'.encode()) % max(2, buckets)}"
                for s in candidates
            }
        if mode == "cap_decile":
            caps = sorted((market_caps.get(s, 0.0), s) for s in candidates)
            total = len(caps) or 1
            out: Dict[str, str] = {}
            for rank, (_cap, symbol) in enumerate(caps):
                out[symbol] = f"d{min(9, int(rank * 10 / total))}"
            return out
        raise ValueError(f"unknown neutralize_by: {self.neutralize_by!r}")

    def _gate_bars(self, bars: List[Bar], date: str) -> Tuple[List[Bar], Dict[str, int]]:
        """Apply the PIT availability gate to a symbol's history."""
        stats = {"gated": 0, "unverified": 0}
        if self.pit_policy == "off":
            return bars, stats
        out: List[Bar] = []
        changed = False
        for bar in bars:
            has_fields = any(getattr(bar, name, None) is not None for name in PIT_FUNDAMENTAL_FIELDS)
            if not has_fields:
                out.append(bar)
                continue
            if not str(bar.fundamentals_available_at or "").strip():
                stats["unverified"] += 1
                out.append(bar)  # no stamp -> policy-dependent; see components()
                continue
            gated_bar, gated = gate_fundamentals(bar, date, self.pit_policy)
            if gated:
                stats["gated"] += 1
                changed = True
            out.append(gated_bar)
        return (out if changed else bars), stats

    def _record_error(self, date: str, symbol: str, factor: str, exc: Exception) -> None:
        logger.warning("factor %s failed on %s/%s: %s: %s", factor, date, symbol, type(exc).__name__, exc)
        if len(self.error_log) < 50:
            self.error_log.append(f"{date} {symbol} {factor}: {type(exc).__name__}: {exc}")

    def score(self, market: MarketData, date: str) -> Tuple[Dict[str, float], FactorDiagnostics]:
        """Composite score.

        The divisor is the *book's* total factor weight, not the weight of the
        factors a given name happens to have.  Dividing by the per-name weight
        (``used``) let a stock that was missing, say, its negative-value z-score
        be renormalised upward: two names with different factor subsets were
        silently scored on different scales, and a missing factor acted as a
        free option.  Missing factors now contribute 0 against a fixed
        denominator, so under-coverage dilutes a score toward neutral.
        """
        raw_z, diag = self.components(market, date)
        scores: Dict[str, float] = {}
        total_weight = sum(abs(spec.weight) for spec in self.factors) or 1.0
        symbols = set()
        for mapping in raw_z.values():
            symbols.update(mapping)
        for symbol in symbols:
            score = 0.0
            for spec in self.factors:
                z = raw_z.get(spec.name, {}).get(symbol)
                if z is None:
                    continue
                score += spec.weight * z
            scores[symbol] = score / total_weight
        return scores, diag
