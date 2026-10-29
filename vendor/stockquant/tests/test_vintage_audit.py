"""Vintage audit must fail closed on claims the current data cannot support."""

import unittest

from stockquant.analysis.vintage_audit import audit_market
from stockquant.domain import Bar, MarketData


class VintageAuditTests(unittest.TestCase):
    def test_audit_reports_unverifiable_vintage_and_historical_labels(self):
        rows = [
            Bar("2024-01-02", "000001.SZ", 10, 10, 10, 10, 1000,
                sector="金融", roe=0.1, fundamentals_available_at="2024-01-02"),
            Bar("2024-01-03", "000001.SZ", 10, 10, 10, 10, 1000,
                sector="金融", roe=0.1, fundamentals_available_at="2024-01-03"),
        ]
        payload = audit_market(MarketData.from_rows([r.__dict__ for r in rows]))
        self.assertEqual(payload["fundamental_rows"], 2)
        self.assertEqual(payload["stamped_fundamental_rows"], 2)
        self.assertFalse(payload["as_reported_vintage_available"])
        self.assertFalse(payload["historical_st_available"])
        self.assertFalse(payload["historical_industry_available"])
        self.assertFalse(payload["fundamental_factor_claims_allowed"])
        self.assertTrue(payload["price_factor_claims_allowed"])


if __name__ == "__main__":
    unittest.main()
