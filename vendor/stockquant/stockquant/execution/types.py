from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float = 0.0
    timestamp_utc: str = field(default_factory=utc_now)
    source: str = "unknown"
    amount_step: float = 0.01

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid > 0 and self.ask > 0 else self.last

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return ((self.ask - self.bid) / mid * 10000.0) if mid > 0 else 0.0


@dataclass
class Position:
    symbol: str
    quantity: float
    average_cost: float
    market_price: float = 0.0


@dataclass
class AccountSnapshot:
    cash: float
    equity: float
    positions: Dict[str, Position] = field(default_factory=dict)
    daily_pnl: float = 0.0
    peak_equity: float = 0.0


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: Optional[float] = None
    reason: str = ""
    strategy_id: str = ""
    params: Dict[str, object] = field(default_factory=dict)


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    timestamp_utc: str = field(default_factory=utc_now)
    reason: str = ""
    strategy_id: str = ""


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    adjusted_quantity: float = 0.0
