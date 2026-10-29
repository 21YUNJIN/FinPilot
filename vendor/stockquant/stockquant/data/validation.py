from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from stockquant.domain import MarketData


@dataclass
class ValidationReport:
    rows: int = 0
    symbols: int = 0
    dates: int = 0
    duplicate_keys: int = 0
    negative_prices: int = 0
    invalid_ohlc: int = 0
    negative_volume: int = 0
    missing_sector: int = 0
    # Bars with no traded volume that are not flagged as suspended: the vendor
    # grid says "trading day" but nothing traded.  Backtests must not fill on
    # those prices, and silently treating them as normal rows hid halts.
    stale_or_halted: int = 0
    #: Single-name market caps above ~10 trillion CNY: no A-share is that large,
    #: so this is a construction error (typically adjusted price x shares).
    implausible_market_cap: int = 0
    membership_snapshots: int = 0
    membership_first_date: str = ""
    membership_last_date: str = ""
    issues: List[str] = field(default_factory=list)
    duplicate_examples: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(
            [
                self.duplicate_keys,
                self.negative_prices,
                self.invalid_ohlc,
                self.negative_volume,
            ]
        )

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "rows": self.rows,
            "symbols": self.symbols,
            "dates": self.dates,
            "duplicate_keys": self.duplicate_keys,
            "negative_prices": self.negative_prices,
            "invalid_ohlc": self.invalid_ohlc,
            "negative_volume": self.negative_volume,
            "missing_sector": self.missing_sector,
            "stale_or_halted": self.stale_or_halted,
            "membership_snapshots": self.membership_snapshots,
            "membership_first_date": self.membership_first_date,
            "membership_last_date": self.membership_last_date,
            "ok": self.ok,
            "issues": list(self.issues),
        }
        return out


def validate_market(market: MarketData) -> ValidationReport:
    report = ValidationReport(
        rows=sum(len(v) for v in market.bars_by_date.values()),
        symbols=len(market.symbols),
        dates=len(market.dates),
        duplicate_keys=int(getattr(market, "duplicate_keys", 0) or 0),
        duplicate_examples=list(getattr(market, "duplicate_examples", []) or []),
    )
    for date, bars in market.bars_by_date.items():
        if not bars:
            report.issues.append(f"No bars on {date}")
        for symbol, bar in bars.items():
            if min(bar.open, bar.high, bar.low, bar.close) <= 0 and not bar.suspended:
                report.negative_prices += 1
            if not bar.suspended:
                if bar.high + 1e-9 < max(bar.open, bar.close) or bar.low - 1e-9 > min(bar.open, bar.close):
                    report.invalid_ohlc += 1
            if bar.volume < 0:
                report.negative_volume += 1
            if bar.volume == 0 and not bar.suspended:
                report.stale_or_halted += 1
            if bar.market_cap > 1e13:  # 10 trillion CNY
                report.implausible_market_cap += 1
            if not bar.sector or bar.sector == "Unknown":
                report.missing_sector += 1

    span = market.membership_span
    if span is not None:
        report.membership_snapshots = len(market.membership_by_date)
        report.membership_first_date, report.membership_last_date = span
        uncovered = market.uncovered_membership_dates(market.dates)
        if uncovered:
            report.issues.append(
                f"{len(uncovered)} trading dates precede the first membership snapshot "
                f"({span[0]}); the index universe is unknown there and is treated as empty"
            )

    if report.duplicate_keys:
        report.issues.append(
            f"{report.duplicate_keys} duplicated (date, symbol) rows "
            f"(first examples: {', '.join(report.duplicate_examples)})"
        )
    if report.negative_prices:
        report.issues.append(f"{report.negative_prices} rows have non-positive prices")
    if report.invalid_ohlc:
        report.issues.append(f"{report.invalid_ohlc} rows violate OHLC bounds")
    if report.negative_volume:
        report.issues.append(f"{report.negative_volume} rows have negative volume")
    if report.stale_or_halted:
        report.issues.append(
            f"{report.stale_or_halted} rows carry zero volume without a suspension flag "
            "(treated as non-tradable by Bar.quote_fresh)"
        )
    if report.implausible_market_cap:
        report.issues.append(
            f"{report.implausible_market_cap} rows carry a market cap above 10 trillion CNY "
            "(check that market_cap is raw_close x shares, not an adjusted price x shares)"
        )
    if report.missing_sector:
        report.issues.append(f"{report.missing_sector} rows have missing sector")
    return report
