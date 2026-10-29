"""Production-oriented order lifecycle and reconciliation primitives.

This module does not talk to a broker.  It defines the state machine that a
broker adapter must drive, with explicit partial fills, terminal states and
client-order-id idempotency.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


_TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
_ALLOWED = {
    OrderStatus.NEW: {OrderStatus.SUBMITTED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED},
    OrderStatus.SUBMITTED: {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED},
    OrderStatus.PARTIAL: {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OrderRecord:
    client_order_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: Optional[float] = None
    strategy_id: str = ""
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    broker_order_id: str = ""
    fills: List[dict] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    reason: str = ""

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    def to_dict(self) -> Dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "strategy_id": self.strategy_id,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "broker_order_id": self.broker_order_id,
            "fills": list(self.fills),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }


class OrderStateMachine:
    """In-memory source of truth for order transitions.

    ``create`` is idempotent on ``client_order_id``.  A real broker adapter
    should call ``mark_submitted``, ``apply_fill`` and a terminal transition
    from its order/fill callback stream.
    """

    def __init__(self) -> None:
        self._orders: Dict[str, OrderRecord] = {}

    def create(self, *, symbol: str, side: str, quantity: float, order_type: str = "market",
               limit_price: Optional[float] = None, strategy_id: str = "",
               client_order_id: Optional[str] = None, reason: str = "") -> OrderRecord:
        side = str(side).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if not math.isfinite(float(quantity)) or quantity <= 0:
            raise ValueError("quantity must be positive")
        cid = str(client_order_id or f"ord-{uuid.uuid4().hex[:16]}")
        if cid in self._orders:
            existing = self._orders[cid]
            if (existing.symbol, existing.side, existing.quantity) != (symbol, side, float(quantity)):
                raise ValueError("client_order_id already exists with different order fields")
            return existing
        record = OrderRecord(cid, symbol, side, float(quantity), order_type, limit_price,
                             strategy_id, reason=reason)
        self._orders[cid] = record
        return record

    def get(self, client_order_id: str) -> Optional[OrderRecord]:
        return self._orders.get(client_order_id)

    def transition(self, client_order_id: str, target: OrderStatus,
                   *, broker_order_id: str = "", reason: str = "") -> OrderRecord:
        record = self._require(client_order_id)
        if target not in _ALLOWED[record.status]:
            raise ValueError(f"INVALID_ORDER_TRANSITION: {record.status.value} -> {target.value}")
        record.status = target
        record.updated_at = _now()
        if broker_order_id:
            record.broker_order_id = broker_order_id
        if reason:
            record.reason = reason
        return record

    def mark_submitted(self, client_order_id: str, broker_order_id: str = "") -> OrderRecord:
        return self.transition(client_order_id, OrderStatus.SUBMITTED, broker_order_id=broker_order_id)

    def reject(self, client_order_id: str, reason: str) -> OrderRecord:
        return self.transition(client_order_id, OrderStatus.REJECTED, reason=reason)

    def cancel(self, client_order_id: str, reason: str = "cancelled") -> OrderRecord:
        return self.transition(client_order_id, OrderStatus.CANCELLED, reason=reason)

    def expire(self, client_order_id: str, reason: str = "expired") -> OrderRecord:
        return self.transition(client_order_id, OrderStatus.EXPIRED, reason=reason)

    def apply_fill(self, client_order_id: str, *, quantity: float, price: float,
                   fill_id: str = "", ts: str = "") -> OrderRecord:
        record = self._require(client_order_id)
        if record.status not in {OrderStatus.SUBMITTED, OrderStatus.PARTIAL}:
            raise ValueError(f"ORDER_NOT_FILLABLE: {record.status.value}")
        qty = float(quantity)
        px = float(price)
        if not math.isfinite(qty) or qty <= 0 or not math.isfinite(px) or px <= 0:
            raise ValueError("fill quantity and price must be positive")
        if qty > record.remaining_quantity + 1e-9:
            raise ValueError("fill exceeds remaining order quantity")
        old_value = record.avg_fill_price * record.filled_quantity
        record.filled_quantity += qty
        record.avg_fill_price = (old_value + qty * px) / record.filled_quantity
        record.fills.append({"fill_id": fill_id or f"fill-{len(record.fills)+1}",
                             "quantity": qty, "price": px, "timestamp_utc": ts or _now()})
        record.status = (OrderStatus.FILLED
                         if record.remaining_quantity <= 1e-9 else OrderStatus.PARTIAL)
        record.updated_at = _now()
        return record

    def _require(self, client_order_id: str) -> OrderRecord:
        record = self._orders.get(client_order_id)
        if record is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        return record

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        return {cid: order.to_dict() for cid, order in self._orders.items()}


@dataclass
class ReconciliationResult:
    ok: bool
    differences: List[str]
    details: Dict[str, object] = field(default_factory=dict)


def reconcile_positions(expected: Mapping[str, float], actual: Mapping[str, float],
                       tolerance: float = 1e-6) -> ReconciliationResult:
    symbols = sorted(set(expected) | set(actual))
    diffs = []
    rows = {}
    for symbol in symbols:
        e, a = float(expected.get(symbol, 0.0)), float(actual.get(symbol, 0.0))
        delta = a - e
        rows[symbol] = {"expected": e, "actual": a, "delta": delta}
        if abs(delta) > tolerance:
            diffs.append(f"{symbol}: expected {e}, actual {a}, delta {delta}")
    return ReconciliationResult(not diffs, diffs, {"positions": rows, "tolerance": tolerance})


def reconcile_cash(expected: float, actual: float, tolerance: float = 0.01) -> ReconciliationResult:
    delta = float(actual) - float(expected)
    ok = abs(delta) <= tolerance
    return ReconciliationResult(ok, [] if ok else [f"cash delta {delta}"],
                                {"expected": expected, "actual": actual, "delta": delta, "tolerance": tolerance})


def reconcile_order(order: OrderRecord, broker_fills: Iterable[Mapping[str, object]]) -> ReconciliationResult:
    rows = list(broker_fills)
    broker_qty = sum(float(r.get("quantity") or 0.0) for r in rows)
    delta = broker_qty - order.filled_quantity
    ok = abs(delta) <= 1e-6
    return ReconciliationResult(ok, [] if ok else [f"fill quantity delta {delta}"],
                                {"client_order_id": order.client_order_id,
                                 "internal_filled": order.filled_quantity,
                                 "broker_filled": broker_qty,
                                 "broker_fill_count": len(rows)})


__all__ = ["OrderStatus", "OrderRecord", "OrderStateMachine", "ReconciliationResult",
           "reconcile_positions", "reconcile_cash", "reconcile_order"]
