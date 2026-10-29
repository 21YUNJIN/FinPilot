import random
import unittest

from stockquant.analysis.composite import composite_ic_series
from stockquant.domain import Bar, MarketData


def make_market(days=180, symbols=30):
    rows = []
    rng = random.Random(4)
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            # persistent cross-sectional signal: higher i => higher drift
            drift = 0.010 + 0.0008 * i
            px = 10 + i * 0.1 + d * drift + rng.uniform(-0.05, 0.05)
            rows.append(Bar(
                date=date, symbol=f"S{i}", open=px, high=px * 1.01, low=px * 0.99,
                close=px, volume=100000, sector="Tech" if i % 2 else "Bank",
                market_cap=1e9 * (i + 1), pe=20 - i / 30, pb=2 - i / 100, roe=0.1 + i / 300,
                revenue_growth=0.05 + i / 500, profit_growth=0.04 + i / 600,
                gross_margin=0.2 + i / 700, raw_close=px / 10,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class CompositeTests(unittest.TestCase):
    def test_composite_ic_has_signal(self):
        market = make_market()
        out = composite_ic_series(market, min_history=60, horizon=20, rebalance="monthly",
                                  impute_missing=True, orthogonalize=True)
        self.assertGreater(out["observations"], 0)
        self.assertGreater(out["mean_ic"], 0.01)
        self.assertGreater(out["positive_rate"], 0.5)

    def test_empty_market_is_safe(self):
        out = composite_ic_series(MarketData.from_rows([]), min_history=60)
        self.assertEqual(out["observations"], 0)


if __name__ == "__main__":
    unittest.main()
