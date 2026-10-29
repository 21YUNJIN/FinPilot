import csv
import tempfile
import unittest
from pathlib import Path

from stockquant.convertible.double_low import current_picks, run_double_low


def _write_panel(path):
    # 6 bonds, 40 trading days; bond B is delisted early; bond F is brand new
    rows = []
    days = [f"2024-01-{d:02d}" for d in range(1, 21)] + [f"2024-02-{d:02d}" for d in range(1, 21)]
    prices = {
        "A": [100 + i * 0.1 for i in range(40)],
        "B": [100 + i * 0.1 for i in range(5)] + [None] * 35,      # delisted
        "C": [110 + i * 0.05 for i in range(40)],
        "D": [120 - i * 0.02 for i in range(40)],
        "E": [95 + i * 0.2 for i in range(40)],
        "F": [None] * 35 + [100.0] * 5,                             # brand new
    }
    prem = {"A": 1.0, "B": 2.0, "C": 5.0, "D": 8.0, "E": 10.0, "F": 0.5}
    for i, d in enumerate(days):
        for code, series in prices.items():
            if series[i] is None:
                continue
            rows.append({"date": d, "bond_code": code, "bond_name": code,
                         "close": series[i], "conversion_premium": prem[code]})
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "bond_code", "bond_name", "close", "conversion_premium"])
        w.writeheader()
        w.writerows(rows)


class ConvertibleTests(unittest.TestCase):
    def test_backtest_runs(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cb.csv"
            _write_panel(str(p))
            r = run_double_low(str(p), top_n=3, rebalance_days=5, start="2024-01-01", min_price=90,
                               strict_volume=False)  # 合成面板没有成交量，显式关闭 fail-closed
            self.assertTrue(r.equity)
            self.assertEqual(len(r.dates), len(r.equity))
            self.assertIn("cagr", r.metrics)

    @staticmethod
    def _oscillating_panel(path):
        """5 只债、60 个交易日；P3 每 5 天在"挤进前 3"和"掉到第 4"之间切换。

        用来验证换手带宽：没有带宽时每个调仓日都要换 1 只，有带宽时应当不动。
        """
        rows = []
        days = [f"2024-{m:02d}-{d:02d}" for m in (1, 2, 3) for d in range(1, 21)][:60]
        for i, d in enumerate(days):
            block = i // 5
            px = {"P0": 100.0, "P1": 101.0, "P2": 102.0,
                  "P3": 101.5 if block % 2 == 0 else 103.0,   # 在 102 上下穿越
                  "P4": 120.0}
            for code, v in px.items():
                rows.append({"date": d, "bond_code": code, "bond_name": code,
                             "close": v, "conversion_premium": 0.0})
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["date", "bond_code", "bond_name", "close", "conversion_premium"])
            w.writeheader()
            w.writerows(rows)

    def test_sell_rank_buffer_cuts_boundary_churn(self):
        """排名在边界抖动不该产生买卖：带宽必须把换手压下去，且仓位始终填满。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cb.csv"
            self._oscillating_panel(str(p))
            kw = dict(top_n=3, rebalance_days=5, start="2024-01-01", strict_volume=False)
            off = run_double_low(str(p), **kw)
            on = run_double_low(str(p), sell_rank_buffer=4, **kw)
            filled = [rb for rb in off.rebalances if rb["picks"]]
            self.assertGreater(len(filled), 3)                 # 样本够多才有意义
            for rb in on.rebalances:                            # 有持仓的期，仓位始终填满
                if rb["picks"]:
                    self.assertEqual(len(rb["picks"]), 3)
            mean = lambda r: sum(x["turnover"] for x in r.rebalances) / len(r.rebalances)
            self.assertLess(mean(on), mean(off))                # 换手确实被压下去
            # 带宽下：第 2 期起持仓集合不再变化（没有边界抖动带来的买卖）
            after_first = [rb for rb in on.rebalances if rb["picks"]][1:]
            self.assertTrue(after_first)
            self.assertTrue(all(rb["turnover"] == 0.0 for rb in after_first))

    def test_sell_rank_buffer_smaller_than_top_n_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cb.csv"
            self._oscillating_panel(str(p))
            with self.assertRaises(ValueError):
                run_double_low(str(p), top_n=3, rebalance_days=5, start="2024-01-01",
                               strict_volume=False, sell_rank_buffer=2)

    def test_capital_aware_cost_is_recorded_and_bites(self):
        """本金感知成本：小资金 + 最低佣金 → 成本远高于固定 5bps，净值更低。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cb.csv"
            self._oscillating_panel(str(p))
            kw = dict(top_n=3, rebalance_days=5, start="2024-01-01", strict_volume=False)
            cheap = run_double_low(str(p), **kw)
            real = run_double_low(str(p), capital=3000.0, min_commission=5.0, **kw)
            charged = [rb for rb in real.rebalances if "cost" in rb]
            self.assertTrue(charged)
            self.assertGreater(sum(x.get("cost", 0.0) for x in real.rebalances),
                               sum(x.get("cost", 0.0) for x in cheap.rebalances))
            self.assertLess(real.equity[-1], cheap.equity[-1])
            # 带宽 + 真实成本：两条改进叠加后净值不应更差
            band = run_double_low(str(p), capital=3000.0, min_commission=5.0,
                                  sell_rank_buffer=4, **kw)
            self.assertGreaterEqual(band.equity[-1], real.equity[-1])
            with self.assertRaises(ValueError):
                run_double_low(str(p), capital=0.0, **kw)

    def test_delisted_and_new_bonds_excluded_from_picks(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cb.csv"
            _write_panel(str(p))
            picks = current_picks(str(p), top_n=10, asof="2024-02-20", min_price=90, strict_volume=False)
            codes = {x["bond_code"] for x in picks}
            self.assertNotIn("B", codes)   # delisted -> excluded
            self.assertNotIn("F", codes)   # brand new (no history) -> excluded


if __name__ == "__main__":
    unittest.main()


class CbPositionTests(unittest.TestCase):
    """转债持仓盯盘：建议链必须只依赖当日可得信息，且能识别风险。"""

    def _panel_path(self, td):
        p = Path(td) / "cb.csv"
        rows = []
        days = [f"2024-03-{d:02d}" for d in range(1, 32)]
        # A 平稳；B 近 20 日急跌 30%；C 价格跌到 90（债底区外）
        # 128134: 前 11 天 130，之后 20 天线性跌到 100（刚好 -23%，且仍在 95 之上）
        series = {
            "113046": [110 + i * 0.01 for i in range(len(days))],
            "128134": [130.0 if i <= 10 else 130.0 - 1.5 * (i - 10) for i in range(len(days))],
            "110075": [97 - 0.4 * i for i in range(len(days))],
        }
        for i, d in enumerate(days):
            for code, s in series.items():
                rows.append({"date": d, "bond_code": code, "bond_name": code, "close": s[i],
                             "conversion_premium": 10.0, "rating": "AA",
                             "pure_bond_premium": 1.0})
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["date", "bond_code", "bond_name", "close",
                                              "conversion_premium", "rating", "pure_bond_premium"])
            w.writeheader()
            w.writerows(rows)
        return str(p)

    def test_analyze_positions_flags_risk(self):
        from stockquant.convertible.positions import analyze_positions, is_cb_code
        self.assertTrue(is_cb_code("113046") and is_cb_code("128134"))
        self.assertFalse(is_cb_code("600519") and is_cb_code("000001.SZ") and is_cb_code(""))
        with tempfile.TemporaryDirectory() as td:
            p = self._panel_path(td)
            rows = analyze_positions(
                {"113046": {"shares": 10, "average_cost": 110.0},
                 "128134": {"shares": 10, "average_cost": 100.0},
                 "110075": {"shares": 10, "average_cost": 100.0},
                 "600519": {"shares": 100, "average_cost": 1500.0}},
                data_csv=p, params={"top_n": 1, "min_amount": 0})
        by = {r["code"]: r for r in rows}
        self.assertNotIn("600519", by)          # 股票不在这里
        self.assertEqual(len(by), 3)
        self.assertIn("急跌", by["128134"]["action"])   # 近20日 -30%
        self.assertIn("信用", by["110075"]["action"])   # 价格 90 < 95
        self.assertIsNotNone(by["113046"]["rank"])      # 有全市场排名
        # 风险行排在最前（critical/warning 优先）
        self.assertEqual(rows[0]["severity"], "critical")

    def test_alerts_from_positions_are_dedupable(self):
        from stockquant.convertible.positions import alerts_from_positions
        rows = [{"code": "110075", "name": "X", "action": "卖出（信用风险）", "severity": "critical",
                 "price": 90.0, "cost": 100.0, "pnl_pct": -0.1, "reasons": ["跌破债底"]},
                {"code": "113046", "name": "Y", "action": "持有", "severity": "ok",
                 "price": 110.0, "cost": 110.0, "pnl_pct": 0.0, "reasons": []}]
        alerts = alerts_from_positions(rows)
        self.assertEqual(len(alerts), 1)                 # 持有不发提醒
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertIn("110075", alerts[0]["id"])


class ListedDayAndTradabilityTests(unittest.TestCase):
    """list_date 是 PIT 安全的强过滤：挡住上市前占位行情 + 未进转股期的次新债。

    历史事故：全库有 1369 行 close=100.0 的"上市前占位行情"（最长 21 个交易日、零成交），
    它们的价格（100）和溢价率（约 11%）都能通过价格/溢价率过滤，只能靠成交量或上市日挡。
    """

    def _write(self, td, with_volume=True, with_list_date=True):
        p = Path(td) / "cb.csv"
        days = [f"2024-0{m}-{d:02d}" for m in (1, 2, 3) for d in range(1, 29)]
        # NEW 债：2024-02-20 才上市（3 月前是占位行情，close=100，零成交）
        # OLD 债：2023-01-01 上市（早已进转股期）
        rows = []
        for i, d in enumerate(days):
            rows.append({"date": d, "bond_code": "113001", "bond_name": "OLD", "close": 100 + i * 0.1,
                         "conversion_premium": 5.0, "rating": "AA", "pure_bond_premium": 1.0,
                         "list_date": "2023-01-01" if with_list_date else ""})
            rows.append({"date": d, "bond_code": "128001", "bond_name": "NEW",
                         "close": 100.0, "conversion_premium": 11.0, "rating": "AA",
                         "pure_bond_premium": 1.0,
                         "list_date": "2024-02-20" if with_list_date else ""})
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["date", "bond_code", "bond_name", "close",
                                              "conversion_premium", "rating", "pure_bond_premium",
                                              "list_date"])
            w.writeheader(); w.writerows(rows)
        vol = Path(td) / "vol.csv"
        if with_volume:
            with vol.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(["date", "bond_code", "volume", "amount"])
                for d in days:
                    for code in ("113001", "128001"):
                        # 128001 上市前零成交
                        v = 0 if (code == "128001" and d < "2024-02-20") else 100000
                        w.writerow([d, code, v, 2e7 if v else 0])
        return str(p), str(vol)

    def test_prelisting_placeholder_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            panel, vol = self._write(td)
            from stockquant.convertible.double_low import current_picks
            # 2024-01-10：NEW 还没上市（占位价 100、溢价 11%），不能出现在清单里
            picks = current_picks(panel, top_n=5, asof="2024-01-10", min_price=95,
                                  volume_path=vol, min_amount=None, strict_volume=True)
            codes = {x["bond_code"] for x in picks}
            self.assertNotIn("128001", codes)

    def test_newly_listed_under_180d_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            panel, vol = self._write(td)
            from stockquant.convertible.double_low import current_picks
            # 2024-02-25：NEW 已上市但只有 5 天 -> 仍在 180 天窗口内，必须排除
            picks = current_picks(panel, top_n=5, asof="2024-02-25", min_price=95,
                                  volume_path=vol, min_amount=None, strict_volume=True)
            codes = {x["bond_code"] for x in picks}
            self.assertNotIn("128001", codes)
            # 放宽到 5 天则应可选（证明是 180 天规则在起作用，不是别的原因）
            relaxed = current_picks(panel, top_n=5, asof="2024-02-25", min_price=95,
                                    volume_path=vol, min_amount=None, min_listed_days=5)
            self.assertIn("128001", {x["bond_code"] for x in relaxed})

    def test_missing_volume_data_raises_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            panel, _vol = self._write(td, with_volume=False)
            from stockquant.convertible.double_low import current_picks, run_double_low
            with self.assertRaises(ValueError):
                current_picks(panel, top_n=3, min_amount=None)
            with self.assertRaises(ValueError):
                run_double_low(panel, top_n=3, rebalance_days=5, start="2024-01-01", min_amount=None)
            # 显式关闭才放行（fail-open 只能由调用方明说）
            picks = current_picks(panel, top_n=3, min_amount=None, strict_volume=False)
            self.assertTrue(isinstance(picks, list))

    def test_zero_volume_day_is_not_tradeable(self):
        with tempfile.TemporaryDirectory() as td:
            panel, vol = self._write(td)
            from stockquant.convertible.double_low import current_picks
            # 造一个"当天零成交"的日子给 OLD 债
            with open(vol, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f); w.writerow(["2024-03-28", "113001", 0, 0])
            picks = current_picks(panel, top_n=5, asof="2024-03-28", min_price=95,
                                  volume_path=vol, min_amount=None, strict_volume=True)
            self.assertNotIn("113001", {x["bond_code"] for x in picks})


class CostAccountingTests(unittest.TestCase):
    """换手成本必须**双边**计：卖 k 只 + 买 k 只都要付费。

    2026-10-13 审计发现的真 bug：原来 cost = turnover×bps，只收一边，
    使 CAGR 虚高 0.83 个百分点/年（真实 16.84% vs 虚高 17.67%）。
    """

    def _panel(self, td):
        p = Path(td) / "cb.csv"
        days = [f"2024-0{m}-{d:02d}" for m in (1, 2, 3) for d in range(1, 29)]
        rows = []
        for i, d in enumerate(days):
            # 两只债价格交叉（交替领先），迫使每次调仓都换手
            rows.append({"date": d, "bond_code": "113001", "bond_name": "A",
                         "close": 100 + (i % 2) * 5, "conversion_premium": 5.0, "rating": "AA",
                         "pure_bond_premium": 1.0, "list_date": "2023-01-01"})
            rows.append({"date": d, "bond_code": "128001", "bond_name": "B",
                         "close": 100 + ((i + 1) % 2) * 5, "conversion_premium": 6.0, "rating": "AA",
                         "pure_bond_premium": 1.0, "list_date": "2023-01-01"})
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["date", "bond_code", "bond_name", "close",
                                              "conversion_premium", "rating", "pure_bond_premium",
                                              "list_date"])
            w.writeheader(); w.writerows(rows)
        vol = Path(td) / "vol.csv"
        with vol.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["date", "bond_code", "volume", "amount"])
            for d in days:
                for code in ("113001", "128001"):
                    w.writerow([d, code, 100000, 2e7])
        return str(p), str(vol)

    def test_cost_is_charged_on_both_sides(self):
        from stockquant.convertible.double_low import run_double_low
        with tempfile.TemporaryDirectory() as td:
            panel, vol = self._panel(td)
            kw = dict(top_n=1, rebalance_days=5, start="2024-01-01", min_price=50, max_price=200,
                      volume_path=vol, min_amount=None, strict_volume=True)
            free = run_double_low(panel, cost_bps=0.0, **kw)
            costly = run_double_low(panel, cost_bps=10.0, **kw)
            # 成本是在"含收益的净值"上扣的：equity *= (1 + r - c)，
            # 所以要用两次回测的净值反推每期毛收益 r，再核对成本是否正好是 2×turnover×bps。
            def expected_ratio(multiple):
                ratio = 1.0
                # equity[0] 是初始 1.0，之后第 i 个点用的是 rebalances[i] 的换手率
                for i in range(1, len(free.equity)):
                    gross = free.equity[i] / free.equity[i - 1]      # 1 + r
                    cost = multiple * float(costly.rebalances[i]["turnover"]) * 10.0 / 10000.0
                    ratio *= (gross - cost) / gross
                return ratio
            actual = (costly.metrics["total_return"] + 1.0) / (free.metrics["total_return"] + 1.0)
            self.assertAlmostEqual(actual, expected_ratio(2.0), places=9)   # 双边
            self.assertNotAlmostEqual(actual, expected_ratio(1.0), places=4)  # 不是单边


