import math
import unittest

from stockquant.execution.almgren_chriss import expected_cost, optimal_execution_schedule


class AlmgrenChrissTests(unittest.TestCase):
    def test_schedule_executes_full_quantity(self):
        s = optimal_execution_schedule(10000, horizon=1.0, n_steps=10, sigma=0.2, eta=1e-6, risk_aversion=1e-6)
        self.assertAlmostEqual(sum(s.trades), 10000, places=4)
        self.assertAlmostEqual(s.holdings[0], 10000, places=4)
        self.assertAlmostEqual(s.holdings[-1], 0.0, places=4)
        self.assertTrue(all(t >= -1e-9 for t in s.trades))

    def test_low_risk_aversion_is_twap(self):
        s = optimal_execution_schedule(1000, horizon=1.0, n_steps=5, sigma=0.2, eta=1e-6, risk_aversion=0.0)
        self.assertAlmostEqual(s.kappa, 0.0, places=9)
        for t in s.trades:
            self.assertAlmostEqual(t, 200.0, places=6)

    def test_high_risk_aversion_front_loads(self):
        calm = optimal_execution_schedule(10000, 1.0, 10, sigma=0.2, eta=1e-6, risk_aversion=1e-8)
        eager = optimal_execution_schedule(10000, 1.0, 10, sigma=0.2, eta=1e-6, risk_aversion=1e-4)
        self.assertLess(eager.kappa, 1e9)
        self.assertGreater(eager.trades[0], calm.trades[0])
        self.assertLess(eager.trades[-1], calm.trades[-1])

    def test_cost_scales_with_slice_size(self):
        fine = optimal_execution_schedule(10000, 1.0, 50, 0.2, 1e-6, 1e-6)
        coarse = optimal_execution_schedule(10000, 1.0, 5, 0.2, 1e-6, 1e-6)
        c_fine = expected_cost(fine, temporary_impact=1e-6)["total_cost"]
        c_coarse = expected_cost(coarse, temporary_impact=1e-6)["total_cost"]
        # Finer slicing lowers temporary impact cost.
        self.assertLess(c_fine, c_coarse)


if __name__ == "__main__":
    unittest.main()
