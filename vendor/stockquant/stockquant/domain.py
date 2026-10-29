from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def _f(value: object, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _b(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


@dataclass(frozen=True)
class Bar:
    date: str
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    sector: str = "Unknown"
    market_cap: float = 0.0
    pe: Optional[float] = None
    pb: Optional[float] = None
    roe: Optional[float] = None
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    is_st: bool = False
    source: str = "unknown"
    raw_close: float = 0.0
    eps: Optional[float] = None
    bps: Optional[float] = None
    revenue_growth: Optional[float] = None
    profit_growth: Optional[float] = None
    gross_margin: Optional[float] = None
    forecast_growth: Optional[float] = None
    forecast_positive: Optional[float] = None
    holder_change: Optional[float] = None
    #: Availability of the *price/valuation row* itself (e.g. "2024-04-30 15:00:00").
    available_at: str = ""
    #: Availability of the announcement-derived fields below (eps/bps/roe/...).
    #: Date-only means "published that day, assume after the close"; a full
    #: timestamp is compared against the session close.  Empty means unknown,
    #: which the factor engine treats as unverifiable (never as 'known').
    fundamentals_available_at: str = ""

    @classmethod
    def from_row(cls, row: Dict[str, object]) -> "Bar":
        return cls(
            date=str(row["date"]),
            symbol=str(row["symbol"]).strip().upper(),
            open=_f(row.get("open")),
            high=_f(row.get("high")),
            low=_f(row.get("low")),
            close=_f(row.get("close")),
            volume=_f(row.get("volume")),
            sector=str(row.get("sector") or "Unknown"),
            market_cap=_f(row.get("market_cap")),
            pe=None if row.get("pe") in (None, "") else _f(row.get("pe")),
            pb=None if row.get("pb") in (None, "") else _f(row.get("pb")),
            roe=None if row.get("roe") in (None, "") else _f(row.get("roe")),
            suspended=_b(row.get("suspended")),
            limit_up=_b(row.get("limit_up")),
            limit_down=_b(row.get("limit_down")),
            is_st=_b(row.get("is_st")),
            source=str(row.get("source") or "csv"),
            raw_close=_f(row.get("raw_close"), _f(row.get("close"))),
            eps=None if row.get("eps") in (None, "") else _f(row.get("eps")),
            bps=None if row.get("bps") in (None, "") else _f(row.get("bps")),
            revenue_growth=None if row.get("revenue_growth") in (None, "") else _f(row.get("revenue_growth")),
            profit_growth=None if row.get("profit_growth") in (None, "") else _f(row.get("profit_growth")),
            gross_margin=None if row.get("gross_margin") in (None, "") else _f(row.get("gross_margin")),
            forecast_growth=None if row.get("forecast_growth") in (None, "") else _f(row.get("forecast_growth")),
            forecast_positive=None if row.get("forecast_positive") in (None, "") else _f(row.get("forecast_positive")),
            holder_change=None if row.get("holder_change") in (None, "") else _f(row.get("holder_change")),
            available_at=str(row.get("available_at") or ""),
            fundamentals_available_at=str(row.get("fundamentals_available_at") or ""),
        )

    def quote_fresh(self) -> bool:
        """A bar that carries an actual traded print (price AND volume).

        Zero volume on the daily grid is what a suspension, a trading halt or a
        forward-filled stale quote looks like after the vendors flatten intraday
        halts onto daily rows.  Treating it as tradable was the fail-open hole
        that let backtests "fill" orders on frozen prices.
        """
        return (
            not self.suspended
            and self.open > 0
            and self.close > 0
            and self.volume > 0
        )

    def tradable(self) -> bool:
        """Eligible for the long-selection universe (ST names excluded)."""
        return self.quote_fresh() and not self.is_st

    def can_buy(self) -> bool:
        """Can a *new* long position be opened on this bar?

        Limit-up bars cannot be bought: the order would sit unfilled at the
        close (we execute at the open, so this is the conservative reading).
        """
        return self.quote_fresh() and not self.is_st and not self.limit_up

    def can_sell(self) -> bool:
        """Can an existing position be reduced on this bar?

        Deliberately does *not* test ``is_st``: a name that turns ST must still
        be sellable.  Limit-down and suspended bars are treated as unsellable.
        """
        return self.quote_fresh() and not self.limit_down


@dataclass
class Position:
    symbol: str
    shares: int
    avg_cost: float
    entry_date: str
    last_buy_date: str


@dataclass
class Trade:
    date: str
    signal_date: str
    symbol: str
    side: str
    shares: int
    price: float
    gross_amount: float
    commission: float
    stamp_duty: float
    slippage_cost: float
    cash_after: float
    reason: str = ""


class DuplicateBarError(ValueError):
    """Raised when a (date, symbol) pair appears more than once in the input."""


@dataclass
class MarketData:
    bars_by_symbol: Dict[str, Dict[str, Bar]] = field(default_factory=dict)
    bars_by_date: Dict[str, Dict[str, Bar]] = field(default_factory=dict)
    membership_by_date: Dict[str, set] = field(default_factory=dict)
    # Integrity counters filled in by ``from_rows``.  Duplicates used to be
    # silently absorbed by the dict writes below, so a corrupted vendor file
    # could change a backtest without leaving a trace.
    duplicate_keys: int = 0
    duplicate_examples: List[str] = field(default_factory=list)
    rows_seen: int = 0
    # Caches so hot paths (history / snapshot / members_at) do not re-sort on
    # every call.  Populated lazily; invalidated never (the market is read-only
    # after construction in the research path).
    _symbol_sorted_dates: Dict[str, List[str]] = field(default_factory=dict, repr=False)
    _sorted_dates_cache: Optional[List[str]] = field(default=None, repr=False)
    _membership_dates_cache: Optional[List[str]] = field(default=None, repr=False)

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Dict[str, object]],
        strict_duplicates: bool = True,
        max_examples: int = 5,
    ) -> "MarketData":
        """Build a market from long-format rows.

        ``strict_duplicates=True`` (default) refuses to build a market when the
        same ``(date, symbol)`` appears twice.  Silently keeping "the last row"
        made vendor duplication invisible; callers that *want* last-wins
        semantics for a legacy file must opt in explicitly.
        """
        obj = cls()
        seen: Dict[Tuple[str, str], int] = {}
        for index, row in enumerate(rows):
            bar = Bar.from_row(row)
            key = (bar.date, bar.symbol)
            obj.rows_seen += 1
            if key in seen:
                obj.duplicate_keys += 1
                if len(obj.duplicate_examples) < max_examples:
                    obj.duplicate_examples.append(
                        f"{bar.date} {bar.symbol} (row #{seen[key] + 1} and #{index + 1})"
                    )
            else:
                seen[key] = index
            obj.bars_by_symbol.setdefault(bar.symbol, {})[bar.date] = bar
            obj.bars_by_date.setdefault(bar.date, {})[bar.symbol] = bar
        if obj.duplicate_keys and strict_duplicates:
            examples = ", ".join(obj.duplicate_examples)
            raise DuplicateBarError(
                f"{obj.duplicate_keys} duplicated (date, symbol) rows; "
                f"first examples: {examples}. "
                "De-duplicate the source (or pass strict_duplicates=False to keep last-wins)."
            )
        return obj

    @property
    def dates(self) -> List[str]:
        cache = getattr(self, '_sorted_dates_cache', None)
        if cache is None:
            cache = self._sorted_dates_cache = sorted(self.bars_by_date)
        return cache

    @property
    def symbols(self) -> List[str]:
        return sorted(self.bars_by_symbol)

    def bar(self, symbol: str, date: str) -> Optional[Bar]:
        return self.bars_by_symbol.get(symbol, {}).get(date)

    @property
    def membership_dates(self) -> List[str]:
        cache = getattr(self, '_membership_dates_cache', None)
        if cache is None or len(cache) != len(self.membership_by_date):
            cache = self._membership_dates_cache = sorted(self.membership_by_date)
        return cache

    def members_at(self, date: str) -> Optional[set]:
        """Index members in force on ``date``.

        Returns ``None`` only when the dataset carries no membership data at all
        ("no index filter requested").  When membership data exists but does not
        cover ``date`` this returns an **empty set**: before the first snapshot we
        know the membership is unknown, and the old code used to back-fill the
        first *future* snapshot into the past, which is a look-ahead bias that
        silently made early history use later index constituents.
        """
        if not self.membership_by_date:
            return None
        import bisect
        md = self.membership_dates
        idx = bisect.bisect_right(md, date) - 1
        if idx < 0:
            return set()
        return set(self.membership_by_date[md[idx]])

    @property
    def membership_span(self) -> Optional[Tuple[str, str]]:
        """(first, last) snapshot dates, or None when membership is absent."""
        md = self.membership_dates
        if not md:
            return None
        return md[0], md[-1]

    def uncovered_membership_dates(self, dates: Iterable[str]) -> List[str]:
        """Dates that precede the first membership snapshot (unknown universe)."""
        span = self.membership_span
        if span is None:
            return []
        first = span[0]
        return [d for d in dates if d < first]

    def snapshot(self, date: str, apply_membership: bool = True) -> Dict[str, Bar]:
        bars = dict(self.bars_by_date.get(date, {}))
        if apply_membership and self.membership_by_date:
            members = self.members_at(date)
            if members is not None:
                bars = {s: b for s, b in bars.items() if s in members}
        return bars

    def history(
        self,
        symbol: str,
        as_of: str,
        lookback: int,
        include_as_of: bool = True,
    ) -> List[Bar]:
        series = self.bars_by_symbol.get(symbol, {})
        cache = getattr(self, '_symbol_sorted_dates', None)
        if cache is None:
            cache = self._symbol_sorted_dates = {}
        sorted_dates = cache.get(symbol)
        if sorted_dates is None or len(sorted_dates) != len(series):
            sorted_dates = sorted(series)
            cache[symbol] = sorted_dates
        if include_as_of:
            import bisect
            idx = bisect.bisect_right(sorted_dates, as_of)
        else:
            import bisect
            idx = bisect.bisect_left(sorted_dates, as_of)
        return [series[d] for d in sorted_dates[max(0, idx - lookback):idx]]

    def date_slice(self, start: str, end: str) -> "MarketData":
        rows: List[Dict[str, object]] = []
        # Keep every snapshot up to ``end`` (including ones before ``start``) so
        # the as-of lookup still resolves inside the slice.  Dropping earlier
        # snapshots silently downgraded "index-filtered" into "no filter at all".
        membership = {d: set(v) for d, v in self.membership_by_date.items() if d <= end}
        for date in self.dates:
            if start <= date <= end:
                for bar in self.snapshot(date).values():
                    rows.append(
                        {
                            "date": bar.date,
                            "symbol": bar.symbol,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "sector": bar.sector,
                            "market_cap": bar.market_cap,
                            "pe": bar.pe,
                            "pb": bar.pb,
                            "roe": bar.roe,
                            "suspended": bar.suspended,
                            "limit_up": bar.limit_up,
                            "limit_down": bar.limit_down,
                            "is_st": bar.is_st,
                            "source": bar.source,
                            "raw_close": bar.raw_close,
                            "eps": bar.eps,
                            "bps": bar.bps,
                            "revenue_growth": bar.revenue_growth,
                            "profit_growth": bar.profit_growth,
                            "gross_margin": bar.gross_margin,
                            "forecast_growth": bar.forecast_growth,
                            "forecast_positive": bar.forecast_positive,
                            "holder_change": bar.holder_change,
                            "available_at": bar.available_at,
                            "fundamentals_available_at": bar.fundamentals_available_at,
                        }
                    )
        sliced = MarketData.from_rows(rows)
        sliced.membership_by_date = membership
        return sliced

    def previous_date(self, date: str) -> Optional[str]:
        dates = self.dates
        try:
            idx = dates.index(date)
        except ValueError:
            return None
        return dates[idx - 1] if idx > 0 else None

    def next_date(self, date: str) -> Optional[str]:
        dates = self.dates
        try:
            idx = dates.index(date)
        except ValueError:
            return None
        return dates[idx + 1] if idx + 1 < len(dates) else None

    def benchmark_daily_returns(self, start: Optional[str] = None) -> Tuple[List[str], List[float]]:
        dates = [d for d in self.dates if start is None or d >= start]
        returns: List[float] = []
        out_dates: List[str] = []
        for prev, cur in zip(dates, dates[1:]):
            prev_bars = self.snapshot(prev)
            cur_bars = self.snapshot(cur)
            vals: List[float] = []
            for symbol, prev_bar in prev_bars.items():
                cur_bar = cur_bars.get(symbol)
                if cur_bar is None:
                    continue
                if prev_bar.close > 0 and cur_bar.close > 0:
                    vals.append(cur_bar.close / prev_bar.close - 1.0)
            if vals:
                out_dates.append(cur)
                returns.append(sum(vals) / len(vals))
        return out_dates, returns


def parse_date(value: str) -> str:
    """Normalize common date strings to ISO YYYY-MM-DD."""
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    if "/" in value:
        parts = value.split("/")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return value[:10]
