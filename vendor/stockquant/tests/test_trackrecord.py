import random
import unittest

from stockquant.analysis.trackrecord import (
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    harvey_liu_haircut,
    min_backtest_length,
    min_track_record_length,
    multiple_testing_pvalues,
    probabilistic_sharpe_ratio,
    return_statistics,
    track_record_report,
)


def _series(seed, mean_daily=0.0008, sigma=0.01, n=1000):
    rng = random.Random(seed)
    return [mean_daily + rng.gauss(0.0, sigma) for _ in range(n)]


class TrackRecordTests(unittest.TestCase):
    def test_return_statistics_moments(self):
        s = return_statistics([0.01, -0.01, 0.02, -0.02, 0.00])
        self.assertAlmostEqual(s["observations"], 5.0)
        self.assertGreater(s["std"], 0.0)
        self.assertAlmostEqual(s["excess_kurtosis"], s["kurtosis"] - 3.0, places=12)

    def test_expected_max_sharpe_grows_with_trials(self):
        self.assertEqual(expected_max_sharpe_ratio(1), 0.0)
        self.assertLess(expected_max_sharpe_ratio(10), expected_max_sharpe_ratio(100))

    def test_psr_high_for_positive_skill(self):
        self.assertGreater(probabilistic_sharpe_ratio(_series(7)), 0.9)

    def test_dsr_penalizes_more_trials(self):
        returns = _series(7)
        single = deflated_sharpe_ratio(returns, n_trials=1)
        repeated = deflated_sharpe_ratio(returns, n_trials=100)
        self.assertGreater(single, repeated)

    def test_min_trl_shorter_for_higher_sharpe(self):
        strong = _series(1, mean_daily=0.002)
        weak = _series(2, mean_daily=0.0002)
        self.assertLess(min_track_record_length(strong), min_track_record_length(weak))

    def test_min_trl_infinite_for_flat_series(self):
        flat = [0.001] * 500
        self.assertEqual(min_track_record_length(flat), float("inf"))

    def test_min_btl_zero_for_single_trial(self):
        self.assertEqual(min_backtest_length(_series(7), 1), 0.0)

    def test_min_btl_grows_with_trials(self):
        returns = _series(7)
        self.assertLess(min_backtest_length(returns, 2), min_backtest_length(returns, 100))

    def test_haircut_reduces_sharpe(self):
        returns = _series(7)
        sr = return_statistics(returns)["sharpe_annual"]
        h = harvey_liu_haircut(returns, n_trials=100)
        self.assertLess(h["haircut_sharpe_annual"], sr)
        self.assertGreater(h["haircut_sharpe_annual"], 0.0)

    def test_haircut_bonferroni_more_conservative_than_holm(self):
        returns = _series(7)
        bonf = harvey_liu_haircut(returns, 100, method="bonferroni")
        holm = harvey_liu_haircut(returns, 100, method="holm")
        self.assertGreaterEqual(bonf["adjusted_pvalue"], holm["adjusted_pvalue"])

    def test_multiple_testing_bonferroni(self):
        p = [0.005, 0.009, 0.0128, 0.0135, 0.045, 0.06]
        adj = multiple_testing_pvalues(p, method="bonferroni")
        self.assertAlmostEqual(adj[0], 0.03, places=4)
        self.assertAlmostEqual(adj[1], 0.054, places=4)
        self.assertAlmostEqual(adj[4], 0.27, places=4)

    def test_multiple_testing_holm_more_lenient_than_bonferroni(self):
        p = [0.005, 0.009, 0.0128, 0.0135, 0.045, 0.06]
        adj = multiple_testing_pvalues(p, method="holm")
        bonf = multiple_testing_pvalues(p, method="bonferroni")
        self.assertTrue(all(a <= b for a, b in zip(adj, bonf)))

    def test_multiple_testing_bhy_matches_reference(self):
        p = [0.005, 0.009, 0.0128, 0.0135, 0.045, 0.06]
        adj = multiple_testing_pvalues(p, method="bhy")
        self.assertAlmostEqual(adj[0], 0.04956644, places=4)
        self.assertAlmostEqual(adj[4], 0.06, places=6)

    def test_track_record_report_smoke(self):
        r = track_record_report(_series(7), n_trials=50)
        for key in (
            "probabilistic_sharpe_ratio",
            "deflated_sharpe_ratio",
            "min_track_record_length_years",
            "min_backtest_length_years",
            "haircut_sharpe_annual",
        ):
            self.assertIn(key, r)


if __name__ == "__main__":
    unittest.main()
