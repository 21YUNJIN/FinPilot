from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .broker import Broker
from .order_lifecycle import OrderStateMachine, OrderStatus
from .risk import RiskManager
from .types import Fill, OrderRequest, Quote


@dataclass
class TradingContext:
    account: object
    quotes: Dict[str, Quote]
    daily_plan: Dict[str, object]
    broker: Broker
    risk: RiskManager


class DailyPlanStrategy:
    def __init__(self, strategy_id: str = "daily_plan_v1"):
        self.strategy_id = strategy_id

    def generate_orders(self, context: TradingContext) -> List[OrderRequest]:
        plan = context.daily_plan
        account = context.account
        orders: List[OrderRequest] = []
        prices = {s: q.last for s, q in context.quotes.items()}
        holdings = {str(x["symbol"]): x for x in plan.get("holdings_analysis", [])}
        account_positions = getattr(account, "positions", {})
        for holding in plan.get("holdings_analysis", []):
            symbol = str(holding.get("symbol"))
            if symbol not in context.quotes:
                continue
            action = str(holding.get("action", ""))
            qty = float(holding.get("shares", 0.0))
            available_qty = float(account_positions.get(symbol).quantity) if symbol in account_positions else 0.0
            if "止损" in action or "退出" in action:
                sell_qty = min(qty, available_qty)
            elif "减仓" in action or "止盈" in action:
                sell_qty = min(qty * 0.5, available_qty)
            else:
                sell_qty = 0.0
            if sell_qty > 0:
                orders.append(OrderRequest(symbol=symbol, side="sell", quantity=round(sell_qty, 4), reason=action, strategy_id=self.strategy_id))
        for alert in plan.get("entry_alerts", {}).get("rows", []):
            if alert.get("status") != "ready":
                continue
            symbol = str(alert.get("symbol"))
            if symbol in holdings or symbol in account_positions or symbol not in context.quotes:
                continue
            target_weight = 0.02
            quote = context.quotes[symbol]
            notional = min(account.equity * target_weight, context.risk.limits.max_order_notional)
            qty = round(notional / quote.ask, 4) if quote.ask > 0 else 0.0
            if qty > 0:
                orders.append(OrderRequest(symbol=symbol, side="buy", quantity=qty, reason=alert.get("reason", "entry_alert"), strategy_id=self.strategy_id))
        return orders


class TradingEngine:
    def __init__(self, broker: Broker, risk: RiskManager, strategy: DailyPlanStrategy, state_dir: str,
                 order_state: Optional[OrderStateMachine] = None):
        self.broker = broker
        self.risk = risk
        self.strategy = strategy
        self.state_dir = Path(state_dir)
        self.log_path = self.state_dir / "execution_log.jsonl"
        self.order_state = order_state or OrderStateMachine()

    def _load_daily_plan(self) -> Dict[str, object]:
        path = self.state_dir / "daily_plan.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _log(self, payload: Dict[str, object]) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def run_once(self, symbols: Optional[List[str]] = None) -> List[Fill]:
        plan = self._load_daily_plan()
        if not plan:
            return []
        symbols = symbols or sorted({str(x["symbol"]) for x in plan.get("holdings_analysis", [])} | {str(x["symbol"]) for x in plan.get("entry_alerts", {}).get("rows", []) if x.get("status") == "ready"})
        quotes = self.broker.get_quotes(symbols)
        account = self.broker.get_account()
        context = TradingContext(account=account, quotes=quotes, daily_plan=plan, broker=self.broker, risk=self.risk)
        orders = self.strategy.generate_orders(context)
        fills = []
        for order in orders:
            quote = quotes.get(order.symbol)
            if quote is None:
                continue
            decision = self.risk.check_order(order, account, quote, prices={s: q.last for s, q in quotes.items()})
            self._log({"event": "risk_decision", "order": order.__dict__, "decision": decision.__dict__})
            if not decision.allowed or decision.adjusted_quantity <= 0:
                continue
            order.quantity = decision.adjusted_quantity
            client_id = str(order.params.get("client_order_id") or "") or None
            record = self.order_state.create(
                symbol=order.symbol, side=order.side, quantity=order.quantity,
                order_type=order.order_type, limit_price=order.limit_price,
                strategy_id=order.strategy_id, client_order_id=client_id,
                reason=order.reason,
            )
            if record.status != OrderStatus.NEW:
                self._log({"event": "order_idempotent_skip",
                           "order": record.to_dict()})
                continue
            self.order_state.mark_submitted(record.client_order_id)
            self._log({"event": "order_submitted", "order": self.order_state.get(record.client_order_id).to_dict()})
            try:
                fill = self.broker.submit_order(order, quote)
                self.order_state.apply_fill(record.client_order_id, quantity=fill.quantity,
                                            price=fill.price, fill_id=fill.order_id,
                                            ts=fill.timestamp_utc)
                fills.append(fill)
                self._log({"event": "fill", "fill": fill.__dict__,
                           "order_state": self.order_state.get(record.client_order_id).to_dict()})
            except Exception as exc:
                self.order_state.reject(record.client_order_id, f"{type(exc).__name__}: {exc}")
                self._log({"event": "order_error", "order": order.__dict__,
                           "order_state": self.order_state.get(record.client_order_id).to_dict(),
                           "error": f"{type(exc).__name__}: {exc}"})
        return fills
