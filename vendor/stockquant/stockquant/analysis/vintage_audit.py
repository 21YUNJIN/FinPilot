"""Data-vintage and PIT audit for research claims.

This does not pretend that current-snapshot data is as-reported historical data.
It makes the limits explicit so a model cannot claim more evidence than the
dataset supports.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

from stockquant.domain import MarketData
from stockquant.factors.engine import PIT_FUNDAMENTAL_FIELDS


@dataclass
class VintageAudit:
    rows: int = 0
    symbols: int = 0
    first_date: str = ""
    last_date: str = ""
    fundamental_rows: int = 0
    stamped_fundamental_rows: int = 0
    available_at_rows: int = 0
    future_availability_rows: int = 0
    sectors: Dict[str, int] = None
    as_reported_vintage_available: bool = False
    historical_st_available: bool = False
    historical_industry_available: bool = False
    price_factor_claims_allowed: bool = True
    fundamental_factor_claims_allowed: bool = False
    blocking_reasons: list = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def audit_market(market: MarketData, source_path: Optional[str] = None) -> Dict[str, Any]:
    rows = 0
    fundamental_rows = 0
    stamped = 0
    available = 0
    future = 0
    sectors = Counter()
    symbols = set()
    for symbol, series in market.bars_by_symbol.items():
        symbols.add(symbol)
        for bar in series.values():
            rows += 1
            if bar.sector:
                sectors[bar.sector] += 1
            if bar.available_at:
                available += 1
            has_fund = any(getattr(bar, name, None) is not None for name in PIT_FUNDAMENTAL_FIELDS)
            if has_fund:
                fundamental_rows += 1
                stamp = str(bar.fundamentals_available_at or "").strip()
                if stamp:
                    stamped += 1
                    stamp_date = stamp[:10]
                    if stamp_date > str(bar.date)[:10]:
                        future += 1
    reasons = [
        "no as-reported/vintage id or revision chain in the dataset",
        "historical ST status is not versioned",
        "historical industry labels are current-vendor backfills",
    ]
    if available == 0:
        reasons.append("available_at is empty")
    if fundamental_rows and stamped < fundamental_rows:
        reasons.append("some fundamental rows lack fundamentals_available_at")
    if future:
        reasons.append("some availability stamps are later than the row date")
    result = VintageAudit(
        rows=rows,
        symbols=len(symbols),
        first_date=market.dates[0] if market.dates else "",
        last_date=market.dates[-1] if market.dates else "",
        fundamental_rows=fundamental_rows,
        stamped_fundamental_rows=stamped,
        available_at_rows=available,
        future_availability_rows=future,
        sectors=dict(sorted(sectors.items())),
        blocking_reasons=reasons,
    )
    payload = result.to_dict()
    payload["source_path"] = source_path
    payload["claim_policy"] = {
        "price_and_volume_factors": "allowed",
        "fundamental_factors": "research_only_until_as_reported_vintage_is_available",
        "historical_st_or_industry": "not_verifiable_with_current_dataset",
    }
    return payload


def write_audit(payload: Dict[str, Any], path: str) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


__all__ = ["VintageAudit", "audit_market", "write_audit"]
