"""Panel operator and GTJA cross-sectional factor tests."""

from __future__ import annotations

import math
import unittest

from stockquant.domain import Bar, MarketData
from stockquant.factors.panel import (
    PANEL_FACTORS,
    add,
    cross_sectional_rank,
    delay,
    delta,
    panel_values,
    rolling_corr,
)


def make_market(days=240):
    rows = []
    symbols = ("A", "B", "C")
    for i in range(days):
        month = 1 + i // 28
        day = 1 + i % 28
        date = f"2024-{month:02d}-{day:02d}" if month <= 12 else f"2025-{month-12:02d}-{day:02d}"
        for j, symbol in enumerate(symbols):
            close = (10 + j + 0.02 * i + (j + 1) * 0.1 * math.sin(i / 7.0)
                     + 0.5 * ((i + 2 * j) % 3) + 1.5 * math.sin(i / 5.0 + j * 2.0))
            open_ = close - 0.03 * (j + 1) + 0.05 * math.sin(i / 3.0 + j)
            rows.append(Bar(date, symbol, open_, close + 0.2, close - 0.2, close,
                            1_000_000 + j * 100_000 + i * 1000
                            + int(300_000 * (1 + math.sin(i / 4.0 + j * 1.7)))))
    return MarketData.from_rows([r.__dict__ for r in rows])


class PanelOperatorTests(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.dates = self.market.dates

    def test_rank_is_cross_sectional(self):
        panel = panel_values(self.market, "close")
        ranked = cross_sectional_rank(panel)
        date = self.dates[-1]
        self.assertEqual(set(ranked[date]), {"A", "B", "C"})
        self.assertAlmostEqual(max(ranked[date].values()), 1.0)
        self.assertAlmostEqual(min(ranked[date].values()), 0.0)

    def test_delay_and_delta_align_by_symbol(self):
        panel = panel_values(self.market, "close")
        shifted = delay(panel, 1)
        change = delta(panel, 1)
        date = self.dates[-1]
        prev = self.dates[-2]
        self.assertAlmostEqual(change[date]["A"], panel[date]["A"] - shifted[date]["A"])
        self.assertAlmostEqual(shifted[date]["A"], panel[prev]["A"])

    def test_rolling_correlation_matches_identical_series(self):
        panel = panel_values(self.market, "close")
        corr = rolling_corr(panel, panel, 10)
        values = list(corr[self.dates[-1]].values())
        self.assertTrue(values)
        self.assertTrue(all(abs(v - 1.0) < 1e-12 for v in values))

    def test_all_panel_factors_produce_values(self):
        for spec in PANEL_FACTORS:
            with self.subTest(factor=spec.name):
                values = spec.fn(self.market)
                self.assertTrue(values, spec.name)
                last = values[self.dates[-1]]
                self.assertTrue(last, spec.name)
                self.assertTrue(any(math.isfinite(v) for v in last.values()), spec.name)


if __name__ == "__main__":
    unittest.main()
