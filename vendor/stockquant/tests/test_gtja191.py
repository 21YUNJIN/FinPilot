"""Smoke and invariant tests for the first GTJA191 distillation batch."""

from __future__ import annotations

import math
import unittest

from stockquant.domain import Bar
from stockquant.factors.gtja191 import GTJA191_CANDIDATES, candidate_names


def make_bars(n=100):
    rows = []
    for i in range(n):
        close = 10.0 + 0.03 * i + 0.20 * math.sin(i / 3.0)
        open_ = close - 0.03 * math.cos(i / 2.0)
        high = max(open_, close) + 0.10 + 0.01 * (i % 5)
        low = min(open_, close) - 0.08 - 0.01 * (i % 3)
        rows.append(Bar(
            date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            symbol="TEST",
            open=open_, high=high, low=low, close=close,
            volume=1_000_000 + 3_000 * i + 10_000 * (i % 7),
        ))
    return rows


class Gtja191CandidateTests(unittest.TestCase):
    def test_catalog_is_experimental_and_complete_for_batch(self):
        self.assertEqual(len(candidate_names()), 17)
        self.assertEqual(len(set(candidate_names())), 17)
        self.assertTrue(all(name.startswith("gtja_") for name in candidate_names()))

    def test_all_candidates_return_finite_values_with_enough_history(self):
        bars = make_bars(120)
        for spec in GTJA191_CANDIDATES:
            with self.subTest(factor=spec.name):
                value = spec.fn(None, "TEST", bars)  # type: ignore[arg-type]
                self.assertIsNotNone(value, spec.name)
                self.assertTrue(math.isfinite(float(value)), spec.name)

    def test_all_candidates_fail_closed_with_short_history(self):
        bars = make_bars(5)
        for spec in GTJA191_CANDIDATES:
            with self.subTest(factor=spec.name):
                self.assertIsNone(spec.fn(None, "TEST", bars), spec.name)

    def test_rsi_direction_on_monotonic_up_and_down(self):
        up = [
            Bar(f"2026-01-{i+1:02d}", "UP", 10 + i, 10.5 + i, 9.9 + i, 10 + i, 1_000_000)
            for i in range(20)
        ]
        down = [
            Bar(f"2026-01-{i+1:02d}", "DOWN", 30 - i, 30.1 - i, 29.5 - i, 30 - i, 1_000_000)
            for i in range(20)
        ]
        up_rsi = next(s.fn(None, "UP", up) for s in GTJA191_CANDIDATES if s.name == "gtja_079")
        down_rsi = next(s.fn(None, "DOWN", down) for s in GTJA191_CANDIDATES if s.name == "gtja_079")
        self.assertGreater(up_rsi, 90)
        self.assertLess(down_rsi, 10)


if __name__ == "__main__":
    unittest.main()
