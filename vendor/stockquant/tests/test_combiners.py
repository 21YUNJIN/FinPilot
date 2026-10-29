import random
import unittest

try:
    import numpy as _np  # noqa: F401
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False

from stockquant.analysis.combiners import equal_weight, ic_weight, ridge


def _panel(n=3000, k=3, signal=True, seed=3):
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        row = [rng.gauss(0, 1) for _ in range(k)]
        target = (0.7 * row[0] - 0.4 * row[1]) if signal else 0.0
        y.append(target + rng.gauss(0, 1.0))
        X.append(row)
    return X, y


class CombinerTests(unittest.TestCase):
    def test_equal_weight_is_mean_of_row(self):
        f = equal_weight()
        out = f([[1.0, 2.0], [3.0, 4.0]], [0.0, 0.0], [[1.0, 3.0]])
        self.assertAlmostEqual(out[0], 2.0)

    def test_ic_weight_recovers_positive_factor(self):
        X, y = _panel(signal=True)
        f = ic_weight()
        preds = f(X, y, X[:400])
        self.assertGreater(max(preds) - min(preds), 0.1)

    @unittest.skipUnless(HAS_NUMPY, "ridge requires numpy")
    def test_ridge_predicts_direction(self):
        X, y = _panel(signal=True)
        f = ridge()
        preds = f(X, y, X[:400])
        actual = y[:400]
        # predictions should correlate positively with the target direction
        from stockquant.analysis.combiners import _corr
        self.assertGreater(_corr(preds, actual), 0.1)

    @unittest.skipUnless(HAS_NUMPY, "ridge requires numpy")
    def test_ridge_handles_no_signal(self):
        X, y = _panel(signal=False)
        f = ridge()
        preds = f(X, y, X[:400])
        self.assertEqual(len(preds), 400)


if __name__ == "__main__":
    unittest.main()
