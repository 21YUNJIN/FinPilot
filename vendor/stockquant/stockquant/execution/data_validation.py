from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .types import Quote


@dataclass
class DataValidationResult:
    ok: bool
    issues: List[str]


def validate_quotes(quotes: Dict[str, Quote], required_symbols: Iterable[str]) -> DataValidationResult:
    issues = []
    for symbol in required_symbols:
        quote = quotes.get(symbol)
        if quote is None:
            issues.append(f"missing quote: {symbol}")
    if not quotes:
        issues.append("empty quote set")
    prices = {s: q.last for s, q in quotes.items()}
    for symbol, price in prices.items():
        if price <= 0:
            issues.append(f"non-positive price: {symbol}")
    return DataValidationResult(not issues, issues)
