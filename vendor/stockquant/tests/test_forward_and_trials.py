import json
import tempfile
import unittest
from pathlib import Path

from stockquant.analysis.trial_registry import cumulative_trials, record_trials
from stockquant.assistant.forward_test import evaluate_frozen, freeze_targets
from stockquant.domain import Bar, MarketData


def make_market(days=40, symbols=4):
    rows = []
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            px = 10 + i + d * 0.1
            rows.append(Bar(date=date, symbol=f"S{i}", open=px, high=px * 1.01,
                            low=px * 0.99, close=px, volume=100000, sector="Tech",
                            market_cap=1e9, pe=15, pb=1.5, roe=0.1))
    return MarketData.from_rows([b.__dict__ for b in rows])


class TrialRegistryTests(unittest.TestCase):
    def test_records_and_counts_distinct(self):
        with tempfile.TemporaryDirectory() as td:
            import stockquant.analysis.trial_registry as tr
            old = tr.STATE_DIR
            tr.STATE_DIR = Path(td)
            try:
                self.assertEqual(cumulative_trials(), 0)
                record_trials(["a", "b", "a"])  # 'a' duplicate
                self.assertEqual(cumulative_trials(), 2)
            finally:
                tr.STATE_DIR = old


class ForwardTestTests(unittest.TestCase):
    def test_evaluate_frozen_returns_forward_performance(self):
        market = make_market()
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "freeze.json"
            freeze_targets({"S0": 0.5, "S1": 0.5}, market.dates[10], path=fp)
            out = evaluate_frozen(market, path=fp, results_path=Path(td) / "res.jsonl")
            self.assertTrue(out["available"])
            self.assertEqual(out["as_of"], market.dates[10])
            self.assertGreater(out["n_priced"], 0)
            # rising market: forward return should be positive
            self.assertGreater(out["portfolio_return"], 0)
            self.assertGreater(out["benchmark_return"], 0)

    def test_evaluate_missing_freeze(self):
        market = make_market()
        with tempfile.TemporaryDirectory() as td:
            out = evaluate_frozen(market, path=Path(td) / "nope.json")
            self.assertFalse(out["available"])


if __name__ == "__main__":
    unittest.main()
