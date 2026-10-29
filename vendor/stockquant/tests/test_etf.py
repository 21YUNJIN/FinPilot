"""ETF pilot strategy: invariants that must hold regardless of the data."""
from __future__ import annotations

import unittest

from stockquant.domain import Bar, MarketData
from stockquant.etf import (
    EtfConfig,
    EtfCostConfig,
    apply_lot_constraints,
    lot_rounded_weights,
    plan_orders,
    run_etf_backtest,
    sleeve_targets,
)


def make_market(days=320, vol=None):
    """Three sleeves with different volatilities + one very expensive sleeve."""
    vol = vol or {"A": 0.005, "B": 0.02, "C": 0.01, "D": 0.01}
    price = {"A": 4.0, "B": 6.0, "C": 100.0, "D": 140.0}
    drift = {"A": 0.0004, "B": 0.0006, "C": 0.0002, "D": 0.0002}
    rows = []
    import random
    rng = random.Random(3)
    for d in range(days):
        date = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
        for symbol in price:
            shock = rng.gauss(0.0, vol[symbol]) if symbol != "A" else vol["A"] * ((-1) ** d)
            price[symbol] *= 1.0 + drift[symbol] + shock
            rows.append(Bar(date=date, symbol=symbol, open=price[symbol], high=price[symbol] * 1.002,
                            low=price[symbol] * 0.998, close=price[symbol], volume=10_000_000,
                            sector=symbol, market_cap=0.0))
    return MarketData.from_rows([b.__dict__ for b in rows])


class SleeveTargetTests(unittest.TestCase):
    def test_weights_sum_to_investable_and_respect_cap(self):
        market = make_market()
        cfg = EtfConfig(symbols=("A", "B", "C"), max_weight=0.35, cash_buffer=0.02)
        targets = sleeve_targets(market, market.dates[-1], cfg)
        self.assertTrue(targets)
        self.assertAlmostEqual(sum(targets.values()), 0.98, places=9)
        self.assertTrue(all(w <= 0.35 + 1e-9 for w in targets.values()))

    def test_low_vol_sleeve_gets_more_weight(self):
        market = make_market()
        cfg = EtfConfig(symbols=("A", "B"), max_weight=1.0, cash_buffer=0.0)
        targets = sleeve_targets(market, market.dates[-1], cfg)
        # A is the near-riskless sleeve, B has 4x the volatility -> A must dominate
        self.assertGreater(targets["A"], targets["B"])

    def test_insufficient_history_is_excluded_not_defaulted(self):
        market = make_market(days=100)          # < trend_lookback (200)
        cfg = EtfConfig(symbols=("A", "B"), trend_lookback=200)
        self.assertEqual(sleeve_targets(market, market.dates[-1], cfg), {})

    def test_trend_filter_scales_down_a_sleeve(self):
        market = make_market(days=320)
        cfg = EtfConfig(symbols=("B",), max_weight=1.0, cash_buffer=0.0)
        full = sleeve_targets(market, market.dates[-1], cfg)
        risk_off = sleeve_targets(market, market.dates[-1],
                                  EtfConfig(symbols=("B",), max_weight=1.0, cash_buffer=0.0,
                                            risk_off_multiplier=0.0))
        # Either the trend filter fires (empty book = cash) or it does not; it must
        # never scale a sleeve *up*, and an all-risk-off universe must not crash.
        self.assertLessEqual(sum(risk_off.values()) + 1e-12, sum(full.values()) + 1e-12)
        self.assertIsInstance(risk_off, dict)


class LotConstraintTests(unittest.TestCase):
    def test_expensive_sleeve_is_dropped_and_weight_redistributed(self):
        cost = EtfCostConfig(lot_size=100)
        targets = {"A": 0.5, "D": 0.48}          # D ~ 140 CNY -> one lot = 14,000
        prices = {"A": 4.0, "D": 140.0}
        effective, dropped = apply_lot_constraints(20_000.0, targets, prices, cost,
                                                  max_lot_fraction=0.5, max_weight=0.99)
        self.assertIn("D", dropped)
        self.assertNotIn("D", effective)
        self.assertAlmostEqual(sum(effective.values()), 0.98, places=9)   # nothing left in cash
        self.assertAlmostEqual(effective["A"], 0.98, places=9)

    def test_cap_binds_when_only_one_sleeve_remains(self):
        cost = EtfCostConfig(lot_size=100)
        effective, dropped = apply_lot_constraints(20_000.0, {"A": 0.5, "D": 0.48},
                                                  {"A": 4.0, "D": 140.0}, cost,
                                                  max_lot_fraction=0.5, max_weight=0.35)
        self.assertAlmostEqual(effective["A"], 0.35, places=9)   # capped; the rest is cash
        self.assertIn("D", dropped)

    def test_large_account_can_hold_the_expensive_sleeve(self):
        cost = EtfCostConfig(lot_size=100)
        effective, dropped = apply_lot_constraints(200_000.0, {"A": 0.5, "D": 0.48},
                                                  {"A": 4.0, "D": 140.0}, cost, max_lot_fraction=0.5)
        self.assertEqual(dropped, {})
        self.assertIn("D", effective)


class LotRoundedWeightTests(unittest.TestCase):
    def test_actual_weight_reflects_whole_lots(self):
        cost = EtfCostConfig(lot_size=100)
        weights, dropped = lot_rounded_weights(
            50_000.0, {"A": 0.33}, {"A": 4.0}, cost,
            max_lot_fraction=0.5, max_weight=0.35,
        )
        # floor(16,500 / 400) = 41 lots -> 16,400 / 50,000 = 0.328
        self.assertEqual(dropped, {})
        self.assertAlmostEqual(weights["A"], 0.328, places=9)

    def test_expensive_sleeve_is_explicitly_dropped(self):
        cost = EtfCostConfig(lot_size=100)
        weights, dropped = lot_rounded_weights(
            20_000.0, {"A": 0.5, "D": 0.48}, {"A": 4.0, "D": 140.0}, cost,
            max_lot_fraction=0.5, max_weight=0.99,
        )
        self.assertIn("D", dropped)
        self.assertNotIn("D", weights)


class PlanOrderTests(unittest.TestCase):
    def test_band_suppresses_small_trades(self):
        cost = EtfCostConfig(lot_size=100, rebalance_band=0.01, min_trade_weight=0.005)
        orders = plan_orders(20_000.0, {"A": 0.50}, {"A": 0.50 * 20_000}, {"A": 4.0}, cost)
        self.assertEqual(orders, [])

    def test_sells_come_before_buys_and_sizes_are_lots(self):
        cost = EtfCostConfig(lot_size=100, rebalance_band=0.0, min_trade_weight=0.0)
        orders = plan_orders(20_000.0, {"A": 0.5, "B": 0.0},
                             {"B": 6_000.0}, {"A": 4.0, "B": 6.0}, cost)
        self.assertEqual([o["side"] for o in orders], ["SELL", "BUY"])
        for order in orders:
            self.assertEqual(order["shares"] % 100, 0)


class EtfBacktestTests(unittest.TestCase):
    def test_backtest_runs_with_next_open_execution_and_costs(self):
        market = make_market(days=320)
        cfg = EtfConfig(symbols=("A", "B", "C"), trend_lookback=200)
        cost = EtfCostConfig(initial_cash=20_000.0, commission_bps=1.0, stamp_duty_bps=0.0,
                            slippage_bps=2.0)
        result = run_etf_backtest(market, cfg, cost, benchmark_symbol="A")
        self.assertEqual(len(result.dates), len(result.equity_curve))
        self.assertTrue(result.trades)
        for trade in result.trades:
            self.assertGreater(trade.date, trade.signal_date)
            self.assertGreater(trade.commission, 0.0)
            self.assertEqual(trade.shares % 100, 0)
        self.assertIn("years", result.metrics)
        self.assertGreater(result.metrics["years"], 0)

    def test_no_lookahead_in_the_signal(self):
        """Signals are computed from the close and executed on the next session."""
        market = make_market(days=320)
        result = run_etf_backtest(market, EtfConfig(symbols=("A", "B")), EtfCostConfig())
        for trade in result.trades:
            self.assertIn(trade.signal_date, result.target_history)
            self.assertLess(trade.signal_date, trade.date)


class AnalystRevisionFeatureTests(unittest.TestCase):
    """PIT rules and window math for the pre-registered C-line signal."""

    def _events(self):
        from stockquant.data.analyst import Event, RATING_SCORE
        raw = [
            ("2024-01-10", "增持", "A"), ("2024-02-10", "增持", "B"), ("2024-03-01", "买入", "A"),
            ("2024-04-01", "买入", "B"), ("2024-05-01", "买入", "C"), ("2024-06-01", "买入", "D"),
        ]
        return {"X": [Event(d, RATING_SCORE[r], inst) for d, r, inst in raw]}

    def test_forward_return_and_window_math(self):
        from stockquant.data.analyst import revision_features

        # Windows are half-open [start, end):
        #   recent = [2024-04-02, 2024-07-01) -> 2 reports, both 买入 (4.0)
        #   prior  = [2024-01-03, 2024-04-02) -> 4 reports (3,3,4,4) -> 3.5
        import math
        feats = revision_features(self._events(), "2024-07-01", window_days=90)
        self.assertIn("X", feats)
        self.assertAlmostEqual(feats["X"]["rating_level"], 4.0, places=9)
        self.assertAlmostEqual(feats["X"]["rating_rev_3m"], 0.5, places=9)
        self.assertAlmostEqual(feats["X"]["coverage_rev_3m"], math.log(3) - math.log(5), places=9)

    def test_report_is_not_visible_on_its_own_date(self):
        from stockquant.data.analyst import revision_features

        # The 2024-06-01 report must NOT count at asof=2024-06-01 (date-only stamp
        # is "published after the close" -> usable from the next session).
        on_date = revision_features(self._events(), "2024-06-01", window_days=90)
        next_day = revision_features(self._events(), "2024-06-02", window_days=90)
        self.assertNotEqual(on_date.get("X"), next_day.get("X"))

    def test_insufficient_coverage_is_missing_not_neutral(self):
        from stockquant.data.analyst import Event, RATING_SCORE, revision_features

        sparse = {"Y": [Event("2024-06-01", RATING_SCORE["买入"], "A")]}
        self.assertEqual(revision_features(sparse, "2024-07-01", window_days=90), {})

    def test_dedup_keeps_one_report_per_institution(self):
        from stockquant.data.analyst import Event, RATING_SCORE, revision_features

        events = {"Z": [Event(f"2024-0{m}-01", RATING_SCORE["买入"], "SameHouse") for m in (1, 2, 3, 4, 5, 6)]}
        plain = revision_features(events, "2024-07-01", window_days=90)
        dedup = revision_features(events, "2024-07-01", window_days=90, dedup=True)
        # same-institution spam inflates coverage in the plain version only
        self.assertGreater(plain["Z"]["coverage_rev_3m"], dedup.get("Z", {}).get("coverage_rev_3m", -9))


class AnalystSymbolKeyTests(unittest.TestCase):
    def test_cache_codes_map_onto_panel_symbols(self):
        from stockquant.data.analyst import canonical_symbol
        self.assertEqual(canonical_symbol("000001"), "000001.SZ")
        self.assertEqual(canonical_symbol("600519"), "600519.SH")
        self.assertEqual(canonical_symbol("300750"), "300750.SZ")
        self.assertEqual(canonical_symbol("430047"), "430047.BJ")
        self.assertEqual(canonical_symbol("000001.SZ"), "000001.SZ")   # already canonical
