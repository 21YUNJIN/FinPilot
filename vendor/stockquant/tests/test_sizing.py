import math
import unittest

from stockquant.domain import Bar, MarketData
from stockquant.portfolio import PortfolioConstraints
from stockquant.portfolio.optimizer import estimate_scenarios, optimize_target_weights, portfolio_cvar
from stockquant.portfolio.sizing import fractional_kelly_scale, garch11_volatility, kelly_weights

try:
    import numpy as np  # noqa: F401
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False


def make_market(days=140, seed=3):
    import random
    rng = random.Random(seed)
    px = {s: 10.0 for s in ["A", "B", "C", "D"]}
    rows = []
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        shock = rng.gauss(0, 0.02)
        for i, s in enumerate(px):
            r = shock + rng.gauss(0, 0.015)
            px[s] *= (1.0 + r)
            rows.append(Bar(date=date, symbol=s, open=px[s], high=px[s]*1.004,
                            low=px[s]*0.996, close=px[s], volume=200000,
                            sector="Tech", market_cap=1e9, pe=15, pb=1.5, roe=0.1))
    return MarketData.from_rows([b.__dict__ for b in rows])


class GarchTests(unittest.TestCase):
    def test_positive_and_reacts_to_vol(self):
        low = [0.001 * ((-1) ** i) for i in range(60)]
        high = [0.05 * ((-1) ** i) for i in range(60)]
        v_low = garch11_volatility(low)
        v_high = garch11_volatility(high)
        self.assertGreater(v_low, 0)
        self.assertGreater(v_high, v_low * 3)

    def test_empty_returns_zero(self):
        self.assertEqual(garch11_volatility([]), 0.0)


class KellyTests(unittest.TestCase):
    def test_fractional_kelly_scale(self):
        # mu=0.02, var=0.01 -> f*=2, half-Kelly=1.0, clipped to max 1.0
        self.assertAlmostEqual(fractional_kelly_scale(0.02, 0.01, fraction=0.5, max_scale=1.0), 1.0)
        # negative edge -> 0
        self.assertEqual(fractional_kelly_scale(-0.01, 0.01), 0.0)

    @unittest.skipUnless(HAS_NUMPY, "numpy required")
    def test_kelly_weights_long_only_and_capped(self):
        market = make_market()
        scores = {"A": 2.0, "B": 1.0, "C": 0.5, "D": -1.0}
        w = kelly_weights(market, scores, list(scores), market.dates[-1], cap=0.4, investable=0.97)
        self.assertTrue(w)
        self.assertTrue(all(v >= 0 for v in w.values()))
        self.assertTrue(all(v <= 0.4 + 1e-9 for v in w.values()))
        self.assertLessEqual(sum(w.values()), 0.97 + 1e-6)


@unittest.skipUnless(HAS_NUMPY, "numpy required")
class CvarTests(unittest.TestCase):
    def test_portfolio_cvar_is_a_tail_mean(self):
        scen = np.array([[0.01], [0.02], [-0.10], [-0.20], [0.03]])
        cvar = portfolio_cvar(scen, [1.0], alpha=0.4)
        # worst 40% (2 of 5) = [-0.20, -0.10] -> mean -0.15
        self.assertAlmostEqual(cvar, -0.15, places=6)

    def test_cvar_penalty_reduces_tail_exposure(self):
        market = make_market(seed=11)
        cons = PortfolioConstraints(max_positions=4, max_weight=1.0, sector_max_weight=1.0, cash_buffer=0.0, weight_scheme="optimize")
        scores = {"A": 2.0, "B": 1.5, "C": 1.0, "D": 0.5}
        bars = market.snapshot(market.dates[-1])
        scen, kept = estimate_scenarios(market, list(scores), market.dates[-1], lookback=120)
        base = optimize_target_weights(market, scores, bars, market.dates[-1], cons, alpha_scale=0.05, risk_aversion=2.0)
        guarded = optimize_target_weights(market, scores, bars, market.dates[-1], cons, alpha_scale=0.05,
                                          risk_aversion=2.0, cvar_alpha=0.25, cvar_lambda=20.0)

        def cvar(w):
            return portfolio_cvar(scen, [w.get(s, 0.0) for s in kept], alpha=0.25)

        # The tail-aware portfolio should not have a worse (more negative) CVaR.
        self.assertGreaterEqual(cvar(guarded), cvar(base) - 1e-9)


if __name__ == "__main__":
    unittest.main()
