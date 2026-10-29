import unittest

from stockquant.execution.policies import ExitPolicy, PositionView, evaluate_exit, evaluate_exits


class ExitPolicyTests(unittest.TestCase):
    def test_stop_loss_triggers(self):
        pos = PositionView("A", 100, average_cost=10.0)
        order = evaluate_exit(pos, 9.0, ExitPolicy(stop_loss_pct=0.08, take_profit_pct=0.20))
        self.assertIsNotNone(order)
        self.assertIn("stop_loss", order.reason)
        self.assertEqual(order.side, "sell")
        self.assertEqual(order.quantity, 100)

    def test_no_trigger_in_band(self):
        pos = PositionView("A", 100, average_cost=10.0)
        self.assertIsNone(evaluate_exit(pos, 10.5, ExitPolicy(stop_loss_pct=0.08, take_profit_pct=0.20)))

    def test_take_profit_triggers(self):
        pos = PositionView("A", 100, average_cost=10.0)
        order = evaluate_exit(pos, 12.5, ExitPolicy(stop_loss_pct=0.08, take_profit_pct=0.20))
        self.assertIsNotNone(order)
        self.assertIn("take_profit", order.reason)

    def test_trailing_stop_triggers_off_peak(self):
        pos = PositionView("A", 100, average_cost=10.0)
        # Price runs to 15 then pulls back 10% to 13.5.
        self.assertIsNone(evaluate_exit(pos, 15.0, ExitPolicy(stop_loss_pct=0.5, take_profit_pct=0.0, trailing_stop_pct=0.10)))
        order = evaluate_exit(pos, 13.5, ExitPolicy(stop_loss_pct=0.5, take_profit_pct=0.0, trailing_stop_pct=0.10))
        self.assertIsNotNone(order)
        self.assertIn("trailing_stop", order.reason)

    def test_limit_order_offset(self):
        pos = PositionView("A", 100, average_cost=10.0)
        policy = ExitPolicy(stop_loss_pct=0.08, use_limit_order=True, limit_offset_bps=20.0)
        order = evaluate_exit(pos, 9.0, policy)
        self.assertEqual(order.order_type, "limit")
        self.assertAlmostEqual(order.limit_price, 9.0 - 9.0 * 0.002, places=6)

    def test_evaluate_exits_across_portfolio(self):
        positions = {"A": PositionView("A", 100, 10.0), "B": PositionView("B", 50, 20.0)}
        orders = evaluate_exits(positions, {"A": 9.0, "B": 21.0}, ExitPolicy(stop_loss_pct=0.08, take_profit_pct=0.20))
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].symbol, "A")


if __name__ == "__main__":
    unittest.main()
