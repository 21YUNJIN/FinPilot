import unittest

from stockquant.domain import Bar
from stockquant.portfolio import PortfolioConstraints, build_target_weights


class PortfolioTests(unittest.TestCase):
    def test_caps(self):
        bars = {}
        scores = {}
        sectors = ["Tech", "Tech", "Tech", "Bank", "Consumer", "Energy"]
        for i, sec in enumerate(sectors):
            sym = f"S{i}"
            bars[sym] = Bar(
                date="2024-01-01", symbol=sym, open=10, high=10, low=10, close=10,
                volume=1000, sector=sec, market_cap=1e9, pe=10, pb=1, roe=0.1,
            )
            scores[sym] = 1.0 - i * 0.05
        weights = build_target_weights(
            scores,
            bars,
            PortfolioConstraints(max_positions=6, max_weight=0.25, sector_max_weight=0.40, cash_buffer=0.05),
        )
        self.assertLessEqual(sum(weights.values()), 0.95 + 1e-9)
        self.assertLessEqual(max(weights.values()), 0.25 + 1e-9)
        sec_w = {}
        for sym, w in weights.items():
            sec = bars[sym].sector
            sec_w[sec] = sec_w.get(sec, 0.0) + w
        for value in sec_w.values():
            self.assertLessEqual(value, 0.40 + 1e-9)

    def test_unknown_sector_does_not_cap_total_exposure(self):
        bars = {
            f"S{i}": Bar(date="2024-01-01", symbol=f"S{i}", open=10, high=10, low=10, close=10, volume=1000, sector="Unknown")
            for i in range(5)
        }
        scores = {f"S{i}": 1.0 - i * 0.1 for i in range(5)}
        weights = build_target_weights(scores, bars, PortfolioConstraints(max_positions=5, max_weight=0.25, sector_max_weight=0.10))
        self.assertGreater(sum(weights.values()), 0.90)


if __name__ == "__main__":
    unittest.main()


class TurnoverCapTests(unittest.TestCase):
    def _constraints(self, max_turnover=0.3):
        from stockquant.portfolio import PortfolioConstraints
        return PortfolioConstraints(max_positions=5, max_weight=0.4, sector_max_weight=1.0, max_turnover=max_turnover)

    def _bars(self, symbols):
        from stockquant.domain import Bar
        return {s: Bar(date="2024-01-02", symbol=s, open=10, high=10, low=10, close=10,
                       volume=100000, sector="Tech", market_cap=1e9) for s in symbols}

    def test_turnover_budget_respected(self):
        from stockquant.portfolio import build_target_weights
        scores = {f"S{i}": float(5 - i) for i in range(5)}
        bars = self._bars(scores)
        constraints = self._constraints(max_turnover=0.2)
        current = {"S0": 1.0}  # fully in S0, target wants to move away
        target = build_target_weights(scores, bars, constraints, current_weights=current)
        move = sum(abs(target.get(s, 0.0) - current.get(s, 0.0)) for s in set(target) | set(current))
        one_way = 0.5 * move
        self.assertLessEqual(one_way, 0.2 + 1e-9)

    def test_no_cap_means_full_move(self):
        from stockquant.portfolio import build_target_weights
        scores = {f"S{i}": float(5 - i) for i in range(3)}
        bars = self._bars(scores)
        constraints = self._constraints(max_turnover=1.0)
        current = {"S0": 1.0}
        target = build_target_weights(scores, bars, constraints, current_weights=current)
        self.assertLess(target.get("S0", 0.0), 1.0)  # it did move away

    def test_no_current_weights_is_safe(self):
        from stockquant.portfolio import build_target_weights
        scores = {f"S{i}": float(5 - i) for i in range(4)}
        bars = self._bars(scores)
        target = build_target_weights(scores, bars, self._constraints())
        self.assertEqual(len(target), 4)


if __name__ == "__main__":
    unittest.main()
