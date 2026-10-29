"""Dashboard ETF API must hide orders whenever the hard plan gate fails."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stockquant import dashboard as dash


class DashboardEtfGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig_root = dash.OUTPUTS_ROOT
        dash.OUTPUTS_ROOT = self.tmp
        base = self.tmp / "stockquant_etf"
        base.mkdir(parents=True)
        (base / "settings.json").write_text(json.dumps({
            "capital": 50000,
            "min_executable_capital": 50000,
        }), encoding="utf-8")

    def tearDown(self):
        dash.OUTPUTS_ROOT = self.orig_root

    def _write_plan(self, payload):
        base = self.tmp / "stockquant_etf"
        (base / "plan.json").write_text(json.dumps(payload), encoding="utf-8")

    def _ready_plan(self):
        return {
            "schema_version": 2,
            "plan_id": "etf-ready",
            "status": "ready",
            "execution_allowed": True,
            "data_fresh": True,
            "capital": 50000,
            "asof": "2026-10-16",
            "data_asof": "2026-10-16",
            "execute_on": "2099-01-02",
            "expires_at": "2099-01-02T09:35:00+08:00",
            "orders": [{"symbol": "510300", "side": "BUY", "shares": 100,
                        "ref_price": 4.0, "amount": 400.0, "target_weight": 0.1}],
            "targets": {"510300": 0.1},
            "prices": {"510300": 4.0},
        }

    def test_ready_plan_exposes_orders(self):
        self._write_plan(self._ready_plan())
        data = dash.app.test_client().get("/api/etf").get_json()
        self.assertTrue(data["executable"], data)
        self.assertEqual(len(data["orders"]), 1)

    def test_legacy_plan_hides_orders(self):
        self._write_plan({"asof": "2026-10-16", "execute_on": "next_trading_day_open",
                          "capital": 20000, "orders": [{"symbol": "510300", "side": "BUY", "shares": 100}]})
        data = dash.app.test_client().get("/api/etf").get_json()
        self.assertFalse(data["executable"])
        self.assertEqual(data["orders"], [])
        self.assertIn("legacy_plan", data["plan_gate"]["reasons"])

    def test_expired_plan_hides_orders(self):
        plan = self._ready_plan()
        plan["execute_on"] = "2020-01-02"
        plan["expires_at"] = "2020-01-02T09:35:00+08:00"
        self._write_plan(plan)
        data = dash.app.test_client().get("/api/etf").get_json()
        self.assertFalse(data["executable"])
        self.assertEqual(data["orders"], [])
        self.assertIn("execute_date_passed", data["plan_gate"]["reasons"])

    def test_expired_demo_plan_hides_paper_orders_too(self):
        plan = self._ready_plan()
        plan["status"] = "demo"
        plan["execution_allowed"] = False
        plan["execute_on"] = "2020-01-02"
        plan["expires_at"] = "2020-01-02T09:35:00+08:00"
        self._write_plan(plan)
        data = dash.app.test_client().get("/api/etf").get_json()
        self.assertFalse(data["executable"])
        self.assertEqual(data["paper_orders"], [])

    def test_demo_plan_hides_orders(self):
        plan = self._ready_plan()
        plan["status"] = "demo"
        plan["execution_allowed"] = False
        self._write_plan(plan)
        data = dash.app.test_client().get("/api/etf").get_json()
        self.assertFalse(data["executable"])
        self.assertEqual(data["orders"], [])
        self.assertEqual(len(data["paper_orders"]), 1)


if __name__ == "__main__":
    unittest.main()
