"""转债报告脚本：必须无未来函数，且实盘清单规则 ⊇ 回测规则。"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _write_panel(path):
    days = [f"2024-0{m}-{d:02d}" for m in (1, 2) for d in range(1, 21)]
    rows = []
    # 113046 = 现在处于强赎名单里（用于验证回测不能偷看）
    codes = {"113046": 100.0, "128134": 105.0, "110075": 110.0, "113711": 95.0,
             "127025": 130.0, "128138": 120.0}
    for i, d in enumerate(days):
        for code, base in codes.items():
            rows.append({"date": d, "bond_code": code, "bond_name": code,
                         "close": base + i * 0.1, "conversion_premium": 5.0,
                         "rating": "AA", "pure_bond_premium": 2.0})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "bond_code", "bond_name", "close",
                                          "conversion_premium", "rating", "pure_bond_premium"])
        w.writeheader()
        w.writerows(rows)


def _write_volume(path, codes):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "bond_code", "volume", "amount"])
        days = [f"2024-0{m}-{d:02d}" for m in (1, 2) for d in range(1, 21)]
        for d in days:
            for code in codes:
                w.writerow([d, code, 100000, 2e7])


class CbReportTests(unittest.TestCase):
    def test_report_excludes_no_future_information(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            panel, vol, live, out = td / "cb.csv", td / "vol.csv", td / "live.json", td / "out"
            _write_panel(panel)
            _write_volume(vol, ["113046", "128134", "110075", "113711", "127025", "128138"])
            live.write_text(json.dumps({
                "redeem": {"113046": {"name": "强赎债", "status": "已公告强赎",
                                      "last_trade_date": "2026-10-20"}},
                "st_bonds": {"128134": "ST某某"},
            }, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run([sys.executable, "scripts/cb_double_low_report.py",
                                "--data", str(panel), "--volume", str(vol), "--live", str(live),
                                "--out", str(out), "--top-n", "3", "--rebalance-days", "5",
                                "--min-amount", "1e7", "--start", "2024-01-01"],
                               cwd=str(REPO), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            payload = json.loads((out / "double_low.json").read_text(encoding="utf-8"))
            # 1) 回测不得使用"当前强赎名单"（未来函数）
            self.assertFalse(payload["rules"]["backtest_used_live_exclude"])
            # 2) 强赎债必须从实盘清单剔除
            self.assertIn("113046", payload["excluded_redeem"])
            self.assertNotIn("113046", [p["bond_code"] for p in payload["picks"]])
            # 3) ST 正股也剔除
            self.assertNotIn("128134", [p["bond_code"] for p in payload["picks"]])
            # 4) 数据新鲜度字段存在
            self.assertIn("data_age_days", payload)

    def test_report_flags_optional_lookahead_switch(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            panel, vol, live, out = td / "cb.csv", td / "vol.csv", td / "live.json", td / "out"
            _write_panel(panel)
            _write_volume(vol, ["113046", "128134", "110075", "113711", "127025", "128138"])
            live.write_text(json.dumps({"redeem": {"113046": {"status": "已公告强赎"}}}), encoding="utf-8")
            r = subprocess.run([sys.executable, "scripts/cb_double_low_report.py",
                                "--data", str(panel), "--volume", str(vol), "--live", str(live),
                                "--out", str(out), "--top-n", "3", "--rebalance-days", "5",
                                "--min-amount", "1e7", "--start", "2024-01-01",
                                "--backtest-exclude-live"],
                               cwd=str(REPO), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            payload = json.loads((out / "double_low.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["rules"]["backtest_used_live_exclude"])


class MinAmountGuardTests(unittest.TestCase):
    def test_min_amount_without_volume_raises(self):
        from stockquant.convertible import run_double_low, current_picks
        with tempfile.TemporaryDirectory() as td:
            panel = Path(td) / "cb.csv"
            _write_panel(panel)
            with self.assertRaises(ValueError):
                run_double_low(str(panel), top_n=3, rebalance_days=5, start="2024-01-01",
                               min_amount=1e7)
            with self.assertRaises(ValueError):
                current_picks(str(panel), top_n=3, min_amount=1e7)


if __name__ == "__main__":
    unittest.main()
