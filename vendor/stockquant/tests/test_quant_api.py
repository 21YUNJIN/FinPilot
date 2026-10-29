"""Read-only quant service and dashboard route tests."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from stockquant import dashboard as dash
from stockquant.domain import Bar, MarketData
from stockquant.quant_api import factor_snapshot, portfolio_risk, stress_test


def make_market(days=180):
    rows = []
    prices = {"000001.SZ": 12.0, "600000.SH": 9.0}
    for i in range(days):
        month = 1 + i // 28
        day = 1 + i % 28
        date = f"2024-{month:02d}-{day:02d}" if month <= 12 else f"2025-{month-12:02d}-{day:02d}"
        for symbol, base in prices.items():
            px = base + 0.01 * i + 0.04 * math.sin(i / 5.0)
            rows.append(Bar(date=date, symbol=symbol, open=px, high=px * 1.01,
                            low=px * 0.99, close=px, volume=1_000_000 + i * 1000,
                            market_cap=1e10))
    return MarketData.from_rows([b.__dict__ for b in rows])


class QuantApiTests(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.as_of = self.market.dates[-1]

    def test_factor_snapshot_resolves_symbol(self):
        out = factor_snapshot(self.market, "000001", self.as_of)
        self.assertEqual(out["symbol"], "000001.SZ")
        self.assertEqual(out["mode"], "read_only")
        self.assertTrue(math.isfinite(out["composite_score"]))

    def test_portfolio_risk_weights_sum_to_one(self):
        out = portfolio_risk(self.market, {"000001": 100, "600000": 100}, self.as_of)
        self.assertAlmostEqual(sum(out["weights"].values()), 1.0, places=9)
        self.assertGreater(out["total_value"], 0)

    def test_stress_test_reports_market_and_symbol_shocks(self):
        out = stress_test(
            self.market,
            {"000001": 100, "600000": 100},
            self.as_of,
            {"down10": {"market_shock": -0.10},
             "single": {"symbol_shocks": {"000001": -0.20}}},
        )
        self.assertEqual(len(out["scenarios"]), 2)
        self.assertLess(out["scenarios"][0]["scenario_value"], out["baseline"]["total_value"])


class QuantDashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.as_of = self.market.dates[-1]

    def test_factor_route(self):
        with patch.object(dash, "_get_market", return_value=self.market):
            r = dash.app.test_client().get(
                f"/api/quant/factor/000001?as_of={self.as_of}&dataset=csi300_full"
            )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["symbol"], "000001.SZ")

    def test_portfolio_and_stress_routes(self):
        payload = {"dataset": "csi300_full", "as_of": self.as_of,
                   "positions": {"000001": 100, "600000": 100}}
        with patch.object(dash, "_get_market", return_value=self.market):
            r1 = dash.app.test_client().post("/api/quant/portfolio-risk", json=payload)
            r2 = dash.app.test_client().post("/api/quant/stress-test", json=payload)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r1.get_json()["ok"])
        self.assertEqual(len(r2.get_json()["result"]["scenarios"]), 3)


if __name__ == "__main__":
    unittest.main()
