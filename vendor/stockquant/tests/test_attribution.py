import random
import unittest

from stockquant.analysis.attribution import (
    attribute_returns,
    factor_long_short_series,
    style_attribution,
)
from stockquant.domain import Bar, MarketData


def make_market(days=220, symbols=12):
    rows = []
    rng = random.Random(3)
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            drift = 0.02 + 0.004 * i
            px = 10 + i + d * drift + rng.uniform(-0.1, 0.1)
            rows.append(Bar(
                date=date, symbol=f"S{i}", open=px * 0.999, high=px * 1.01,
                low=px * 0.99, close=px, volume=100000 + i * 1000,
                sector="Tech" if i < 6 else "Bank", market_cap=1e9 * (i + 1),
                pe=20 - i, pb=2 - 0.1 * i, roe=0.08 + 0.01 * i,
                revenue_growth=0.05, profit_growth=0.04, gross_margin=0.2,
                forecast_growth=0.05, forecast_positive=0.5, holder_change=0.0,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class AttributionTests(unittest.TestCase):
    def test_attribute_recovers_exposures(self):
        rng = random.Random(11)
        n = 120
        f1 = [rng.gauss(0.0, 0.02) for _ in range(n)]
        f2 = [rng.gauss(0.0, 0.01) for _ in range(n)]
        port = [0.5 * f1[i] + 0.2 * f2[i] + rng.gauss(0.0, 0.002) for i in range(n)]
        result = attribute_returns(port, {"f1": f1, "f2": f2})
        self.assertAlmostEqual(result["exposures"]["f1"], 0.5, delta=0.05)
        self.assertAlmostEqual(result["exposures"]["f2"], 0.2, delta=0.05)
        self.assertGreater(result["r_squared"], 0.9)

    def test_attribute_handles_short_series(self):
        result = attribute_returns([0.01, 0.02], {"f1": [0.01]})
        self.assertEqual(result["exposures"]["f1"], 0.0)
        self.assertEqual(result["observations"], 0.0)

    def test_factor_long_short_series_shape(self):
        market = make_market()
        series = factor_long_short_series(market, min_history=60, horizon=20, rebalance="monthly")
        self.assertIn("value", series)
        self.assertTrue(all(isinstance(pairs, list) for pairs in series.values()))

    def test_style_attribution_smoke(self):
        market = make_market()
        series = factor_long_short_series(market, min_history=60, horizon=20, rebalance="monthly")
        value_dates = [d for d, _ in series["value"]]
        if not value_dates:
            self.skipTest("no rebalance dates in synthetic market")
        port = {d: 0.01 for d in value_dates}
        result = style_attribution(market, port, min_history=60, horizon=20, rebalance="monthly")
        self.assertIn("exposures", result)
        self.assertIn("r_squared", result)


if __name__ == "__main__":
    unittest.main()


class EmptyFactorAlignmentTests(unittest.TestCase):
    def _price_only_market(self):
        import random
        rng = random.Random(5)
        rows = []
        for d in range(220):
            date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
            for i in range(12):
                px = 10 + i + d * (0.02 + 0.004 * i) + rng.uniform(-0.1, 0.1)
                rows.append(Bar(
                    date=date, symbol=f"S{i}", open=px * 0.999, high=px * 1.01,
                    low=px * 0.99, close=px, volume=100000 + i * 1000,
                    sector="Tech" if i < 6 else "Bank", market_cap=1e9 * (i + 1),
                ))
        return MarketData.from_rows([b.__dict__ for b in rows])

    def test_empty_factor_series_do_not_break_alignment(self):
        market = self._price_only_market()
        series = factor_long_short_series(market, min_history=60, horizon=20, rebalance="monthly")
        nonempty = [name for name, pairs in series.items() if pairs]
        self.assertTrue(nonempty)  # price-only factors still produced
        first_dates = [d for d, _ in series[nonempty[0]]]
        if not first_dates:
            self.skipTest("no rebalance dates")
        port = {d: 0.005 for d in first_dates}
        result = style_attribution(market, port, min_history=60, horizon=20, rebalance="monthly")
        self.assertGreater(result["observations"], 0)


class BetaNeutralTests(unittest.TestCase):
    def test_recovers_beta_and_hedges(self):
        from stockquant.analysis.attribution import beta_neutral_alpha
        import random
        rng = random.Random(21)
        n = 200
        market = [rng.gauss(0.0, 0.03) for _ in range(n)]
        port = [0.4 * market[i] + 0.002 + rng.gauss(0.0, 0.004) for i in range(n)]
        out = beta_neutral_alpha(port, market, periods_per_year=12)
        self.assertAlmostEqual(out["beta"], 0.4, delta=0.05)
        self.assertAlmostEqual(out["alpha_period"], 0.002, delta=0.002)
        self.assertEqual(len(out["hedged_returns"]), n)

    def test_short_series_is_safe(self):
        from stockquant.analysis.attribution import beta_neutral_alpha
        out = beta_neutral_alpha([0.01, 0.02], [0.01], periods_per_year=12)
        self.assertEqual(out["beta"], 0.0)
        self.assertEqual(out["observations"], 1.0)
