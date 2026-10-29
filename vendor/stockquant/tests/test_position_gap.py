import unittest

from stockquant.assistant.position_gap import compute_position_gap


class PositionGapTests(unittest.TestCase):
    def _holdings(self):
        return [
            {"symbol": "A.SH", "shares": 1000, "price": 10.0, "market_value": 10000.0},
            {"symbol": "C.SH", "shares": 500, "price": 20.0, "market_value": 10000.0},
        ]

    def test_actions_cover_all_cases(self):
        targets = {"A.SH": 0.20, "B.SH": 0.30, "C.SH": 0.0, "D.SH": 0.05}
        out = compute_position_gap(self._holdings(), targets, cash=80000.0)
        by_symbol = {r["symbol"]: r for r in out["rows"]}
        # total = 80000 cash + 20000 holdings = 100000
        self.assertAlmostEqual(by_symbol["A.SH"]["current_weight"], 0.10)
        self.assertEqual(by_symbol["A.SH"]["action"], "加仓")   # 0.10 -> 0.20
        self.assertEqual(by_symbol["B.SH"]["action"], "新建")   # not held
        self.assertEqual(by_symbol["C.SH"]["action"], "清仓")   # target 0
        self.assertEqual(by_symbol["D.SH"]["action"], "新建")
        self.assertEqual(out["summary"]["add"], 1)
        self.assertEqual(out["summary"]["new"], 2)
        self.assertEqual(out["summary"]["exit"], 1)

    def test_hold_when_gap_small(self):
        targets = {"A.SH": 0.102}
        out = compute_position_gap([{"symbol": "A.SH", "shares": 1000, "price": 10.0, "market_value": 1000.0}],
                                   targets, cash=9000.0)
        self.assertEqual(out["rows"][0]["action"], "持有")

    def test_target_shares_are_lot_rounded(self):
        out = compute_position_gap([], {"A.SH": 0.5}, cash=10000.0, lot_size=100, prices={"A.SH": 10.0})
        row = out["rows"][0]
        # 0.5 * 10000 / 10 = 500 shares, rounded to lots
        self.assertEqual(row["target_shares"], 500)
        self.assertEqual(row["shares_delta"], 500)

    def test_notes(self):
        out = compute_position_gap([], {"A.SH": 0.01}, cash=10000.0, prices={"A.SH": 1000.0})
        row = out["rows"][0]
        self.assertEqual(row["target_shares"], 0)
        self.assertIn("不足一手", row["note"])
        out2 = compute_position_gap([{"symbol": "Z.SH", "shares": 100, "price": 10.0, "market_value": 1000.0}],
                                    {}, cash=9000.0)
        self.assertEqual(out2["rows"][0]["action"], "清仓")
        self.assertIn("已无此股", out2["rows"][0]["note"])

    def test_empty_inputs_are_safe(self):
        out = compute_position_gap([], {}, cash=0.0)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["holdings"], 0)


if __name__ == "__main__":
    unittest.main()
