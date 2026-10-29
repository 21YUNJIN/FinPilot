import random
import unittest

try:
    import numpy as _np  # noqa: F401
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False

from stockquant.analysis.cpcv import combinatorial_splits, cpcv_evaluate


class SplitTests(unittest.TestCase):
    def test_number_of_combinations(self):
        splits = combinatorial_splits(60, n_groups=6, n_test_groups=2)
        self.assertEqual(len(splits), 15)  # C(6,2)

    def test_train_and_test_are_disjoint(self):
        for train, test in combinatorial_splits(60, n_groups=6, n_test_groups=2):
            self.assertEqual(set(train) & set(test), set())

    def test_purging_removes_overlapping_training_labels(self):
        # With a large purge window, training dates right before a test block
        # must be dropped.
        train, test = combinatorial_splits(60, n_groups=6, n_test_groups=2, purge_bars=20, embargo_bars=5)[0]
        lo = min(test)
        # nothing in [lo-20, lo) may remain in train
        self.assertFalse([i for i in train if lo - 20 <= i < lo])

    def test_embargo_removes_post_test_training(self):
        train, test = combinatorial_splits(60, n_groups=6, n_test_groups=2, purge_bars=0, embargo_bars=5)[0]
        hi = max(test)
        self.assertFalse([i for i in train if hi < i <= hi + 5])

    def test_invalid_params(self):
        with self.assertRaises(ValueError):
            combinatorial_splits(60, n_groups=1, n_test_groups=1)
        with self.assertRaises(ValueError):
            combinatorial_splits(60, n_groups=4, n_test_groups=4)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for the CPCV evaluate tests")
class EvaluateTests(unittest.TestCase):
    def _panel(self, signal=True, n_dates=60, per_date=20):
        rng = random.Random(7)
        dates, X, y = [], [], []
        for d in range(n_dates):
            date = f"D{d:03d}"
            for _ in range(per_date):
                x1 = rng.gauss(0.0, 1.0)
                x2 = rng.gauss(0.0, 1.0)
                noise = rng.gauss(0.0, 1.0)
                target = (0.8 * x1 - 0.3 * x2 + noise) if signal else noise
                dates.append(date)
                X.append([x1, x2])
                y.append(target)
        return dates, X, y

    def _ols(self, x_train, y_train, x_test):
        import numpy as np
        a = np.column_stack([np.ones(len(x_train)), np.asarray(x_train, dtype=float)])
        b = np.column_stack([np.ones(len(x_test)), np.asarray(x_test, dtype=float)])
        beta, *_ = np.linalg.lstsq(a, np.asarray(y_train, dtype=float), rcond=None)
        return (b @ beta).tolist()

    def test_detects_real_signal(self):
        dates, X, y = self._panel(signal=True)
        out = cpcv_evaluate(dates, X, y, self._ols, n_groups=6, n_test_groups=2, purge_bars=2, embargo_bars=1)
        self.assertTrue(out["available"])
        self.assertGreater(out["paths"], 5)
        self.assertGreater(out["mean_ic"], 0.3)
        self.assertGreater(out["positive_path_rate"], 0.8)

    def test_no_signal_has_ic_near_zero(self):
        dates, X, y = self._panel(signal=False)
        out = cpcv_evaluate(dates, X, y, self._ols, n_groups=6, n_test_groups=2, purge_bars=2, embargo_bars=1)
        self.assertTrue(out["available"])
        self.assertLess(abs(out["mean_ic"]), 0.25)


if __name__ == "__main__":
    unittest.main()
