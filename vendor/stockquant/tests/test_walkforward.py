import unittest

from stockquant.factors import DEFAULT_FACTORS
from stockquant.walkforward import CANDIDATES, with_weights


class WalkForwardWeightTests(unittest.TestCase):
    def test_unspecified_factors_are_zeroed(self):
        """A 6-factor preset must not silently keep default weights for the
        fundamental factors (growth/forecast/holder/gross_margin)."""
        weighted = with_weights(DEFAULT_FACTORS, CANDIDATES["balanced"])
        by_name = {f.name: f.weight for f in weighted}
        self.assertAlmostEqual(by_name["value"], 0.20)
        self.assertAlmostEqual(by_name["growth"], 0.0)
        self.assertAlmostEqual(by_name["forecast"], 0.0)
        self.assertAlmostEqual(by_name["holder"], 0.0)
        self.assertAlmostEqual(by_name["gross_margin"], 0.0)
        self.assertAlmostEqual(sum(abs(w) for w in by_name.values()), 1.0, places=6)

    def test_value_low_vol_present_and_fully_specified(self):
        self.assertIn("value_low_vol", CANDIDATES)
        weighted = with_weights(DEFAULT_FACTORS, CANDIDATES["value_low_vol"])
        by_name = {f.name: f.weight for f in weighted}
        self.assertAlmostEqual(by_name["value"], 0.35)
        self.assertAlmostEqual(by_name["low_vol"], 0.35)
        self.assertAlmostEqual(by_name["liquidity"], 0.0)
        # Non-core fundamental factors stay out of the Smart Beta tilt.
        self.assertAlmostEqual(by_name["growth"], 0.0)
        self.assertAlmostEqual(by_name["forecast"], 0.0)

    def test_full_presets_keep_their_explicit_zeros(self):
        weighted = with_weights(DEFAULT_FACTORS, CANDIDATES["forecast_quality"])
        by_name = {f.name: f.weight for f in weighted}
        self.assertAlmostEqual(by_name["value"], 0.0)
        self.assertAlmostEqual(by_name["forecast"], 0.30)


if __name__ == "__main__":
    unittest.main()
