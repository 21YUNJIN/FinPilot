import unittest

from stockquant.analysis.pbo import probability_of_backtest_overfitting


class PBOTests(unittest.TestCase):
    def test_stable_winner_has_low_pbo(self):
        rows = []
        for strategy in range(2):
            series = []
            for i in range(120):
                noise = 0.002 * (1 if i % 7 else -1)
                series.append((0.010 if strategy == 0 else -0.005) + noise)
            rows.append(series)
        result = probability_of_backtest_overfitting(rows, n_splits=10)
        self.assertEqual(result["pbo"], 0.0)

    def test_regime_dependent_winner_has_high_pbo(self):
        first = []
        second = []
        for i in range(160):
            block = i // 16
            noise = 0.001 * (1 if i % 3 else -1)
            first.append((0.020 if block % 2 == 0 else -0.020) + noise)
            second.append((-0.020 if block % 2 == 0 else 0.020) + noise)
        result = probability_of_backtest_overfitting([first, second], n_splits=10)
        self.assertGreater(result["pbo"], 0.5)

    def test_rejects_unequal_series(self):
        with self.assertRaises(ValueError):
            probability_of_backtest_overfitting([[0.1, 0.2], [0.1]])


if __name__ == "__main__":
    unittest.main()
