import unittest

try:
    import numpy as _np  # noqa: F401
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False

from stockquant.domain import Bar, MarketData
from stockquant.factors.basic import momentum
from stockquant.factors.engine import winsorized_zscore, FactorEngine, FactorSpec


class FactorTests(unittest.TestCase):
    def test_zscore_centered(self):
        z = winsorized_zscore([1, 2, 3, 4, 100])
        self.assertAlmostEqual(sum(z) / len(z), 0.0, places=10)
        self.assertLess(max(z), 2.5)

    def test_momentum(self):
        bars = []
        for i in range(100):
            bars.append(Bar(
                date=f"2024-01-{i+1:02d}",
                symbol="A",
                open=float(i + 1),
                high=float(i + 1),
                low=float(i + 1),
                close=float(i + 1),
                volume=1000,
            ))
        self.assertGreater(momentum(bars, lookback=60, skip=5), 0)

    def test_factor_engine_scores(self):
        bars = []
        for day in range(1, 121):
            for sym, base in [("A", 10), ("B", 20), ("C", 30)]:
                px = base + day * (0.05 if sym == "A" else 0.02)
                bars.append(Bar(
                    date=f"2024-{1 + (day-1)//28:02d}-{1 + (day-1)%28:02d}",
                    symbol=sym,
                    open=px,
                    high=px * 1.01,
                    low=px * 0.99,
                    close=px,
                    volume=100000,
                    sector="Tech" if sym != "C" else "Bank",
                    market_cap={"A": 1e9, "B": 2e9, "C": 3e9}[sym],
                    pe={"A": 20, "B": 15, "C": 10}[sym],
                    pb={"A": 2, "B": 1.5, "C": 1}[sym],
                    roe={"A": 0.15, "B": 0.10, "C": 0.08}[sym],
                ))
        market = MarketData.from_rows([b.__dict__ for b in bars])
        engine = FactorEngine(min_history=60)
        scores, diag = engine.score(market, market.dates[-1])
        self.assertEqual(diag.scored_count, 3)
        self.assertEqual(set(scores), {"A", "B", "C"})


if __name__ == "__main__":
    unittest.main()


class ExtendedFactorTests(unittest.TestCase):
    def _market(self):
        rows = []
        for day in range(1, 121):
            for sym, base, cap in [("A", 10, 1e9), ("B", 20, 2e9), ("C", 30, 3e9), ("D", 40, 4e9)]:
                drift = 0.02 if sym in ("A", "B") else 0.01
                px = base + day * drift
                rows.append(Bar(
                    date=f"2024-{1 + (day - 1)//28:02d}-{1 + (day - 1)%28:02d}",
                    symbol=sym, open=px, high=px * 1.01, low=px * 0.99, close=px,
                    volume=100000 + day * 1000, sector="Tech", market_cap=cap,
                    pe=20, pb=2, roe=0.12,
                ))
        return MarketData.from_rows([b.__dict__ for b in rows])

    def test_extended_factors_produce_values(self):
        from stockquant.factors import EXTENDED_FACTORS
        market = self._market()
        engine = FactorEngine(factors=EXTENDED_FACTORS, min_history=60, sector_neutral=False)
        scores, diag = engine.score(market, market.dates[-1])
        self.assertGreaterEqual(diag.scored_count, 2)
        self.assertTrue(set(scores))

    def test_min_market_cap_exclusion(self):
        market = self._market()
        full = FactorEngine(min_history=60, sector_neutral=False)
        _, diag_full = full.components(market, market.dates[-1])
        trimmed = FactorEngine(min_history=60, sector_neutral=False, min_market_cap_quantile=0.5)
        _, diag_trimmed = trimmed.components(market, market.dates[-1])
        self.assertEqual(diag_full.scored_count, 4)
        self.assertEqual(diag_trimmed.scored_count, 2)


if __name__ == "__main__":
    unittest.main()


class OrthogonalizationTests(unittest.TestCase):
    def _correlated_market(self):
        import random
        rng = random.Random(9)
        rows = []
        for d in range(160):
            date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
            for i in range(25):
                base = rng.gauss(0.0, 1.0)
                drift = 0.02 + 0.01 * base
                px = 10 + i + d * drift
                rows.append(Bar(
                    date=date, symbol=f"S{i}", open=px, high=px * 1.01, low=px * 0.99,
                    close=px, volume=100000 + int(abs(base) * 1000),
                    sector="Tech" if i % 2 else "Bank", market_cap=1e9 * (i + 1),
                    pe=20, pb=2, roe=0.1,
                ))
        return MarketData.from_rows([b.__dict__ for b in rows])

    @unittest.skipUnless(HAS_NUMPY, "orthogonalization requires numpy")
    def test_orthogonalization_changes_z_scores(self):
        """Regression: an empty first factor used to silently skip the step."""
        from stockquant.factors import EXTENDED_FACTORS
        market = self._correlated_market()
        plain = FactorEngine(EXTENDED_FACTORS, min_history=60, sector_neutral=False)
        orth = FactorEngine(EXTENDED_FACTORS, min_history=60, sector_neutral=False, orthogonalize=True)
        a, _ = plain.components(market, market.dates[-1])
        b, _ = orth.components(market, market.dates[-1])
        active = [n for n in a if a[n]]
        self.assertTrue(active)
        changed = any(abs(a[n][s] - b[n][s]) > 1e-9 for n in active for s in a[n] if s in b.get(n, {}))
        self.assertTrue(changed, "orthogonalization should alter at least some z-scores")

    def test_orthogonalization_is_noop_without_numpy(self):
        # Whatever the environment, the engine must still return valid z-scores.
        market = self._correlated_market()
        orth = FactorEngine(min_history=60, sector_neutral=False, orthogonalize=True)
        z, diag = orth.components(market, market.dates[-1])
        self.assertGreater(diag.scored_count, 0)
        self.assertTrue(all(isinstance(v, float) for m in z.values() for v in m.values()))

class ExtendedFactorSmokeTests(unittest.TestCase):
    def _bars(self, prices):
        out=[]
        for i,p in enumerate(prices):
            out.append(Bar(date=f"2024-01-{i+1:02d}", symbol="A", open=p, high=p*1.01,
                           low=p*0.99, close=p, volume=1_000_000, sector="Tech",
                           market_cap=1_000_000_000.0))
        return out
    def test_max_effect_is_negative_max_return(self):
        from stockquant.factors.engine import _factor_max_effect
        bars = self._bars([10.0] + [10.0*(1+0.01*i) for i in range(1, 25)])
        self.assertLess(_factor_max_effect(None, "A", bars), 0)
    def test_turnover_is_negative(self):
        from stockquant.factors.engine import _factor_turnover
        bars = self._bars([10.0]*25)
        self.assertLess(_factor_turnover(None, "A", bars), 0)


if __name__ == "__main__":
    unittest.main()
