import unittest

from stockquant.analysis.ml_baseline import collect_panel_with_meta, lightgbm_baseline
from stockquant.domain import Bar, MarketData


def make_market(days=160, symbols=6):
    rows = []
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            px = 10 + i + d * (0.02 + 0.005 * i)
            rows.append(Bar(
                date=date, symbol=f"S{i}", open=px * 0.999, high=px * 1.01,
                low=px * 0.99, close=px, volume=100000 + i * 1000,
                sector="Tech" if i < 3 else "Bank", market_cap=1e9 * (i + 1),
                pe=20 - i, pb=2 - 0.1 * i, roe=0.08 + 0.01 * i,
                revenue_growth=0.05 + 0.01 * i, profit_growth=0.03 + 0.01 * i,
                gross_margin=0.20 + 0.01 * i, forecast_growth=0.05,
                forecast_positive=0.5, holder_change=-0.01 + 0.005 * i,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class MLBaselineTests(unittest.TestCase):
    def test_collect_panel_with_meta_shape(self):
        market = make_market()
        names, X, y, meta = collect_panel_with_meta(
            market, market.dates[60], market.dates[-1], min_history=60, horizon=20, rebalance="monthly"
        )
        self.assertEqual(len(names), 10)
        self.assertEqual(len(X), len(y))
        self.assertEqual(len(X), len(meta))
        self.assertGreater(len(X), 0)
        self.assertEqual(len(X[0]), len(names))
        self.assertEqual(len(meta[0]), 2)  # (date, symbol)

    def test_baseline_reports_availability(self):
        market = make_market()
        result = lightgbm_baseline(
            market, market.dates[60], market.dates[-1], min_history=60, horizon=20, rebalance="monthly"
        )
        self.assertIn("available", result)
        if result["available"] is False:
            self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
