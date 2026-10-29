import random
import unittest

from stockquant.analysis.neutralize import MODE_INDUSTRY, MODE_MEAN, neutralized_labels
from stockquant.domain import Bar, MarketData


def make_market(symbols=9, days=140):
    rows = []
    rng = random.Random(5)
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            sector = "Bank" if i < symbols // 2 else "Tech"
            px = 10 + i + d * 0.02 + rng.uniform(-0.05, 0.05)
            rows.append(Bar(date=date, symbol=f"S{i}", open=px, high=px * 1.01, low=px * 0.99,
                            close=px, volume=100000, sector=sector, market_cap=1e9,
                            pe=20, pb=2, roe=0.1, raw_close=px / 10,
                            revenue_growth=0.05, profit_growth=0.04, gross_margin=0.2,
                            forecast_growth=0.05, forecast_positive=0.5, holder_change=0.0))
    return MarketData.from_rows([b.__dict__ for b in rows])


class NeutralizeTests(unittest.TestCase):
    def test_industry_mode_sums_to_zero_within_sector(self):
        market = make_market()
        date, future = market.dates[100], market.dates[120]
        symbols = list(market.snapshot(date).keys())
        fwd = {s: 0.01 * (i + 1) for i, s in enumerate(symbols)}
        out = neutralized_labels(market, date, future, symbols, fwd, mode=MODE_INDUSTRY)
        for sector in ("Bank", "Tech"):
            vals = [out[s] for s in symbols if market.bar(s, date).sector == sector]
            self.assertAlmostEqual(sum(vals), 0.0, places=9)

    def test_mean_mode_removes_universe_mean(self):
        market = make_market()
        date, future = market.dates[100], market.dates[120]
        symbols = list(market.snapshot(date).keys())
        fwd = {s: 0.01 * (i + 1) for i, s in enumerate(symbols)}
        out = neutralized_labels(market, date, future, symbols, fwd, mode=MODE_MEAN)
        self.assertAlmostEqual(sum(out.values()), 0.0, places=9)

    def test_empty_input(self):
        self.assertEqual(neutralized_labels(make_market(), "2024-01-01", "2024-02-01", [], {}), {})


if __name__ == "__main__":
    unittest.main()
