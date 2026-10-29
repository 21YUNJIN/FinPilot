"""Read-only StockQuant adapter tests using a synthetic market."""

from __future__ import annotations

import math
import unittest

from bankagent.quant_adapter.adapter import StockQuantAdapter
from stockquant.domain import Bar, MarketData


def make_market(days=180):
    rows = []
    prices = {"000001.SZ": 12.0, "600000.SH": 9.0, "510300.SH": 4.0}
    for i in range(days):
        month = 1 + i // 28
        day = 1 + i % 28
        date = f"2024-{month:02d}-{day:02d}" if month <= 12 else f"2025-{month-12:02d}-{day:02d}"
        for symbol, base in prices.items():
            px = base + 0.01 * i + 0.05 * math.sin(i / 4.0)
            rows.append(Bar(
                date=date, symbol=symbol, open=px, high=px * 1.01,
                low=px * 0.99, close=px, volume=1_000_000 + i * 1000,
                market_cap=1e10 if symbol != "510300.SH" else 1e11,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class QuantAdapterTests(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.adapter = StockQuantAdapter(market=self.market)
        self.as_of = self.market.dates[-1]

    def test_factor_snapshot_is_read_only_and_resolves_suffix(self):
        out = self.adapter.factor_snapshot("000001", self.as_of)
        self.assertEqual(out["mode"], "read_only")
        self.assertEqual(out["symbol"], "000001.SZ")
        self.assertIn("factor_z", out)
        self.assertIn("composite_score", out)
        self.assertTrue(math.isfinite(out["composite_score"]))

    def test_portfolio_risk_returns_weights_and_vol_key(self):
        out = self.adapter.portfolio_risk({"000001": 100, "600000": 200}, self.as_of)
        self.assertEqual(out["mode"], "read_only")
        self.assertAlmostEqual(sum(out["weights"].values()), 1.0, places=9)
        self.assertGreater(out["total_value"], 0)
        self.assertIn("annualized_volatility", out)

    def test_unknown_symbol_fails_closed(self):
        with self.assertRaises(ValueError):
            self.adapter.factor_snapshot("NOT_A_SYMBOL", self.as_of)
        with self.assertRaises(ValueError):
            self.adapter.portfolio_risk({"NOT_A_SYMBOL": 1}, self.as_of)


if __name__ == "__main__":
    unittest.main()
