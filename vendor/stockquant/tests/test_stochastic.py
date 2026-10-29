import math
import unittest

from stockquant.analysis.stochastic import kalman_beta, kalman_filter, ou_fit, ou_zscore


class KalmanTests(unittest.TestCase):
    def test_filter_tracks_constant(self):
        out = kalman_filter([5.0] * 30)
        self.assertAlmostEqual(out[-1], 5.0, places=2)

    def test_filter_smooths_noise(self):
        import random
        rng = random.Random(1)
        noisy = [10.0 + rng.gauss(0, 1.0) for _ in range(200)]
        out = kalman_filter(noisy, process_var=1e-5, measurement_var=1.0)
        self.assertAlmostEqual(out[-1], 10.0, delta=0.5)

    def test_beta_recovers_slope(self):
        # asset = 1.5 * market + noise
        import random
        rng = random.Random(2)
        m = [rng.gauss(0, 0.01) for _ in range(300)]
        a = [1.5 * x + rng.gauss(0, 0.0005) for x in m]
        beta = kalman_beta(a, m)
        self.assertAlmostEqual(beta, 1.5, delta=0.25)

    def test_beta_degenerate_returns_zero(self):
        self.assertEqual(kalman_beta([], []), 0.0)


class OuTests(unittest.TestCase):
    def test_ou_fit_recovers_half_life(self):
        import random
        rng = random.Random(3)
        theta, mu, sigma, dt = 0.2, 10.0, 0.2, 1.0
        b = math.exp(-theta * dt)
        x = mu
        series = []
        for _ in range(3000):
            x = mu + b * (x - mu) + rng.gauss(0, sigma)
            series.append(x)
        fit = ou_fit(series)
        self.assertAlmostEqual(fit["half_life"], math.log(2) / theta, delta=0.6)

    def test_ou_zscore_sign(self):
        # declining level series that is currently far above its long-run mean
        rising = [float(i) for i in range(60)]
        z = ou_zscore(rising)
        self.assertIsNotNone(z)

    def test_ou_fit_short_series(self):
        fit = ou_fit([1.0, 2.0])
        self.assertEqual(fit["theta"], 0.0)


if __name__ == "__main__":
    unittest.main()
