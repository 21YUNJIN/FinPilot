import math
import unittest

from stockquant.domain import Bar, MarketData
from stockquant.portfolio import PortfolioConstraints, build_target_weights
from stockquant.portfolio.optimizer import (
    estimate_covariance,
    optimize_target_weights,
    project_budget_box,
)

try:
    import numpy as np  # noqa: F401
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False


def make_correlated_market(days=120, seed=7):
    """S0/S1 nearly identical (high correlation); S2/S3 independent noise."""
    import random
    rng = random.Random(seed)
    base = 0.0
    rows = []
    px = {"S0": 10.0, "S1": 12.0, "S2": 8.0, "S3": 15.0}
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        base = 0.7 * base + rng.gauss(0, 0.01)
        common = base
        for s in ["S0", "S1", "S2", "S3"]:
            if s in ("S0", "S1"):
                r = common + rng.gauss(0, 0.001)
            else:
                r = rng.gauss(0, 0.02)
            px[s] *= (1.0 + r)
            rows.append(Bar(
                date=date, symbol=s, open=px[s], high=px[s] * 1.005,
                low=px[s] * 0.995, close=px[s], volume=200000,
                sector="Tech" if s in ("S0", "S1") else "Bank",
                market_cap=1e9, pe=15, pb=1.5, roe=0.1,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class ProjectionTests(unittest.TestCase):
    def test_projection_hits_budget_and_cap(self):
        w = project_budget_box([0.9, 0.8, 0.1, -0.2], budget=1.0, cap=0.4)
        self.assertAlmostEqual(sum(w), 1.0, places=6)
        self.assertTrue(all(-1e-9 <= x <= 0.4 + 1e-9 for x in w))

    def test_projection_handles_infeasible_cap(self):
        w = project_budget_box([1.0, 1.0, 1.0], budget=1.0, cap=0.2)  # 3*0.2 < 1.0
        self.assertEqual(w, [0.2, 0.2, 0.2])


@unittest.skipUnless(HAS_NUMPY, "numpy required")
class OptimizerTests(unittest.TestCase):
    def test_covariance_shape(self):
        market = make_correlated_market()
        cov, kept = estimate_covariance(market, market.symbols, market.dates[-1], lookback=60)
        self.assertIsNotNone(cov)
        self.assertEqual(len(kept), 4)
        self.assertEqual(cov.shape, (4, 4))

    def test_optimizer_respects_caps_and_budget(self):
        market = make_correlated_market()
        cons = PortfolioConstraints(max_positions=4, max_weight=0.4, sector_max_weight=0.6, cash_buffer=0.03, weight_scheme="optimize")
        scores = {"S0": 2.0, "S1": 1.5, "S2": 0.5, "S3": 0.2}
        bars = market.snapshot(market.dates[-1])
        w = optimize_target_weights(market, scores, bars, market.dates[-1], cons)
        self.assertLessEqual(sum(w.values()), 0.97 + 1e-6)
        self.assertTrue(all(v <= 0.4 + 1e-6 for v in w.values()))
        # sector cap: S0+S1 are both Tech
        tech = sum(v for s, v in w.items() if s in ("S0", "S1"))
        self.assertLessEqual(tech, 0.6 + 1e-6)

    def test_min_variance_optimizer_beats_score_heuristic_on_variance(self):
        market = make_correlated_market()
        cons = PortfolioConstraints(max_positions=4, max_weight=1.0, sector_max_weight=1.0, cash_buffer=0.0, weight_scheme="optimize")
        scores = {"S0": 2.0, "S1": 1.5, "S2": 0.5, "S3": 0.2}
        bars = market.snapshot(market.dates[-1])
        cov, kept = estimate_covariance(market, list(scores), market.dates[-1], lookback=60)
        idx = {s: i for i, s in enumerate(kept)}
        opt = optimize_target_weights(market, scores, bars, market.dates[-1], cons, alpha_scale=0.0, risk_aversion=50.0)
        heur = build_target_weights(scores, bars, PortfolioConstraints(max_positions=4, max_weight=1.0, sector_max_weight=1.0, cash_buffer=0.0))

        def var(w):
            v = np.array([w.get(s, 0.0) for s in kept])
            return float(v @ cov @ v)

        self.assertLess(var(opt), var(heur))


if __name__ == "__main__":
    unittest.main()
