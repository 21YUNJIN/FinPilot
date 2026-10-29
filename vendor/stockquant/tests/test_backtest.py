import unittest

from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.domain import Bar, MarketData
from stockquant.portfolio import PortfolioConstraints


def make_market(days=160, symbols=6):
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


class BacktestTests(unittest.TestCase):
    def test_execution_happens_after_signal(self):
        market = make_market()
        cfg = BacktestConfig(
            start=market.dates[20],
            end=market.dates[-1],
            rebalance="monthly",
            min_history=60,
            constraints=PortfolioConstraints(max_positions=3, max_weight=0.4, sector_max_weight=0.7),
        )
        result = BacktestEngine(market, cfg).run()
        self.assertEqual(len(result.dates), len(result.equity_curve))
        self.assertEqual(len(result.dates), len(result.benchmark_curve))
        for trade in result.trades:
            self.assertGreater(trade.date, trade.signal_date)


if __name__ == "__main__":
    unittest.main()
