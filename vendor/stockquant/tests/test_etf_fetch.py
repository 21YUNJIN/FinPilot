"""ETF fetch fallback: Tencent rows must map to the project's bar schema."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.fetch_etf_data import _tencent_rows_for


class TencentEtfFallbackTests(unittest.TestCase):
    def test_fallback_maps_volume_and_raw_close(self):
        adjusted = [
            ("2026-10-20", 4.0, 4.1, 4.2, 3.9, 1000.0),
            ("2026-10-21", 4.1, 4.2, 4.3, 4.0, 1100.0),
        ]
        raw = [
            ("2026-10-20", 4.0, 4.05, 4.2, 3.9, 1000.0),
            ("2026-10-21", 4.1, 4.15, 4.3, 4.0, 1100.0),
        ]

        with patch("stockquant.data.tencent.daily_bars", side_effect=[adjusted, raw]):
            rows = _tencent_rows_for("510300", "沪深300ETF", "2026-10-20", "2026-10-22")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["source"], "tencent:etf:qfq")
        self.assertEqual(rows[-1]["volume"], 110000.0)  # lots -> shares
        self.assertEqual(rows[-1]["raw_close"], 4.15)
        self.assertEqual(rows[-1]["available_at"], "2026-10-21 15:00:00")

    def test_failed_adjusted_series_returns_empty(self):
        with patch("stockquant.data.tencent.daily_bars", return_value=[]):
            self.assertEqual(_tencent_rows_for("510300", "沪深300ETF", "2026-10-20", "2026-10-22"), [])


if __name__ == "__main__":
    unittest.main()
