import tempfile
import unittest

from stockquant.assistant.portfolio import PortfolioLedger


class PortfolioLedgerHoldingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger = PortfolioLedger(f"{self.dir}/portfolio.json")

    def test_add_holding_creates_position(self):
        self.ledger.add_holding("600309.SH", 100, 80.0)
        payload = self.ledger.load()
        self.assertEqual(payload["external_positions"]["600309.SH"]["shares"], 100)
        self.assertAlmostEqual(payload["external_positions"]["600309.SH"]["average_cost"], 80.0)

    def test_add_holding_merges_with_weighted_average(self):
        self.ledger.add_holding("600309.SH", 100, 80.0)
        self.ledger.add_holding("600309.SH", 100, 90.0)
        pos = self.ledger.load()["external_positions"]["600309.SH"]
        self.assertEqual(pos["shares"], 200)
        self.assertAlmostEqual(pos["average_cost"], 85.0)

    def test_reduce_holding_partial_then_clear(self):
        self.ledger.add_holding("600309.SH", 200, 80.0)
        self.ledger.reduce_holding("600309.SH", 50, 85.0)
        pos = self.ledger.load()["external_positions"]["600309.SH"]
        self.assertEqual(pos["shares"], 150)
        self.assertAlmostEqual(pos["average_cost"], 80.0)  # cost basis unchanged on a sell
        self.ledger.reduce_holding("600309.SH", 150, 90.0)
        self.assertNotIn("600309.SH", self.ledger.load()["external_positions"])

    def test_reduce_unknown_symbol_raises(self):
        with self.assertRaises(ValueError):
            self.ledger.reduce_holding("999999.SZ", 100)

    def test_invalid_input_raises(self):
        with self.assertRaises(ValueError):
            self.ledger.add_holding("600309.SH", 0, 80.0)
        with self.assertRaises(ValueError):
            self.ledger.add_holding("600309.SH", 100, 0.0)


if __name__ == "__main__":
    unittest.main()
