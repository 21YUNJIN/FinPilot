import random
import unittest

from stockquant.analysis.reality_check import stationary_bootstrap, white_reality_check, spa_test


def _series(seed, mean, n=500):
    rng = random.Random(seed)
    return [mean + rng.gauss(0, 0.01) for _ in range(n)]


class StationaryBootstrapTests(unittest.TestCase):
    def test_replicates_have_same_length(self):
        boot = stationary_bootstrap([0.1, 0.2, 0.3, 0.4, 0.5], 20, 3.0, seed=1)
        self.assertEqual(len(boot), 20)
        self.assertTrue(all(len(b) == 5 for b in boot))

    def test_empty_series_is_safe(self):
        self.assertEqual(stationary_bootstrap([], 10, 3.0), [])


class RealityCheckTests(unittest.TestCase):
    def test_rejects_when_best_has_true_edge(self):
        bench = _series(1, 0.0)
        cands = [_series(2, 0.0), _series(3, 0.0), _series(4, 0.002)]  # third has real edge
        out = white_reality_check(bench, cands, n_bootstrap=200, seed=7)
        self.assertGreater(out["statistic"], 0)
        self.assertLess(out["p_value"], 0.1)

    def test_accepts_when_all_are_noise(self):
        bench = _series(1, 0.0)
        cands = [_series(2, 0.0), _series(3, 0.0), _series(4, 0.0)]
        out = white_reality_check(bench, cands, n_bootstrap=200, seed=7)
        self.assertGreater(out["p_value"], 0.05)

    def test_spa_matches_reality_check_on_noise(self):
        bench = _series(1, 0.0)
        cands = [_series(2, 0.0), _series(3, 0.0)]
        out = spa_test(bench, cands, n_bootstrap=200, seed=7)
        self.assertGreater(out["p_value"], 0.05)

    def test_empty_candidates(self):
        out = white_reality_check([0.1, 0.2], [], n_bootstrap=10)
        self.assertEqual(out["n_candidates"], 0)
        self.assertEqual(out["p_value"], 1.0)


if __name__ == "__main__":
    unittest.main()
