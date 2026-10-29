import unittest

from stockquant.backtest.costs import (
    participation_rate,
    square_root_impact_bps,
    total_slippage_bps,
)


class CostModelTests(unittest.TestCase):
    def test_participation_rate(self):
        self.assertAlmostEqual(participation_rate(1e5, 1e6), 0.1)
        self.assertAlmostEqual(participation_rate(5e5, 1e6, cap=1.0), 0.5)
        self.assertAlmostEqual(participation_rate(1e6, 1e6, cap=0.10), 0.10)
        self.assertEqual(participation_rate(1e5, 0.0), 0.0)

    def test_impact_monotone_in_participation(self):
        self.assertLess(square_root_impact_bps(0.001), square_root_impact_bps(0.10))

    def test_total_slippage_grows_with_order_size(self):
        small = total_slippage_bps(1e4, 1e7, slippage_bps=5.0, impact_coefficient=25.0)
        large = total_slippage_bps(1e6, 1e7, slippage_bps=5.0, impact_coefficient=25.0)
        self.assertGreater(large, small)
        self.assertGreaterEqual(small, 5.0)

    def test_impact_disabled_returns_spread_only(self):
        out = total_slippage_bps(1e6, 1e7, slippage_bps=5.0, impact_coefficient=25.0, use_volume_impact=False)
        self.assertAlmostEqual(out, 5.0)


if __name__ == "__main__":
    unittest.main()
