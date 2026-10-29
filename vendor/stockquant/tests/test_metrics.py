import random
import unittest

from stockquant.backtest.metrics import daily_returns, max_drawdown, performance_metrics


class MetricsTests(unittest.TestCase):
    def test_daily_returns(self):
        self.assertAlmostEqual(daily_returns([100, 110, 99])[0], 0.1)
        self.assertAlmostEqual(daily_returns([100, 110, 99])[1], -0.1)

    def test_max_drawdown(self):
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 95]), -0.25)

    def test_metrics_smoke(self):
        eq = [100.0]
        for _ in range(252):
            eq.append(eq[-1] * 1.0005)
        m = performance_metrics(eq, benchmark=eq, turnover=[0.2] * 12)
        self.assertGreater(m["total_return"], 0)
        self.assertIn("sharpe", m)
        self.assertIn("annualized_turnover", m)

    def test_deflated_sharpe_penalizes_repeated_trials(self):
        rng = random.Random(7)
        returns = [0.0002 + rng.gauss(0.0, 0.01) for _ in range(756)]
        equity = [1.0]
        for value in returns:
            equity.append(equity[-1] * (1.0 + value))
        single = performance_metrics(equity, trials=1)
        repeated = performance_metrics(equity, trials=100)
        self.assertLess(repeated["deflated_sharpe_ratio"], single["deflated_sharpe_ratio"])
        self.assertEqual(repeated["trials"], 100.0)


if __name__ == "__main__":
    unittest.main()


class AutocorrelationTests(unittest.TestCase):
    def test_factor_is_one_for_iid(self):
        from stockquant.backtest.metrics import autocorrelation_factor
        rng = random.Random(11)
        iid = [rng.gauss(0.0, 0.01) for _ in range(2000)]
        self.assertLess(autocorrelation_factor(iid), 1.25)

    def test_factor_exceeds_one_for_positive_autocorrelation(self):
        from stockquant.backtest.metrics import autocorrelation_factor
        rng = random.Random(12)
        series = []
        x = 0.0
        for _ in range(2000):
            x = 0.7 * x + rng.gauss(0.0, 0.01)
            series.append(x)
        self.assertGreater(autocorrelation_factor(series), 1.5)

    def test_sharpe_t_stat_penalised_by_autocorrelation(self):
        from stockquant.backtest.metrics import sharpe_statistics
        rng = random.Random(13)
        series = []
        x = 0.0
        for _ in range(1500):
            x = 0.7 * x + rng.gauss(0.0006, 0.01)
            series.append(x)
        stats = sharpe_statistics(series)
        # The adjusted t-stat must be smaller than the naive iid one.
        self.assertLess(abs(stats["sharpe_t_stat"]), abs(stats["sharpe_t_stat_iid"]))
        self.assertGreater(stats["autocorrelation_factor"], 1.0)

    def test_performance_metrics_reports_adjusted_fields(self):
        from stockquant.backtest.metrics import performance_metrics
        rng = random.Random(14)
        eq = [1.0]
        for _ in range(600):
            eq.append(eq[-1] * (1.0 + rng.gauss(0.0004, 0.01)))
        m = performance_metrics(eq, trials=10)
        for key in ("autocorrelation_factor", "psr_autocorr_adjusted",
                    "dsr_autocorr_adjusted", "sharpe_t_stat", "sharpe_se_annual"):
            self.assertIn(key, m)
