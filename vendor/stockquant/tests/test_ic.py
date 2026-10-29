import unittest

from stockquant.analysis.ic import newey_west_t_stat, spearman, two_sided_pvalue


class ICTests(unittest.TestCase):
    def test_spearman_monotonic(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]), 1.0, places=10)

    def test_spearman_reverse(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]), -1.0, places=10)


if __name__ == "__main__":
    unittest.main()


class ICStatisticsTests(unittest.TestCase):
    def test_newey_west_autocorrelated_series(self):
        # Strong positive autocorrelation inflates the naive t-stat; Newey-West
        # should be more conservative than the naive estimator here.
        vals = []
        import random
        rng = random.Random(1)
        level = 0.0
        for _ in range(200):
            level = 0.8 * level + rng.gauss(0.002, 0.01)
            vals.append(level)
        nw = newey_west_t_stat(vals)
        naive = (__import__("statistics").mean(vals) /
                 __import__("statistics").pstdev(vals) * (len(vals) ** 0.5))
        self.assertLess(abs(nw), abs(naive))

    def test_two_sided_pvalue_zero_t_is_one(self):
        self.assertAlmostEqual(two_sided_pvalue(0.0), 1.0)


class ICDecayTests(unittest.TestCase):
    def test_ic_decay_shape(self):
        from stockquant.analysis.ic import analyze_ic_decay
        from stockquant.domain import Bar, MarketData
        rows = []
        for day in range(1, 160):
            for sym, base in [("A", 10), ("B", 20), ("C", 30)]:
                px = base + day * (0.03 if sym == "A" else 0.01)
                rows.append(Bar(
                    date=f"2024-{1 + (day-1)//28:02d}-{1 + (day-1)%28:02d}",
                    symbol=sym, open=px, high=px * 1.01, low=px * 0.99, close=px,
                    volume=100000, sector="Tech", market_cap=1e9,
                    pe=20, pb=2, roe=0.12,
                ))
        market = MarketData.from_rows([b.__dict__ for b in rows])
        decay = analyze_ic_decay(market, horizons=(5, 20), min_history=60, rebalance="monthly")
        self.assertEqual(set(decay), {"value", "quality", "momentum", "low_vol", "reversal", "liquidity", "growth", "gross_margin", "forecast", "holder"})
        for per_factor in decay.values():
            self.assertEqual(set(per_factor), {5, 20})


if __name__ == "__main__":
    unittest.main()
