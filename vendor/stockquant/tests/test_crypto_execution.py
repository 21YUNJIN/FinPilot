import os
import tempfile
import unittest

from stockquant.execution.crypto_execution import CryptoExecutionEngine, CryptoMonitorStrategy, CryptoPolicy
from stockquant.execution.engine import TradingContext
from stockquant.execution.risk import RiskLimits, RiskManager
from stockquant.execution.types import AccountSnapshot, Fill, OrderRequest, Position, Quote


class FakeCryptoBroker:
    def __init__(self, account, quotes):
        self.account = account
        self.quotes = quotes
        self.orders = []

    def get_account(self):
        return self.account

    def get_quotes(self, symbols):
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def submit_order(self, order, quote):
        self.orders.append(order)
        return Fill(order_id=str(len(self.orders)), symbol=order.symbol, side=order.side, quantity=order.quantity, price=quote.last)

    def cancel_all(self):
        return None


def quote(symbol="BTC/USDT", last=50000.0):
    return Quote(symbol=symbol, bid=last - 1.0, ask=last + 1.0, last=last, volume=1000.0, amount_step=0.00001)


class CryptoExecutionTests(unittest.TestCase):
    def test_strategy_buys_ready_symbol(self):
        account = AccountSnapshot(cash=1000.0, equity=1000.0, peak_equity=1000.0)
        q = quote()
        risk = RiskManager(RiskLimits(max_order_notional=20.0, max_position_weight=0.5), "/tmp/nonexistent-halt")
        context = TradingContext(
            account=account,
            quotes={q.symbol: q},
            daily_plan={"rows": [{"symbol": q.symbol, "status": "ready", "last": q.last, "reason": "trend"}]},
            broker=None,
            risk=risk,
        )
        policy = CryptoPolicy(target_weight=0.02, min_order_notional=1.0)
        orders = CryptoMonitorStrategy(policy).generate_orders(context)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, "buy")
        self.assertAlmostEqual(orders[0].quantity, 20.0 / q.ask, places=8)

    def test_strategy_sells_on_breakdown(self):
        q = quote()
        account = AccountSnapshot(
            cash=0.0,
            equity=500.0,
            positions={q.symbol: Position(symbol=q.symbol, quantity=0.01, average_cost=50000.0, market_price=q.last)},
            peak_equity=500.0,
        )
        risk = RiskManager(RiskLimits(), "/tmp/nonexistent-halt")
        context = TradingContext(
            account=account,
            quotes={q.symbol: q},
            daily_plan={"rows": [{"symbol": q.symbol, "status": "bearish", "last": q.last, "stop_loss": 49000.0, "take_profit": 60000.0}]},
            broker=None,
            risk=risk,
        )
        orders = CryptoMonitorStrategy(CryptoPolicy()).generate_orders(context)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, "sell")
        self.assertEqual(orders[0].quantity, 0.01)

    def test_engine_blocks_duplicate_buy_during_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            q = quote()
            account = AccountSnapshot(cash=1000.0, equity=1000.0, peak_equity=1000.0)
            broker = FakeCryptoBroker(account, {q.symbol: q})
            policy = CryptoPolicy(target_weight=0.02, min_order_notional=1.0, buy_cooldown_seconds=3600)
            risk = RiskManager(RiskLimits(max_order_notional=20.0, max_position_weight=0.5), os.path.join(td, "halt"), os.path.join(td, "risk.json"))
            engine = CryptoExecutionEngine(broker, risk, CryptoMonitorStrategy(policy), td, policy)
            payload = {"rows": [{"symbol": q.symbol, "status": "ready", "last": q.last}]}
            first = engine.run_once(payload)
            second = engine.run_once(payload)
            restarted = CryptoExecutionEngine(broker, risk, CryptoMonitorStrategy(policy), td, policy).run_once(payload)
            self.assertEqual(len(first["fills"]), 1)
            self.assertEqual(len(second["fills"]), 0)
            self.assertEqual(len(restarted["fills"]), 0)
            self.assertIn("buy cooldown", second["skipped"][0]["reason"])
            self.assertIn("buy cooldown", restarted["skipped"][0]["reason"])

    def test_risk_uses_crypto_amount_step(self):
        with tempfile.TemporaryDirectory() as td:
            risk = RiskManager(RiskLimits(max_order_notional=100.0), os.path.join(td, "halt"), os.path.join(td, "risk.json"))
            q = quote(last=50000.0)
            account = AccountSnapshot(cash=1000.0, equity=1000.0, peak_equity=1000.0)
            decision = risk.check_order(OrderRequest(q.symbol, "buy", 1.0), account, q)
            self.assertTrue(decision.allowed)
            self.assertAlmostEqual(decision.adjusted_quantity, 0.00199, places=8)

    def test_engine_skips_order_when_position_limit_reduces_quantity_to_zero(self):
        with tempfile.TemporaryDirectory() as td:
            q = quote(last=50000.0)
            account = AccountSnapshot(cash=1000.0, equity=1000.0, peak_equity=1000.0)
            broker = FakeCryptoBroker(account, {q.symbol: q})
            policy = CryptoPolicy(target_weight=0.02, min_order_notional=1.0)
            risk = RiskManager(
                RiskLimits(max_order_notional=100.0, max_position_weight=0.0),
                os.path.join(td, "halt"),
                os.path.join(td, "risk.json"),
            )
            engine = CryptoExecutionEngine(broker, risk, CryptoMonitorStrategy(policy), td, policy)
            result = engine.run_once({"rows": [{"symbol": q.symbol, "status": "ready", "last": q.last}]})
            self.assertEqual(result["orders_generated"], 1)
            self.assertEqual(result["fills"], [])
            self.assertIn("position limit", result["skipped"][0]["reason"])
            self.assertEqual(broker.orders, [])


if __name__ == "__main__":
    unittest.main()
