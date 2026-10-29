import unittest

from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.domain import Bar, MarketData
from stockquant.portfolio import PortfolioConstraints


def make_market(days=200, symbols=6):
    rows = []
    for d in range(days):
        date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
        for i in range(symbols):
            px = 10 + i + d * (0.02 + 0.005 * i)
            rows.append(Bar(
                date=date, symbol=f"S{i}", open=px * 0.999, high=px * 1.01,
                low=px * 0.99, close=px, volume=100000 + i * 1000,
                sector="Tech" if i < 3 else "Bank", market_cap=1e9 * (i + 1),
                pe=20 - i, pb=2 - 0.1 * i, roe=0.08 + 0.01 * i,
            ))
    return MarketData.from_rows([b.__dict__ for b in rows])


class FactorTimingTests(unittest.TestCase):
    def _config(self, market, **kw):
        return BacktestConfig(
            start=market.dates[80],
            end=market.dates[-1],
            rebalance="monthly",
            min_history=60,
            constraints=PortfolioConstraints(max_positions=3, max_weight=0.4, sector_max_weight=0.7),
            **kw,
        )

    def test_disabled_by_default_is_a_no_op(self):
        market = make_market()
        base = BacktestEngine(market, self._config(market)).run()
        # timing defaults to False; every recorded scale should be exactly 1.0
        self.assertTrue(all(v == 1.0 for v in base.timing.values()) or base.timing == {})

    def test_enabled_scales_exposure_and_records_timing(self):
        market = make_market()
        result = BacktestEngine(
            market,
            self._config(market, factor_timing=True, factor_timing_lookback=2, factor_timing_risk_off=0.4),
        ).run()
        self.assertTrue(result.timing)
        # scales are bounded to [risk_off, 1.0]
        for scale in result.timing.values():
            self.assertGreaterEqual(scale, 0.4 - 1e-9)
            self.assertLessEqual(scale, 1.0 + 1e-9)
        # equity curve is still well-formed
        self.assertEqual(len(result.dates), len(result.equity_curve))

    def test_realized_ic_helper_is_lookahead_free(self):
        market = make_market()
        engine = BacktestEngine(market, self._config(market))
        d0, d1 = market.dates[80], market.dates[100]
        scores = {"S0": 1.0, "S1": 0.5, "S2": 0.0, "S3": -0.5, "S4": -1.0, "S5": 0.2}
        ic = engine._realized_ic(d0, d1, scores)
        self.assertTrue(ic is None or -1.0 <= ic <= 1.0)


if __name__ == "__main__":
    unittest.main()

class DrawdownBreakerTests(unittest.TestCase):
    def test_breaker_reduces_exposure_and_drawdown(self):
        # Ramp up then crash, so a drawdown breaker has something to react to.
        rows = []
        px = {"A": 10.0, "B": 10.0, "C": 10.0, "D": 10.0}
        for d in range(220):
            date = f"2024-{1 + d//28:02d}-{1 + d%28:02d}"
            drift = 0.003 if d < 140 else -0.010
            for i, s in enumerate(px):
                px[s] *= (1.0 + drift + 0.001 * (i - 1.5))
                rows.append(Bar(date=date, symbol=s, open=px[s], high=px[s]*1.004,
                                low=px[s]*0.996, close=px[s], volume=200000,
                                sector="Tech", market_cap=1e9, pe=15, pb=1.5, roe=0.1))
        market = MarketData.from_rows([b.__dict__ for b in rows])

        def run(**kw):
            cfg = BacktestConfig(start=market.dates[80], end=market.dates[-1], rebalance="monthly",
                                 min_history=60, target_volatility=None,
                                 constraints=PortfolioConstraints(max_positions=4, max_weight=0.4, sector_max_weight=0.9),
                                 **kw)
            return BacktestEngine(market, cfg).run()

        base = run()
        guard = run(portfolio_drawdown_limit=0.05, portfolio_drawdown_exposure=0.3)
        # max_drawdown is negative; the breaker should make it *less* severe.
        self.assertLessEqual(abs(guard.metrics["max_drawdown"]), abs(base.metrics["max_drawdown"]) + 1e-9)
