from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .broker import Broker
from .engine import TradingContext
from .risk import RiskManager
from .types import Fill, OrderRequest


@dataclass(frozen=True)
class CryptoPolicy:
    symbols: Sequence[str] = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    target_weight: float = 0.02
    min_order_notional: float = 10.0
    buy_cooldown_seconds: int = 3600
    take_profit_fraction: float = 0.5
    strategy_id: str = "crypto_monitor_v1"


class CryptoMonitorStrategy:
    """Turn the public 1h trend monitor into explicitly risk-gated orders."""

    def __init__(self, policy: CryptoPolicy):
        self.policy = policy
        self.strategy_id = policy.strategy_id

    def generate_orders(self, context: TradingContext) -> List[OrderRequest]:
        rows = context.daily_plan.get("rows", [])
        positions = getattr(context.account, "positions", {})
        orders: List[OrderRequest] = []

        for row in rows:
            symbol = str(row.get("symbol", ""))
            quote = context.quotes.get(symbol)
            if not quote or quote.ask <= 0 or quote.bid <= 0:
                continue
            status = str(row.get("status", "watch"))
            signal_close = float(row.get("last") or quote.last or 0.0)
            current_price = quote.last if quote.last > 0 else signal_close
            stop_loss = float(row.get("stop_loss") or 0.0)
            take_profit = float(row.get("take_profit") or 0.0)
            position = positions.get(symbol)
            held_qty = float(position.quantity) if position else 0.0

            if held_qty > 0:
                # Exits are never delayed by the entry cooldown.  The risk
                # manager still has a chance to reject stale/abnormal quotes.
                if status == "bearish" or (stop_loss > 0 and current_price <= stop_loss):
                    orders.append(
                        OrderRequest(
                            symbol=symbol,
                            side="sell",
                            quantity=held_qty,
                            reason="trend breakdown / stop loss",
                            strategy_id=self.strategy_id,
                        )
                    )
                elif take_profit > 0 and current_price >= take_profit:
                    qty = held_qty * self.policy.take_profit_fraction
                    if qty > 0:
                        orders.append(
                            OrderRequest(
                                symbol=symbol,
                                side="sell",
                                quantity=qty,
                                reason="take profit",
                                strategy_id=self.strategy_id,
                            )
                        )
                continue

            if status != "ready":
                continue
            notional = min(
                context.account.equity * self.policy.target_weight,
                context.risk.limits.max_order_notional,
            )
            if notional < self.policy.min_order_notional:
                continue
            qty = notional / quote.ask
            orders.append(
                OrderRequest(
                    symbol=symbol,
                    side="buy",
                    quantity=qty,
                    reason=str(row.get("reason") or "trend entry"),
                    strategy_id=self.strategy_id,
                )
            )
        return orders


class CryptoExecutionEngine:
    """Small execution loop for crypto demo/paper testing.

    The engine persists buy cooldowns and take-profit state so a daemon cannot
    re-buy the same symbol on every polling interval.
    """

    def __init__(
        self,
        broker: Broker,
        risk: RiskManager,
        strategy: CryptoMonitorStrategy,
        state_dir: str,
        policy: CryptoPolicy,
        dry_run: bool = False,
    ):
        self.broker = broker
        self.risk = risk
        self.strategy = strategy
        self.policy = policy
        self.dry_run = dry_run
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "crypto_execution_state.json"
        self.log_path = self.state_dir / "crypto_execution_log.jsonl"

    def _load_state(self) -> Dict[str, object]:
        if not self.state_path.exists():
            return {"buy_times": {}, "take_profit_taken": []}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("buy_times", {})
                payload.setdefault("take_profit_taken", [])
                return payload
        except Exception:
            pass
        return {"buy_times": {}, "take_profit_taken": []}

    def _save_state(self, state: Dict[str, object]) -> None:
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _log(self, payload: Dict[str, object]) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _entry_allowed(self, symbol: str, state: Dict[str, object]) -> bool:
        if self.policy.buy_cooldown_seconds <= 0:
            return True
        raw = state.get("buy_times", {})
        try:
            last = float(raw.get(symbol, 0.0))  # type: ignore[union-attr]
        except Exception:
            last = 0.0
        return (time.time() - last) >= self.policy.buy_cooldown_seconds

    def run_once(self, monitor_payload: Dict[str, object]) -> Dict[str, object]:
        rows = monitor_payload.get("rows", [])
        if not isinstance(rows, list):
            raise ValueError("monitor payload must contain a rows list")
        symbols: List[str] = []
        for row in rows:
            symbol = str(row.get("symbol", ""))
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        if not symbols:
            symbols = [str(x) for x in self.policy.symbols]

        state = self._load_state()
        quotes = self.broker.get_quotes(symbols)
        account = self.broker.get_account()
        context = TradingContext(
            account=account,
            quotes=quotes,
            daily_plan=monitor_payload,
            broker=self.broker,
            risk=self.risk,
        )
        orders = self.strategy.generate_orders(context)
        fills: List[Fill] = []
        skipped: List[Dict[str, object]] = []
        taken = set(str(x) for x in state.get("take_profit_taken", []))  # type: ignore[arg-type]

        for order in orders:
            if order.side.lower() == "buy" and not self._entry_allowed(order.symbol, state):
                skipped.append({"symbol": order.symbol, "side": order.side, "reason": "buy cooldown active"})
                continue
            if order.side.lower() == "sell" and order.reason == "take profit" and order.symbol in taken:
                skipped.append({"symbol": order.symbol, "side": order.side, "reason": "take profit already taken"})
                continue
            quote = quotes.get(order.symbol)
            if quote is None:
                continue
            prices = {symbol: q.last for symbol, q in quotes.items()}
            decision = self.risk.check_order(order, account, quote, prices=prices)
            self._log({"event": "risk_decision", "order": order.__dict__, "decision": decision.__dict__})
            if not decision.allowed or decision.adjusted_quantity <= 0:
                skipped.append({"symbol": order.symbol, "side": order.side, "reason": decision.reason})
                continue
            order.quantity = decision.adjusted_quantity
            if self.dry_run:
                self._log({"event": "dry_run_order", "order": order.__dict__})
                continue
            try:
                fill = self.broker.submit_order(order, quote)
                fills.append(fill)
                self._log({"event": "fill", "fill": fill.__dict__})
                if order.side.lower() == "buy":
                    state.setdefault("buy_times", {})[order.symbol] = time.time()  # type: ignore[union-attr]
                elif order.reason == "take profit":
                    taken.add(order.symbol)
                    state["take_profit_taken"] = sorted(taken)
            except Exception as exc:
                self._log({"event": "order_error", "order": order.__dict__, "error": f"{type(exc).__name__}: {exc}"})
                skipped.append({"symbol": order.symbol, "side": order.side, "reason": f"{type(exc).__name__}: {exc}"})

        # Clear one-shot state once the position is gone.
        next_taken = set(str(x) for x in state.get("take_profit_taken", []))  # type: ignore[arg-type]
        for symbol in list(next_taken):
            position = getattr(account, "positions", {}).get(symbol)
            if position is None or float(position.quantity) <= 0:
                next_taken.discard(symbol)
        state["take_profit_taken"] = sorted(next_taken)
        self._save_state(state)
        return {
            "symbols": symbols,
            "orders_generated": len(orders),
            "fills": [f.__dict__ for f in fills],
            "skipped": skipped,
            "dry_run": self.dry_run,
            "state_file": str(self.state_path),
        }
