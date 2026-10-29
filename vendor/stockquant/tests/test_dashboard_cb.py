"""看板转债部分：入金不能当盈利、出金不能当回撤。"""

import unittest

from stockquant import dashboard as dash


class NavNormalisationTests(unittest.TestCase):
    def setUp(self):
        self._orig = dash._nav_series

    def tearDown(self):
        dash._nav_series = self._orig

    def test_nav_uses_value_over_cost(self):
        dash._nav_series = lambda: [
            {"date": "2026-10-01", "value": 10000.0, "cost": 10000.0},
            {"date": "2026-10-02", "value": 11000.0, "cost": 10000.0},
            {"date": "2026-10-03", "value": 22000.0, "cost": 20000.0},  # 加钱 1 万
        ]
        pts = dash._nav_points()
        self.assertAlmostEqual(pts[0]["nav"], 1.0)
        self.assertAlmostEqual(pts[1]["nav"], 1.1)
        # 加钱后净值仍是 1.1（而不是"忽然翻倍"）
        self.assertAlmostEqual(pts[2]["nav"], 1.1)

    def test_legacy_records_without_cost_degrade_gracefully(self):
        dash._nav_series = lambda: [{"date": "2026-10-01", "value": 10000.0}]
        pts = dash._nav_points()
        self.assertIsNone(pts[0]["nav"])
        self.assertEqual(pts[0]["value"], 10000.0)

    def test_zero_value_records_dropped(self):
        dash._nav_series = lambda: [{"date": "d", "value": 0.0, "cost": 100.0}]
        self.assertEqual(dash._nav_points(), [])


class CbPositionHelpersTests(unittest.TestCase):
    def test_is_cb_code(self):
        from stockquant.convertible.positions import is_cb_code
        self.assertTrue(is_cb_code("113046"))
        self.assertTrue(is_cb_code(128134))
        self.assertFalse(is_cb_code("600519"))
        self.assertFalse(is_cb_code("000651.SZ"))
        self.assertFalse(is_cb_code(None))

    def test_params_follow_report(self):
        orig = dash._cb_report
        dash._cb_report = lambda: {"params": {"top_n": 20, "min_amount": 5e6, "min_price": 98.0}}
        try:
            p = dash._cb_params()
        finally:
            dash._cb_report = orig
        self.assertEqual(p["top_n"], 20)
        self.assertEqual(p["min_amount"], 5e6)
        self.assertEqual(p["min_price"], 98.0)
        self.assertIn("max_drop_20d", p)   # 报告没给的用默认值补齐


if __name__ == "__main__":
    unittest.main()


class CbPaperDedupeTests(unittest.TestCase):
    """同一周重复保存不能把滑点重复计入（2026-10-13 实际踩到的坑）。"""

    def test_same_week_kept_once(self):
        import json, tempfile
        from pathlib import Path
        from stockquant import dashboard as dash

        tmp = Path(tempfile.mkdtemp())
        orig_state = dash.STATE_DIR
        orig_report = dash._cb_report
        dash.STATE_DIR = tmp
        dash._cb_report = lambda: {"picks_asof": "2026-10-12", "picks": [
            {"bond_code": "113042", "bond_name": "上银转债", "price": 117.14}]}
        try:
            pth = tmp / "cb_paper.jsonl"
            with pth.open("w", encoding="utf-8") as f:
                # 同一周两条记录：第一条错（未成交），第二条是修正后的
                f.write(json.dumps({"week": "2026-10-12", "fills": [
                    {"code": "113042", "model_price": 117.14, "actual_price": 100.0, "filled": False}]},
                    ensure_ascii=False) + "\n")
                f.write(json.dumps({"week": "2026-10-12", "fills": [
                    {"code": "113042", "model_price": 117.14, "actual_price": 116.80, "filled": True,
                     "fee": 5.0}]}, ensure_ascii=False) + "\n")
            data = dash.app.test_client().get("/api/cb/paper").get_json()
            self.assertEqual(data["weeks"], 1)                    # 只算一周
            self.assertEqual(data["fill_rate"], 1.0)              # 以最后一条为准
            self.assertAlmostEqual(data["median_slippage"], 116.80 / 117.14 - 1.0, places=4)
            self.assertAlmostEqual(data["total_fee"], 5.0)
        finally:
            dash.STATE_DIR = orig_state
            dash._cb_report = orig_report
