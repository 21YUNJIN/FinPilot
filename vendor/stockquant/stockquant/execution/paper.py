from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Dict, List

from .broker import Broker
from .types import AccountSnapshot, Fill, OrderRequest, Position, Quote


class PaperBroker(Broker):
    def __init__(self, state_path: str, initial_cash: float = 1_000_000.0, commission_bps: float = 3.0, slippage_bps: float = 5.0, quote_provider=None):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.commission_bps = commission_bps
        self.slippage_bps = slippage_bps
        self.quote_provider = quote_provider
        if not self.path.exists():
            self._save({"cash": initial_cash, "initial_cash": initial_cash, "equity": initial_cash, "peak_equity": initial_cash, "positions": {}, "fills": []})

    def _load(self) -> Dict[str, object]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, payload: Dict[str, object]) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_account(self) -> AccountSnapshot:
        payload = self._load()
        positions = {
            s: Position(symbol=s, quantity=float(p["quantity"]), average_cost=float(p["average_cost"]), market_price=float(p.get("market_price", p["average_cost"])))
            for s, p in payload.get("positions", {}).items()
        }
        return AccountSnapshot(
            cash=float(payload["cash"]),
            equity=float(payload.get("equity", payload["cash"])),
            positions=positions,
            daily_pnl=float(payload.get("daily_pnl", 0.0)),
            peak_equity=float(payload.get("peak_equity", payload.get("equity", payload["cash"]))),
        )

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if self.quote_provider is None:
            raise RuntimeError("PaperBroker requires an external quote provider")
        return self.quote_provider.get_quotes(symbols)

    def submit_order(self, order: OrderRequest, quote: Quote) -> Fill:
        payload = self._load()
        side = order.side.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if order.quantity <= 0:
            raise ValueError("quantity must be positive")
        slip = self.slippage_bps / 10000.0
        exec_price = quote.ask * (1.0 + slip) if side == "buy" else quote.bid * (1.0 - slip)
        gross = exec_price * order.quantity
        commission = gross * self.commission_bps / 10000.0
        cash = float(payload["cash"])
        positions = payload.setdefault("positions", {})
        pos = positions.get(order.symbol, {"quantity": 0.0, "average_cost": 0.0})
        qty = float(pos["quantity"])
        avg = float(pos["average_cost"])
        if side == "buy":
            if gross + commission > cash:
                raise ValueError("insufficient paper cash")
            new_qty = qty + order.quantity
            avg = (qty * avg + gross) / new_qty if new_qty else 0.0
            positions[order.symbol] = {"quantity": new_qty, "average_cost": avg, "market_price": quote.last}
            cash -= gross + commission
        else:
            if order.quantity > qty + 1e-12:
                raise ValueError("insufficient paper position")
            cash += gross - commission
            new_qty = qty - order.quantity
            if new_qty <= 1e-12:
                positions.pop(order.symbol, None)
            else:
                positions[order.symbol] = {"quantity": new_qty, "average_cost": avg, "market_price": quote.last}
        equity = cash
        for symbol, p in positions.items():
            market_price = quote.last if symbol == order.symbol else float(p.get("market_price", p["average_cost"]))
            p["market_price"] = market_price
            equity += float(p["quantity"]) * market_price
        payload["cash"] = cash
        payload["equity"] = equity
        payload["peak_equity"] = max(float(payload.get("peak_equity", equity)), equity)
        fill = Fill(order_id=str(uuid.uuid4()), symbol=order.symbol, side=side, quantity=order.quantity, price=exec_price, reason=order.reason, strategy_id=order.strategy_id)
        payload.setdefault("fills", []).append(fill.__dict__)
        self._save(payload)
        return fill

    def cancel_all(self) -> None:
        return None
