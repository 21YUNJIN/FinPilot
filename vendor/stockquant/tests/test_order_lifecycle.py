"""Order lifecycle, kill-switch and reconciliation invariants."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stockquant.execution.order_lifecycle import (
    OrderStateMachine,
    OrderStatus,
    reconcile_cash,
    reconcile_order,
    reconcile_positions,
)
from stockquant.execution.risk import RiskLimits, RiskManager


class OrderStateMachineTests(unittest.TestCase):
    def test_idempotent_client_order_id(self):
        sm = OrderStateMachine()
        a = sm.create(symbol="A", side="buy", quantity=100, client_order_id="client-1")
        b = sm.create(symbol="A", side="buy", quantity=100, client_order_id="client-1")
        self.assertIs(a, b)
        self.assertRaises(ValueError, sm.create, symbol="B", side="buy", quantity=100,
                          client_order_id="client-1")

    def test_partial_then_filled(self):
        sm = OrderStateMachine()
        rec = sm.create(symbol="A", side="buy", quantity=100, client_order_id="client-2")
        sm.mark_submitted("client-2", "broker-2")
        sm.apply_fill("client-2", quantity=40, price=10.0, fill_id="f1")
        self.assertEqual(rec.status, OrderStatus.PARTIAL)
        self.assertEqual(rec.remaining_quantity, 60)
        sm.apply_fill("client-2", quantity=60, price=11.0, fill_id="f2")
        self.assertEqual(rec.status, OrderStatus.FILLED)
        self.assertAlmostEqual(rec.avg_fill_price, 10.6)

    def test_overfill_and_invalid_terminal_transition_rejected(self):
        sm = OrderStateMachine()
        sm.create(symbol="A", side="buy", quantity=100, client_order_id="client-3")
        sm.mark_submitted("client-3")
        with self.assertRaises(ValueError):
            sm.apply_fill("client-3", quantity=101, price=10)
        sm.apply_fill("client-3", quantity=100, price=10)
        with self.assertRaises(ValueError):
            sm.cancel("client-3")

    def test_reconciliation(self):
        self.assertTrue(reconcile_positions({"A": 100}, {"A": 100}).ok)
        self.assertFalse(reconcile_positions({"A": 100}, {"A": 99}).ok)
        self.assertTrue(reconcile_cash(1000, 1000.005).ok)
        sm = OrderStateMachine()
        rec = sm.create(symbol="A", side="buy", quantity=100, client_order_id="client-4")
        sm.mark_submitted("client-4")
        sm.apply_fill("client-4", quantity=100, price=10)
        self.assertTrue(reconcile_order(rec, [{"quantity": 100}]).ok)

    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as td:
            halt = Path(td) / "TRADING_HALTED"
            risk = RiskManager(RiskLimits(), str(halt))
            self.assertFalse(risk.halted())
            risk.halt("manual test")
            self.assertTrue(risk.halted())
            self.assertIn("manual", halt.read_text())
            risk.resume()
            self.assertFalse(risk.halted())


if __name__ == "__main__":
    unittest.main()
