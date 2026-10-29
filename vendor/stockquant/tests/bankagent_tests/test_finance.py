import unittest

from bankagent.finance import compute_metrics, dcf_value, sensitivity_table


class FinanceTests(unittest.TestCase):
    def test_compute_metrics(self):
        bundle = compute_metrics({
            "2023": {"revenue": 80, "net_income": 8, "cfo": 9, "equity": 45, "gross_profit": 24, "total_liabilities": 60, "total_assets": 100},
            "2024": {"revenue": 100, "net_income": 10, "cfo": 7.5, "equity": 50, "gross_profit": 30, "total_liabilities": 75, "total_assets": 100},
        })
        metrics = bundle["metrics"]
        self.assertAlmostEqual(metrics["revenue_growth"], 0.25)
        self.assertAlmostEqual(metrics["net_margin"], 0.10)
        self.assertAlmostEqual(metrics["roe"], 0.20)
        self.assertAlmostEqual(metrics["debt_ratio"], 0.75)
        self.assertTrue(any("cash_conversion" in warning for warning in bundle["warnings"]))

    def test_dcf_rejects_invalid_spread(self):
        with self.assertRaises(ValueError):
            dcf_value(8, 0.03, 0.03, 5)

    def test_sensitivity_table(self):
        rows = sensitivity_table(8, [0.09, 0.11], [0.02, 0.03], 5, net_debt=20)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["valid"] for row in rows))


if __name__ == "__main__":
    unittest.main()
