"""Human-in-the-loop broker gateway.

The gateway never pretends that a manual trade filled.  It creates a handoff
ticket, and the user later records the actual/paper fill from a broker statement.
This keeps discretionary human decisions separate from strategy-generated fills.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .broker_gateway import BrokerGateway, BrokerNotConfiguredError, BrokerStatus, ReadOnlyBrokerError
from .types import AccountSnapshot, Fill, OrderRequest, Position, Quote


class ManualExecutionRequired(RuntimeError):
    """Raised when an order was prepared but must be executed by a human."""

    def __init__(self, ticket: Dict[str, Any]):
        super().__init__(f"manual execution required: {ticket['manual_order_id']}")
        self.ticket = ticket


@dataclass
class ManualOrder:
    manual_order_id: str
    symbol: str
    side: str
    quantity: float
    limit_price: Optional[float] = None
    reference_price: float = 0.0
    status: str = "PENDING_MANUAL"
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    fills: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manual_order_id": self.manual_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "reference_price": self.reference_price,
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "remaining": self.remaining,
            "created_at": self.created_at,
            "fills": list(self.fills),
        }


class ManualBrokerGateway(BrokerGateway):
    """Virtual account + explicit manual fill recording.

    ``submit_order`` never auto-fills.  It creates a ticket and raises
    :class:`ManualExecutionRequired`; callers can then show the ticket to the
    user.  After the user executes in a broker app, call ``record_fill``.
    """

    provider = "manual"
    read_only = False

    def __init__(self, state_path: str, initial_cash: float = 100_000.0,
                 quote_provider=None, read_only: bool = False):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.quote_provider = quote_provider
        self.read_only = bool(read_only)
        self._state = self._load_or_create(float(initial_cash))
        self._status = BrokerStatus(provider=self.provider, connected=False,
                                    read_only=self.read_only, account_id="manual")

    def _load_or_create(self, initial_cash: float) -> Dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        state = {"cash": initial_cash, "initial_cash": initial_cash,
                 "positions": {}, "orders": {}, "fills": []}
        self._save(state)
        return state

    def _save(self, state: Optional[Dict[str, Any]] = None) -> None:
        if state is not None:
            self._state = state
        self.path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")

    def connect(self) -> BrokerStatus:
        if self.read_only:
            self._status.read_only = True
        self._status.connected = True
        self._status.message = "manual gateway connected; orders require human execution"
        return self._status

    def disconnect(self) -> None:
        self._status.connected = False

    def status(self) -> BrokerStatus:
        return self._status

    def get_account(self) -> AccountSnapshot:
        cash = float(self._state.get("cash", 0.0))
        positions = {}
        for symbol, p in (self._state.get("positions") or {}).items():
            qty = float(p.get("quantity", 0.0))
            avg = float(p.get("average_cost", 0.0))
            last = float(p.get("last_price", avg) or avg)
            positions[symbol] = Position(symbol=symbol, quantity=qty, average_cost=avg, market_price=last)
        equity = cash + sum(p.quantity * p.market_price for p in positions.values())
        return AccountSnapshot(cash=cash, equity=equity, positions=positions)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if self.quote_provider is None:
            raise BrokerNotConfiguredError("manual gateway requires a quote_provider")
        return self.quote_provider.get_quotes(symbols)

    def prepare_order(self, order: OrderRequest, quote: Quote) -> Dict[str, Any]:
        if self.read_only:
            raise ReadOnlyBrokerError("manual gateway is read-only")
        oid = "manual-" + uuid.uuid4().hex[:14]
        ticket = ManualOrder(oid, order.symbol, order.side.lower(), order.quantity,
                             order.limit_price, quote.last)
        self._state.setdefault("orders", {})[oid] = ticket.to_dict()
        self._save()
        return ticket.to_dict()

    def submit_order(self, order: OrderRequest, quote: Quote) -> Fill:
        ticket = self.prepare_order(order, quote)
        raise ManualExecutionRequired(ticket)

    def record_fill(self, manual_order_id: str, *, filled_quantity: float, fill_price: float,
                    commission: float = 0.0, filled_at: Optional[str] = None) -> Fill:
        orders = self._state.setdefault("orders", {})
        if manual_order_id not in orders:
            raise KeyError(f"unknown manual order: {manual_order_id}")
        order = orders[manual_order_id]
        qty = float(filled_quantity)
        px = float(fill_price)
        fee = max(0.0, float(commission))
        if not math.isfinite(qty) or qty <= 0 or not math.isfinite(px) or px <= 0:
            raise ValueError("filled_quantity and fill_price must be positive")
        remaining = float(order["quantity"]) - float(order.get("filled_quantity", 0.0))
        if qty > remaining + 1e-9:
            raise ValueError("fill exceeds remaining manual order quantity")
        cash = float(self._state["cash"])
        positions = self._state.setdefault("positions", {})
        symbol = str(order["symbol"])
        side = str(order["side"]).lower()
        pos = positions.get(symbol, {"quantity": 0.0, "average_cost": 0.0, "last_price": px})
        old_qty = float(pos.get("quantity", 0.0))
        old_avg = float(pos.get("average_cost", 0.0))
        gross = qty * px
        if side == "buy":
            if gross + fee > cash + 1e-9:
                raise ValueError("insufficient manual cash")
            new_qty = old_qty + qty
            new_avg = (old_qty * old_avg + gross) / new_qty if new_qty else 0.0
            positions[symbol] = {"quantity": new_qty, "average_cost": new_avg, "last_price": px}
            cash -= gross + fee
        elif side == "sell":
            if qty > old_qty + 1e-9:
                raise ValueError("insufficient manual position")
            new_qty = old_qty - qty
            cash += gross - fee
            if new_qty <= 1e-9:
                positions.pop(symbol, None)
            else:
                positions[symbol] = {"quantity": new_qty, "average_cost": old_avg, "last_price": px}
        else:
            raise ValueError("unsupported side")
        old_filled = float(order.get("filled_quantity", 0.0))
        old_avg = float(order.get("avg_fill_price", 0.0))
        order["filled_quantity"] = old_filled + qty
        order["avg_fill_price"] = (old_avg * old_filled + gross) / order["filled_quantity"]
        order["status"] = "FILLED" if order["filled_quantity"] >= float(order["quantity"]) - 1e-9 else "PARTIAL"
        fill = Fill(manual_order_id, symbol, side, qty, px,
                    timestamp_utc=filled_at or datetime.now(timezone.utc).isoformat(),
                    reason="manual fill")
        order.setdefault("fills", []).append(fill.__dict__)
        self._state.setdefault("fills", []).append(fill.__dict__)
        self._state["cash"] = cash
        self._save()
        return fill

    def cancel_all(self) -> None:
        for order in self._state.setdefault("orders", {}).values():
            if order.get("status") in {"PENDING_MANUAL", "PARTIAL"}:
                order["status"] = "CANCELLED"
        self._save()

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return [o for o in self._state.get("orders", {}).values()
                if o.get("status") in {"PENDING_MANUAL", "PARTIAL"}]


__all__ = ["ManualExecutionRequired", "ManualOrder", "ManualBrokerGateway"]
