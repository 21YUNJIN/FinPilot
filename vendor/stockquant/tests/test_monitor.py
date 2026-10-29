import json
import tempfile
import unittest
from pathlib import Path

from stockquant.assistant.monitor import (
    ACTION_STOP,
    ACTION_TAKE,
    DEFAULT_SETTINGS,
    HoldingsMonitor,
    MonitorStore,
)
from stockquant.backtest import BacktestConfig
from stockquant.domain import Bar, MarketData


def make_market(days=140, symbols=4, drop_last=False):
    rows = []
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            px = 10 + i + d * 0.02
            if drop_last and d == days - 1:
                px *= 0.4
            rows.append(Bar(
                date=date, symbol=f"S{i}", open=px * 0.999, high=px * 1.01,
                low=px * 0.99, close=px, volume=100000, sector="Tech",
                raw_close=px / 40.0,
                market_cap=1e9 * (i + 1), pe=20 - i, pb=2 - 0.1 * i, roe=0.1,
                revenue_growth=0.05, profit_growth=0.04, gross_margin=0.2,
                forecast_growth=0.05, forecast_positive=0.5, holder_change=0.0,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class MonitorStoreTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            store = MonitorStore(td)
            self.assertEqual(store.load_settings()["enabled"], DEFAULT_SETTINGS["enabled"])
            saved = store.save_settings({"enabled": True, "interval_seconds": 5})
            self.assertTrue(saved["enabled"])
            self.assertEqual(saved["interval_seconds"], 60)  # clamped to >= 60
            self.assertTrue(store.load_settings()["enabled"])

    def test_snapshot_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            store = MonitorStore(td)
            self.assertEqual(store.load_snapshot(), {})
            store.save_snapshot({"rows": [{"symbol": "S0"}]})
            self.assertEqual(store.load_snapshot()["rows"][0]["symbol"], "S0")


class HoldingsMonitorTests(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.config = BacktestConfig(min_history=60)

    def test_empty_holdings(self):
        monitor = HoldingsMonitor()
        out = monitor.analyze(self.market, [], self.config)
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["holdings"], 0)

    def test_stop_loss_triggered_on_large_drawdown(self):
        market = make_market(drop_last=True)
        last = market.dates[-1]
        bar = market.bar("S0", last)
        raw = bar.raw_close if bar.raw_close > 0 else bar.close
        holdings = [{
            "symbol": "S0", "shares": 1000,
            "average_cost": raw * 2.5,  # deeply underwater
            "price": raw, "market_value": raw * 1000,
        }]
        out = HoldingsMonitor().analyze(market, holdings, self.config, latest_date=last)
        self.assertEqual(out["rows"][0]["action"], ACTION_STOP)
        self.assertGreater(out["rows"][0]["stop_loss"], 0)

    def test_stop_and_target_are_on_cost_scale(self):
        # Regression: ATR is computed on the adjusted series while cost is on the
        # raw price, so an unguarded ratio produced stop_loss = 0.01.
        market = make_market()
        last = market.dates[-1]
        bar = market.bar("S0", last)
        raw = bar.raw_close if bar.raw_close > 0 else bar.close
        holdings = [{"symbol": "S0", "shares": 100, "average_cost": raw,
                     "price": raw, "market_value": raw * 100}]
        out = HoldingsMonitor().analyze(market, holdings, self.config, latest_date=last)
        row = out["rows"][0]
        self.assertGreater(row["stop_loss"], 0.5 * row["average_cost"])
        self.assertLess(row["stop_loss"], row["average_cost"])
        self.assertGreater(row["take_profit"], row["average_cost"])

    def test_take_profit_triggered_on_large_gain(self):
        market = make_market()
        last = market.dates[-1]
        bar = market.bar("S0", last)
        raw = bar.raw_close if bar.raw_close > 0 else bar.close
        holdings = [{
            "symbol": "S0", "shares": 1000,
            "average_cost": raw * 0.3,  # large unrealised gain
            "price": raw, "market_value": raw * 1000,
        }]
        out = HoldingsMonitor().analyze(market, holdings, self.config, latest_date=last)
        self.assertEqual(out["rows"][0]["action"], ACTION_TAKE)

    def test_summary_counts_match_rows(self):
        market = make_market()
        last = market.dates[-1]
        holdings = [
            {"symbol": "S0", "shares": 100, "average_cost": 10.0, "price": 11.0, "market_value": 1100},
            {"symbol": "S1", "shares": 100, "average_cost": 20.0, "price": 21.0, "market_value": 2100},
        ]
        out = HoldingsMonitor().analyze(market, holdings, self.config, latest_date=last)
        self.assertEqual(len(out["rows"]), 2)
        self.assertEqual(sum(out["summary"].values()), 2)
        # largest weight first
        weights = [r["weight"] for r in out["rows"]]
        self.assertEqual(weights, sorted(weights, reverse=True))


if __name__ == "__main__":
    unittest.main()
