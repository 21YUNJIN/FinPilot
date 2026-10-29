import os
import tempfile
import unittest

from stockquant.data import generate_demo_market, load_csv, write_csv


class DataTests(unittest.TestCase):
    def test_csv_roundtrip(self):
        rows = generate_demo_market(start="2024-01-01", end="2024-02-01", seed=1)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "data.csv")
            write_csv(rows, path)
            market = load_csv(path)
            self.assertGreater(len(market.dates), 10)
            self.assertGreater(len(market.symbols), 5)


if __name__ == "__main__":
    unittest.main()
