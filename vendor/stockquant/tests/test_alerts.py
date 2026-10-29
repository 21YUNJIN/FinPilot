import tempfile
import unittest

from stockquant.assistant.alerts import (
    CRITICAL,
    OPPORTUNITY,
    WARNING,
    AlertStore,
    build_alerts,
)


class AlertBuildTests(unittest.TestCase):
    def _snapshot(self):
        return {
            "signal_date": "2026-09-30",
            "rows": [
                {"symbol": "A.SH", "action": "止损/减仓", "reason": "跌破ATR止损线", "pnl_pct": -0.14, "price": 8.6, "stop_loss": 9.2},
                {"symbol": "B.SH", "action": "减仓", "reason": "模型分数转负", "score": -0.7, "pnl_pct": 0.02, "price": 10.0},
                {"symbol": "C.SH", "action": "止盈/分批减仓", "reason": "触及止盈线", "pnl_pct": 0.35, "price": 13.5, "take_profit": 13.0},
                {"symbol": "D.SH", "action": "持有观察", "reason": "信号中性", "pnl_pct": 0.01, "price": 5.0},
            ],
        }

    def test_severity_mapping(self):
        alerts = build_alerts(self._snapshot())
        by_symbol = {a["symbol"]: a for a in alerts}
        self.assertEqual(by_symbol["A.SH"]["severity"], CRITICAL)
        self.assertEqual(by_symbol["B.SH"]["severity"], WARNING)
        self.assertEqual(by_symbol["C.SH"]["severity"], OPPORTUNITY)
        self.assertNotIn("D.SH", by_symbol)

    def test_entry_alerts_become_opportunities(self):
        entry = {"rows": [{"symbol": "E.SZ", "status": "ready", "current_price": 10, "entry_zone": [9.8, 10.1], "stop_loss": 9.5, "reason": "回踩均线"}]}
        alerts = build_alerts({"signal_date": "2026-09-30", "rows": []}, entry)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], OPPORTUNITY)
        self.assertEqual(alerts[0]["kind"], "entry")

    def test_dedupe_by_symbol_kind_date(self):
        with tempfile.TemporaryDirectory() as td:
            store = AlertStore(td)
            first = store.append_new(build_alerts(self._snapshot()))
            second = store.append_new(build_alerts(self._snapshot()))
            self.assertEqual(len(first), 3)
            self.assertEqual(len(second), 0)  # nothing new on the second pass
            self.assertEqual(len(store.load()), 3)
            self.assertEqual(store.unread_count(), 3)
            store.mark_all_read()
            self.assertEqual(store.unread_count(), 0)


if __name__ == "__main__":
    unittest.main()
