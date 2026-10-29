import os
import tempfile
import unittest
from datetime import datetime, timezone

from stockquant.execution import PaperBroker, Quote, RiskLimits, RiskManager, StrategyRegistry
from stockquant.execution.types import AccountSnapshot, OrderRequest


class ExecutionTests(unittest.TestCase):
    def test_paper_broker_buy_sell(self):
        with tempfile.TemporaryDirectory() as td:
            broker = PaperBroker(os.path.join(td, "paper.json"), initial_cash=100000)
            q = Quote(symbol="TEST", bid=10.0, ask=10.1, last=10.05, volume=100000, timestamp_utc=datetime.now(timezone.utc).isoformat())
            fill = broker.submit_order(OrderRequest("TEST", "buy", 100, reason="test"), q)
            self.assertEqual(fill.side, "buy")
            account = broker.get_account()
            self.assertGreater(account.cash, 0)
            self.assertIn("TEST", account.positions)

    def test_risk_reduces_order(self):
        with tempfile.TemporaryDirectory() as td:
            risk = RiskManager(RiskLimits(max_order_notional=1000, max_quote_age_seconds=300), os.path.join(td, "halt"))
            q = Quote(symbol="TEST", bid=10.0, ask=10.01, last=10.005, volume=100000, timestamp_utc=datetime.now(timezone.utc).isoformat())
            account = AccountSnapshot(cash=0, equity=100000, peak_equity=100000)
            decision = risk.check_order(OrderRequest("TEST", "buy", 1000), account, q)
            self.assertTrue(decision.allowed)
            self.assertLess(decision.adjusted_quantity, 1000)

    def test_registry_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            registry = StrategyRegistry(os.path.join(td, "versions.json"))
            a = registry.register("a", {"x": 1}, {"sharpe": 1.0})
            b = registry.register("b", {"x": 2}, {"sharpe": 1.2})
            registry.promote(a["version_id"], "live")
            registry.promote(b["version_id"], "live")
            restored = registry.rollback()
            self.assertEqual(restored["version_id"], a["version_id"])


if __name__ == "__main__":
    unittest.main()
