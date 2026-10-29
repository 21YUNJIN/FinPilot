"""Regression tests for the 2026-10-16 audit round.

Each test pins a specific failure mode that was found by reading the code rather
than by a red test, so they are written as invariants (what must never happen)
instead of golden numbers.
"""
import csv
import math
import random
import tempfile
import unittest
from pathlib import Path

from stockquant.domain import Bar, DuplicateBarError, MarketData
from stockquant.data.validation import validate_market
from stockquant.factors.engine import (
    FactorEngine,
    FactorSpec,
    PITAvailabilityError,
    gate_fundamentals,
    pit_usable,
)
from stockquant.factors.basic import returns_by_date
from stockquant.portfolio.optimizer import estimate_covariance, optimize_target_weights, returns_by_date as opt_returns
from stockquant.portfolio.risk import portfolio_volatility, target_volatility_scale
from stockquant.portfolio.sizing import kelly_weights
from stockquant.portfolio import PortfolioConstraints
from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.backtest.metrics import performance_metrics
from stockquant.convertible.double_low import (
    execution_price,
    first_executable,
    run_double_low,
    tradeable_price,
    valuation_price,
    _rank,
)

try:
    import numpy as np  # noqa: F401
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False


def make_bar(date, symbol, close=10.0, volume=1000, **kwargs):
    fields = dict(
        date=date, symbol=symbol, open=close, high=close * 1.01, low=close * 0.99,
        close=close, volume=volume, sector="Tech", market_cap=1e9,
        pe=15, pb=1.5, roe=0.1,
    )
    fields.update(kwargs)
    return Bar(**fields)


def flat_market(days=120, symbols=("A", "B", "C")):
    rows = []
    for d in range(days):
        date = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
        for symbol in symbols:
            rows.append(make_bar(date, symbol))
    return MarketData.from_rows([b.__dict__ for b in rows])


class DuplicateKeyTests(unittest.TestCase):
    def test_from_rows_rejects_duplicates_by_default(self):
        rows = [make_bar("2024-01-02", "A").__dict__, make_bar("2024-01-02", "A").__dict__]
        with self.assertRaises(DuplicateBarError):
            MarketData.from_rows(rows)

    def test_opt_in_last_wins_records_the_collision(self):
        rows = [make_bar("2024-01-02", "A").__dict__, make_bar("2024-01-02", "A").__dict__]
        market = MarketData.from_rows(rows, strict_duplicates=False)
        self.assertEqual(market.duplicate_keys, 1)
        report = validate_market(market)
        self.assertEqual(report.duplicate_keys, 1)
        self.assertFalse(report.ok)
        self.assertTrue(any("duplicated" in issue for issue in report.issues))

    def test_csv_loader_rejects_duplicate_rows(self):
        from stockquant.data import load_csv

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "dupes.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["date", "symbol", "open", "high", "low", "close", "volume"])
                writer.writeheader()
                for _ in range(2):
                    writer.writerow({"date": "2024-01-02", "symbol": "A", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100})
            with self.assertRaises(ValueError):
                load_csv(str(path), use_cache=False)


class MembershipTests(unittest.TestCase):
    def _market(self):
        rows = []
        for date in ("2024-01-01", "2024-01-02", "2024-02-01"):
            for symbol in ("A", "B"):
                rows.append(make_bar(date, symbol))
        return MarketData.from_rows([b.__dict__ for b in rows])

    def test_dates_before_first_snapshot_have_empty_universe(self):
        market = self._market()
        market.membership_by_date = {"2024-02-01": {"B"}}
        # The old code back-filled the first *future* snapshot into the past.
        self.assertEqual(market.members_at("2024-01-01"), set())
        self.assertEqual(market.snapshot("2024-01-01"), {})
        self.assertEqual(sorted(market.snapshot("2024-02-01")), ["B"])

    def test_span_and_uncovered_dates_are_reported(self):
        market = self._market()
        market.membership_by_date = {"2024-02-01": {"B"}}
        self.assertEqual(market.membership_span, ("2024-02-01", "2024-02-01"))
        self.assertEqual(market.uncovered_membership_dates(market.dates), ["2024-01-01", "2024-01-02"])

    def test_no_membership_data_means_no_filter(self):
        market = self._market()
        self.assertIsNone(market.members_at("2024-01-01"))
        self.assertEqual(len(market.snapshot("2024-01-01")), 2)


class BarTradabilityTests(unittest.TestCase):
    def test_zero_volume_is_not_tradable(self):
        bar = make_bar("2024-01-02", "A", volume=0)
        self.assertFalse(bar.quote_fresh())
        self.assertFalse(bar.tradable())
        self.assertFalse(bar.can_buy())
        self.assertFalse(bar.can_sell())

    def test_limit_blocks_directional_fills(self):
        limit_up = make_bar("2024-01-02", "A", limit_up=True)
        self.assertFalse(limit_up.can_buy())
        self.assertTrue(limit_up.can_sell())
        limit_down = make_bar("2024-01-02", "A", limit_down=True)
        self.assertTrue(limit_down.can_buy())
        self.assertFalse(limit_down.can_sell())

    def test_st_names_are_not_buyable_but_stay_sellable(self):
        st = make_bar("2024-01-02", "A", is_st=True)
        self.assertFalse(st.can_buy())
        self.assertTrue(st.can_sell())
        self.assertFalse(st.tradable())


class PitPolicyTests(unittest.TestCase):
    def test_pit_usable_rules(self):
        # date-only stamp == "published that day, after the close"
        self.assertFalse(pit_usable("2024-04-30", "2024-04-30"))
        self.assertTrue(pit_usable("2024-04-30", "2024-05-06"))
        # explicit timestamps compare against the session close
        self.assertTrue(pit_usable("2024-04-30 11:00:00", "2024-04-30"))
        self.assertFalse(pit_usable("2024-04-30 19:00:00", "2024-04-30"))
        # unknown availability is never "known"
        self.assertFalse(pit_usable("", "2024-04-30"))

    def test_gate_drops_announcement_fields_only(self):
        bar = make_bar("2024-04-30", "A")
        bar = Bar(**{**bar.__dict__, "fundamentals_available_at": "2024-04-30"})
        gated, was_gated = gate_fundamentals(bar, "2024-04-30")
        self.assertTrue(was_gated)
        self.assertIsNone(gated.pe)
        self.assertIsNone(gated.roe)
        self.assertEqual(gated.close, bar.close)

    def _pit_market(self, stamp):
        """Two years of daily bars with a single announcement stamp."""
        from datetime import date as _date, timedelta

        rows = []
        start = _date(2023, 1, 2)
        for d in range(540):
            date = (start + timedelta(days=d)).isoformat()
            for symbol, base in (("A", 10.0), ("B", 12.0)):
                px = base + d * 0.01
                bar = make_bar(date, symbol, close=px, pe=20, pb=2, roe=0.1)
                rows.append({**bar.__dict__, "fundamentals_available_at": stamp})
        return MarketData.from_rows(rows)

    def test_fundamentals_are_gated_until_they_are_public(self):
        market = self._pit_market("2024-02-05")
        engine = FactorEngine(
            factors=(FactorSpec("value", 1.0, lambda m, s, bars: (None if bars[-1].pe is None else 1.0 / bars[-1].pe), "value"),),
            min_history=60,
            sector_neutral=False,
            pit_policy="session",
        )
        scores_during, diag_during = engine.score(market, "2024-02-05")
        self.assertEqual(scores_during, {})
        self.assertGreater(diag_during.pit_gated, 0)

        scores_after, _ = engine.score(market, "2024-03-20")
        self.assertTrue(scores_after)
        self.assertEqual(diag_during.pit_unverified, 0)

    def test_strict_policy_refuses_unstamped_fundamentals(self):
        market = flat_market()  # pe/pb/roe set, no availability stamp
        engine = FactorEngine(min_history=60, sector_neutral=False, pit_policy="strict")
        with self.assertRaises(PITAvailabilityError):
            engine.score(market, market.dates[-1])

    def test_session_policy_reports_unverified_but_keeps_working(self):
        market = flat_market()
        engine = FactorEngine(min_history=60, sector_neutral=False, pit_policy="session")
        scores, diag = engine.score(market, market.dates[-1])
        self.assertTrue(scores)
        self.assertGreater(diag.pit_unverified, 0)


class FactorEngineRobustnessTests(unittest.TestCase):
    def test_factor_exceptions_are_reported_not_swallowed(self):
        def boom(market, symbol, bars):
            raise ZeroDivisionError("schema drift")

        def ok(name):
            return FactorSpec(name, 1.0, lambda m, s, bars: float({"A": 1, "B": 2, "C": 3}[s]), name)

        engine = FactorEngine(
            factors=(ok("f1"), ok("f2"), ok("f3"), ok("f4"), FactorSpec("boom", 1.0, boom, "broken")),
            min_history=30,
            sector_neutral=False,
        )
        market = flat_market(days=60)
        with self.assertLogs("stockquant.factors.engine", level="WARNING"):
            scores, diag = engine.score(market, market.dates[-1])
        self.assertTrue(scores)  # one live factor still scores the book
        self.assertEqual(diag.error_by_factor.get("boom"), 3)
        self.assertTrue(engine.error_log)

    def test_missing_factor_is_neutral_not_renormalised(self):
        """A missing factor must not act as a free option.

        Fixed-denominator scoring (missing -> 0) has to agree with explicit neutral
        imputation; the old per-name renormalisation divided by a smaller weight
        and silently rescaled stocks that were missing factors.
        """
        values = {"A": 3.0, "B": 2.0, "C": 1.0}
        scoring = {name: (lambda m, s, bars, _v=v: _v.get(s)) for name, v in (
            ("f1", values), ("f2", values), ("f3", values), ("f4", values),
        )}

        def f5(market, symbol, bars):
            return None if symbol == "C" else values[symbol]

        factors = tuple([FactorSpec(name, 1.0, fn, name) for name, fn in scoring.items()] +
                        [FactorSpec("f5", 1.0, f5, "partial")])
        market = flat_market(days=60)

        imputed = FactorEngine(factors=factors, min_history=30, sector_neutral=False, impute_missing=True)
        partial = FactorEngine(factors=factors, min_history=30, sector_neutral=False, impute_missing=False)
        score_imputed, _ = imputed.score(market, market.dates[-1])
        score_partial, _ = partial.score(market, market.dates[-1])
        self.assertIn("C", score_partial)
        self.assertAlmostEqual(score_partial["C"], score_imputed["C"], places=12)

    def test_amihud_factor_prefers_the_liquid_name(self):
        from stockquant.factors.engine import _factor_amihud

        bars_liquid = [make_bar(f"2024-01-{i + 1:02d}", "A", close=10 + 0.01 * i, volume=1_000_000) for i in range(30)]
        bars_illiquid = [make_bar(f"2024-01-{i + 1:02d}", "B", close=10 + 0.01 * i, volume=1_000) for i in range(30)]
        liquid = _factor_amihud(None, "A", bars_liquid)
        illiquid = _factor_amihud(None, "B", bars_illiquid)
        self.assertIsNotNone(liquid)
        self.assertIsNotNone(illiquid)
        self.assertGreater(liquid, illiquid)


class ReturnAlignmentTests(unittest.TestCase):
    def test_returns_by_date_has_no_zero_padding(self):
        bars = [
            make_bar("2024-01-01", "A", close=10.0, volume=1000),
            make_bar("2024-01-02", "A", close=10.0, volume=0),      # halt day
            make_bar("2024-01-03", "A", close=11.0, volume=1000),
        ]
        series = returns_by_date(bars)
        # The halt session has no observation at all (no fabricated 0.0), and the
        # move from the last traded price to the resumed price lands on 01-03.
        self.assertNotIn("2024-01-02", series)
        self.assertAlmostEqual(series["2024-01-03"], 0.1, places=12)

    def test_covariance_fails_closed_without_overlap(self):
        rows = []
        for d in range(1, 41):
            rows.append(make_bar(f"2024-01-{d:02d}", "A", close=10 + 0.05 * d))
        for d in range(1, 41):
            rows.append(make_bar(f"2024-03-{d:02d}", "B", close=20 + 0.05 * d))
        market = MarketData.from_rows([b.__dict__ for b in rows])
        cov, kept = estimate_covariance(market, ["A", "B"], market.dates[-1], lookback=60)
        self.assertIsNone(cov)
        self.assertEqual(kept, [])

    def test_beta_alignment_helper_is_date_keyed(self):
        market = flat_market(days=40)
        series = opt_returns(market, "A", market.dates[-1], 10)
        self.assertTrue(all(isinstance(k, str) for k in series))


@unittest.skipUnless(HAS_NUMPY, "numpy required")
class PortfolioRiskTests(unittest.TestCase):
    def _market(self, days=140, seed=5):
        rng = random.Random(seed)
        rows = []
        px = {"S0": 10.0, "S1": 11.0, "S2": 12.0, "S3": 13.0}
        base = 0.0
        for d in range(days):
            date = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
            base = 0.6 * base + rng.gauss(0, 0.01)
            for symbol in px:
                r = (base + rng.gauss(0, 0.001)) if symbol in ("S0", "S1") else rng.gauss(0, 0.015)
                px[symbol] *= 1 + r
                rows.append(make_bar(date, symbol, close=px[symbol], market_cap=1e9))
        return MarketData.from_rows([b.__dict__ for b in rows])

    def test_portfolio_volatility_uses_the_actual_book(self):
        market = self._market()
        date = market.dates[-1]
        concentrated = portfolio_volatility(market, date, {"S0": 0.5, "S1": 0.5}, lookback=60)
        diversified = portfolio_volatility(market, date, {"S0": 0.25, "S1": 0.25, "S2": 0.25, "S3": 0.25}, lookback=60)
        self.assertIsNotNone(concentrated)
        self.assertIsNotNone(diversified)
        # Two 0.99-correlated names are riskier than four with two independent legs.
        self.assertGreater(concentrated, diversified)

    def test_target_volatility_scales_the_book_not_the_universe(self):
        market = self._market()
        date = market.dates[-1]
        weights = {"S2": 0.5, "S3": 0.5}
        est = portfolio_volatility(market, date, weights, lookback=60)
        self.assertIsNotNone(est)
        target = est * 0.5
        scale, reported, source = target_volatility_scale(market, date, target, weights=weights, lookback=60)
        self.assertEqual(source, "portfolio")
        self.assertAlmostEqual(reported, est, places=10)
        self.assertAlmostEqual(scale * reported, target, places=10)

    def test_optimizer_candidate_pool_can_pick_a_diversifier(self):
        market = self._market()
        date = market.dates[-1]
        bars = market.snapshot(date)
        scores = {"S0": 3.0, "S1": 2.0, "S2": 1.0, "S3": 0.5}
        wide = PortfolioConstraints(max_positions=2, max_weight=1.0, sector_max_weight=1.0,
                                    cash_buffer=0.0, weight_scheme="optimize")
        narrow = optimize_target_weights(market, scores, bars, date, wide, alpha_scale=0.0,
                                         risk_aversion=3.0, candidate_multiplier=1.0)
        wide_pick = optimize_target_weights(market, scores, bars, date, wide, alpha_scale=0.0,
                                            risk_aversion=3.0, candidate_multiplier=3.0)
        self.assertLessEqual(len(narrow), 2)
        self.assertTrue(set(narrow) <= {"S0", "S1"})
        self.assertLessEqual(len(wide_pick), 2)
        self.assertTrue(any(s in ("S2", "S3") for s in wide_pick))

    def test_kelly_fraction_controls_gross_exposure(self):
        market = self._market()
        date = market.dates[-1]
        scores = {"S0": 2.0, "S1": 1.0, "S2": 0.5, "S3": 0.2}
        small = kelly_weights(market, scores, list(scores), date, fraction=0.25, cap=0.9, investable=0.97)
        large = kelly_weights(market, scores, list(scores), date, fraction=1.0, cap=0.9, investable=0.97)
        self.assertLess(sum(small.values()), sum(large.values()) - 1e-9)


class TurnoverBasisTests(unittest.TestCase):
    def test_metrics_report_both_bases(self):
        equity = [1.0 * (1.0005 ** i) for i in range(253)]
        metrics = performance_metrics(equity, turnover=[0.1] * 12, gross_turnover=[0.2] * 12)
        self.assertAlmostEqual(metrics["annualized_turnover"], 12 * 0.1)
        self.assertAlmostEqual(metrics["annualized_gross_turnover"], 12 * 0.2)
        self.assertEqual(metrics["turnover_basis"], 1.0)

    def test_backtest_records_one_way_and_gross_consistently(self):
        market = flat_market(days=200, symbols=("A", "B", "C", "D"))
        cfg = BacktestConfig(start=market.dates[80], end=market.dates[-1], rebalance="monthly",
                             min_history=60, target_volatility=None,
                             constraints=PortfolioConstraints(max_positions=2, max_weight=0.6, sector_max_weight=1.0))
        result = BacktestEngine(market, cfg).run()
        self.assertEqual(len(result.turnover), len(result.gross_turnover))
        for one_way, gross in zip(result.turnover, result.gross_turnover):
            self.assertAlmostEqual(one_way * 2.0, gross, places=12)


class BenchmarkIntegrityTests(unittest.TestCase):
    def test_missing_market_cap_is_dropped_not_replaced_by_turnover(self):
        rows = []
        for d in range(120):
            date = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
            for symbol, px in (("A", 10.0 + 0.01 * d), ("B", 20.0 + 0.01 * d)):
                bar = make_bar(date, symbol, close=px)
                rows.append({**bar.__dict__, "market_cap": 0.0})
        market = MarketData.from_rows(rows)
        cfg = BacktestConfig(start=market.dates[60], end=market.dates[-1], rebalance="monthly",
                             min_history=60, target_volatility=None, benchmark_method="cap",
                             constraints=PortfolioConstraints(max_positions=2, max_weight=0.5, sector_max_weight=1.0))
        result = BacktestEngine(market, cfg).run()
        self.assertTrue(any("market cap" in w for w in result.warnings))
        self.assertTrue(all(v == result.config.initial_cash for v in result.benchmark_curve))


class ConvertibleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.price = {"K": {"2024-01-01": 100.0, "2024-01-02": 100.0, "2024-01-03": 130.0}}
        self.vol = {"K": {"2024-01-01": 10.0, "2024-01-02": 0.0, "2024-01-03": 10.0}}

    def test_execution_price_is_none_on_a_halted_day(self):
        self.assertIsNone(execution_price(self.price, self.vol, "K", "2024-01-02"))
        self.assertEqual(execution_price(self.price, self.vol, "K", "2024-01-03"), 130.0)

    def test_valuation_price_never_looks_forward(self):
        self.assertEqual(valuation_price(self.price, "K", "2024-01-02"), 100.0)

    def test_tradeable_price_default_does_not_use_future_prices(self):
        # The regression: with max_fwd=10 this returned 130.0 (the next session's
        # price) as if it were tradeable on 2024-01-02.
        self.assertEqual(tradeable_price(self.price, self.vol, "K", "2024-01-02"), 100.0)
        self.assertEqual(first_executable(self.price, self.vol, "K", "2024-01-02"), ("2024-01-03", 130.0))

    def _panel(self, td, halt_index):
        """Flat panel where bond K is halted on `halt_index` and jumps afterwards.

        `halt_index` is chosen to coincide with a rebalance date so the exit is
        genuinely unexecutable when the rebalance decision is made.
        """
        path = Path(td) / "cb.csv"
        vol_path = Path(td) / "cb_volume.csv"
        dates = [f"2024-01-{d:02d}" for d in range(1, 31)]
        rows, vols = [], []
        for i, date in enumerate(dates):
            for code in ("K", "A", "B"):
                close = 100.0
                if code == "K" and i > halt_index:
                    close = 130.0
                rows.append({"date": date, "bond_code": code, "bond_name": code, "close": close,
                             "conversion_premium": 10.0, "pure_bond_premium": 1.0,
                             "list_date": "2020-01-01", "rating": "AA", "stock_code": "000001.SZ"})
                volume = 10.0
                if code == "K" and i == halt_index:
                    volume = 0.0          # halted on a rebalance date
                vols.append({"date": date, "bond_code": code, "amount": 1e8, "volume": volume})
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        with vol_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["date", "bond_code", "amount", "volume"])
            writer.writeheader()
            writer.writerows(vols)
        return str(path), str(vol_path), dates

    def test_halted_bond_is_held_and_the_gain_lands_when_it_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            halt_index = 24
            path, vol_path, dates = self._panel(td, halt_index=halt_index)
            result = run_double_low(
                path, top_n=2, rebalance_days=2, start=dates[0], cost_bps=0.0,
                min_price=50, max_price=200, min_listed_days=0,
                volume_path=vol_path,
            )
            halt_date = dates[halt_index]
            at_halt = [rb for rb in result.rebalances if rb["date"] == halt_date]
            self.assertTrue(at_halt, "the halt session must be a rebalance date")
            self.assertGreaterEqual(at_halt[0].get("deferred_exits", 0), 1)
            # The halted bond cannot be sold, so it is still in the book.
            self.assertIn("K", {p["code"] for p in at_halt[0]["picks"]})
            # And no gain is realised on the halt session itself: the mark is the
            # last known close, not the post-resume price.
            self.assertAlmostEqual(at_halt[0]["period_return"], 0.0, places=12)

            later = [rb for rb in result.rebalances if rb["date"] > halt_date and "period_return" in rb]
            self.assertTrue(later)
            self.assertGreater(later[0]["period_return"], 0.05)
            # The whole move is counted exactly once: K's +30% spread over the
            # book that actually existed (top_n names plus the one that could not
            # be sold).  Realising it twice would give 0.30 here.
            book_size = len(at_halt[0]["picks"])
            self.assertAlmostEqual(later[0]["period_return"], 0.30 / book_size, places=9)

if __name__ == "__main__":
    unittest.main()


class BenchmarkWeightingTests(unittest.TestCase):
    """Regression: benchmark_method='cap' used to degenerate into equal weight.

    ``_cap_weights`` expects normalised weights; feeding it raw market caps made
    every name exceed the 2% single-name cap, so all of them were clipped to the
    same value and renormalised -> an equal-weight benchmark in disguise.
    """

    def _market(self):
        rows = []
        px = {"BIG": 100.0, "MID": 30.0, "SML": 8.0, "TINY": 3.0}
        caps = {"BIG": 800e9, "MID": 80e9, "SML": 20e9, "TINY": 5e9}
        for d in range(140):
            date = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
            for symbol in px:
                px[symbol] *= 1.0 + 0.001 * (1 if symbol == "BIG" else -1)
                rows.append(make_bar(date, symbol, close=px[symbol], market_cap=caps[symbol]))
        return MarketData.from_rows([b.__dict__ for b in rows])

    def test_cap_and_equal_benchmarks_differ(self):
        market = self._market()
        d0, d1 = market.dates[100], market.dates[101]
        results = {}
        for method in ("cap", "equal"):
            # A loose single-name cap, otherwise the water-filling legitimately
            # converges to equal weight even for a correct cap-weighted benchmark.
            cfg = BacktestConfig(benchmark_method=method, benchmark_weight_cap=0.5)
            engine = BacktestEngine(market, cfg)
            engine._benchmark_missing_cap = 0
            results[method] = engine._benchmark_return(d0, d1)
        self.assertNotAlmostEqual(results["cap"], results["equal"], places=10)

    def test_cap_benchmark_matches_manual_cap_weighting(self):
        market = self._market()
        d0, d1 = market.dates[100], market.dates[101]
        cfg = BacktestConfig(benchmark_method="cap", benchmark_weight_cap=1.0)  # no single-name cap
        engine = BacktestEngine(market, cfg)
        engine._benchmark_missing_cap = 0
        engine_value = engine._benchmark_return(d0, d1)

        prev, cur = market.snapshot(d0), market.snapshot(d1)
        rets, caps = [], []
        for symbol, a in prev.items():
            b = cur.get(symbol)
            if b and a.close > 0 and b.close > 0:
                rets.append(b.close / a.close - 1.0)
                caps.append(a.market_cap)
        expected = sum(r * c for r, c in zip(rets, caps)) / sum(caps)
        self.assertAlmostEqual(engine_value, expected, places=12)

    def test_implausible_market_cap_is_reported(self):
        rows = [make_bar("2024-01-02", "A", market_cap=2.6e13).__dict__,
                make_bar("2024-01-02", "B", market_cap=1e11).__dict__]
        report = validate_market(MarketData.from_rows(rows))
        self.assertEqual(report.implausible_market_cap, 1)
        self.assertTrue(any("trillion" in issue for issue in report.issues))


class WalkForwardConfigPropagationTests(unittest.TestCase):
    """The window configs used to be built from a hand-written field list.

    Every config field added later (benchmark_method / pit_policy /
    kelly_fraction / optimizer_candidate_multiplier / ...) was silently dropped,
    so "walk-forward with --pit-policy strict" actually ran in the default mode.
    """

    def test_window_config_keeps_every_base_field(self):
        import dataclasses
        from stockquant.walkforward import WalkForwardRunner

        market = flat_market(days=140, symbols=("A", "B", "C"))
        base = BacktestConfig(
            benchmark_method="equal",
            benchmark_weight_cap=0.10,
            pit_policy="off",
            kelly_fraction=0.25,
            optimizer_candidate_multiplier=2.0,
            target_volatility=0.11,
            rebalance="monthly",
            min_history=60,
            commission_bps=7.0,
            stamp_duty_bps=1.0,
            slippage_bps=9.0,
        )
        runner = WalkForwardRunner(market, base_config=base, train_days=40, test_days=20, step_days=20)
        cfg = runner._window_config("2024-02-01", "2024-03-01", {"momentum": 1.0})

        skip = {"start", "end", "factors", "alpha_weight"}
        for f in dataclasses.fields(base):
            if f.name in skip:
                continue
            self.assertEqual(getattr(cfg, f.name), getattr(base, f.name), f"field {f.name} was dropped")
        self.assertEqual(cfg.start, "2024-02-01")
        self.assertEqual(cfg.end, "2024-03-01")
        # weights supplied are applied to the factor set
        by_name = {spec.name: spec.weight for spec in cfg.factors}
        self.assertEqual(by_name["momentum"], 1.0)
        self.assertEqual(by_name["value"], 0.0)


class WalkForwardSelectionRuleTests(unittest.TestCase):
    """Pre-registered selection rules must be reproducible from the CLI."""

    def test_frozen_models_exist_and_match_default_weights(self):
        from stockquant.factors import DEFAULT_FACTORS
        from stockquant.walkforward import FROZEN_MODELS
        self.assertIn("default", FROZEN_MODELS)
        expected = {f.name: f.weight for f in DEFAULT_FACTORS}
        self.assertEqual(FROZEN_MODELS["default"], expected)

    def test_runner_exposes_penalty_and_shrinkage(self):
        from stockquant.walkforward import WalkForwardRunner
        market = flat_market(days=140, symbols=("A", "B", "C"))
        base = BacktestConfig(min_history=60, rebalance="monthly")
        runner = WalkForwardRunner(market, base_config=base, train_days=40, test_days=20,
                                   step_days=20, selection_penalty=1.5, shrink_to_equal=0.5)
        self.assertAlmostEqual(runner.selection_penalty, 1.5)
        self.assertAlmostEqual(runner.shrink_to_equal, 0.5)

    def test_cli_exposes_the_new_flags(self):
        from stockquant.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["walkforward", "--data", "x.csv", "--wf-model", "default",
                                  "--selection-penalty", "1.0", "--shrink-to-equal", "0.5"])
        self.assertEqual(args.wf_model, "default")
        self.assertAlmostEqual(args.selection_penalty, 1.0)
        self.assertAlmostEqual(args.shrink_to_equal, 0.5)


class WalkForwardSelectionWarningTests(unittest.TestCase):
    def test_select_model_warns_about_the_measured_cost(self):
        from stockquant.walkforward import WalkForwardRunner
        market = flat_market(days=200, symbols=("A", "B", "C", "D"))
        base = BacktestConfig(rebalance="monthly", min_history=60, target_volatility=None)
        runner = WalkForwardRunner(market, base_config=base, train_days=60, test_days=20, step_days=20, model="select")
        result = runner.run()
        warnings = result.validation.get("warnings") or []
        self.assertTrue(any("select" in w for w in warnings))

    def test_frozen_model_does_not_warn(self):
        from stockquant.walkforward import WalkForwardRunner
        market = flat_market(days=200, symbols=("A", "B", "C", "D"))
        base = BacktestConfig(rebalance="monthly", min_history=60, target_volatility=None)
        runner = WalkForwardRunner(market, base_config=base, train_days=60, test_days=20, step_days=20, model="default")
        result = runner.run()
        self.assertEqual(result.validation.get("warnings"), [])


class NeutralizationModeTests(unittest.TestCase):
    """Neutralisation grouping options (sector / cap_decile / random[:seed] / none).

    The vendor ``sector`` label is the *current* classification back-filled into
    history, and free historical industry series do not exist.  These modes let
    the comparison be made against PIT-clean (market-cap decile) and arbitrary
    (placebo) groupings instead of silently trusting the label.
    """

    def _engine(self, mode):
        return FactorEngine(min_history=20, sector_neutral=True, neutralize_by=mode)

    def test_cap_decile_is_monotone_in_market_cap(self):
        caps = {f"S{i}": float(1e9 * (i + 1)) for i in range(20)}
        engine = self._engine("cap_decile")
        groups = engine._neutral_groups({s: {} for s in caps}, {s: "TMT" for s in caps}, caps)
        self.assertEqual(len(set(groups.values())), 10)
        self.assertLess(groups["S0"], groups["S19"])          # smallest cap -> lowest decile
        self.assertEqual(groups["S0"], "d0")

    def test_random_mode_is_deterministic_and_respects_the_seed(self):
        symbols = {f"S{i}": {} for i in range(200)}
        sectors = {s: "TMT" for s in symbols}
        first = self._engine("random:13:7")._neutral_groups(symbols, sectors, {})
        again = self._engine("random:13:7")._neutral_groups(symbols, sectors, {})
        other = self._engine("random:13:8")._neutral_groups(symbols, sectors, {})
        self.assertEqual(first, again)                        # same seed -> same groups
        self.assertNotEqual(first, other)                     # different seed -> different groups
        self.assertLessEqual(len(set(first.values())), 13)
        self.assertGreaterEqual(len(set(first.values())), 10)  # 200 names over 13 buckets

    def test_none_mode_puts_everything_in_one_group(self):
        symbols = {"A": {}, "B": {}}
        groups = self._engine("none")._neutral_groups(symbols, {"A": "X", "B": "Y"}, {})
        self.assertEqual(set(groups.values()), {"ALL"})

    def test_unknown_mode_is_rejected_loudly(self):
        with self.assertRaises(ValueError):
            self._engine("sector_v2")._neutral_groups({"A": {}}, {"A": "X"}, {})


class ConvertibleRankIndexTests(unittest.TestCase):
    """The O(log n) date index must not change ``_rank``'s output at all."""

    def _panel(self, td):
        path = Path(td) / "cb.csv"
        rows = []
        dates = [f"2024-{m:02d}-{d:02d}" for m in (1, 2, 3, 4) for d in range(1, 11)]
        for i, date in enumerate(dates):
            for code, base in (("A", 100.0), ("B", 120.0), ("C", 95.0)):
                close = base + 0.3 * i
                rows.append({"date": date, "bond_code": code, "bond_name": code, "close": close,
                             "conversion_premium": 10.0 + i, "pure_bond_premium": 1.0,
                             "list_date": "2020-01-01", "rating": "AA", "stock_code": "000001.SZ"})
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return str(path)

    def test_rank_with_and_without_index_is_identical(self):
        from stockquant.convertible.double_low import _DateIndex, _load_panel, _rank

        with tempfile.TemporaryDirectory() as td:
            path = self._panel(td)
            price, prem, name, dates, rating, stock, amount, pbond, volmap, listed = _load_panel(path)
            asof = dates[-1]
            kwargs = dict(min_price=50.0, max_price=300.0, max_premium=200.0, min_premium=-5.0,
                          pbond=pbond, min_pure_bond_premium=0.0, listed=listed, min_listed_days=0)
            without = _rank(price, prem, asof, 3, **kwargs)
            index = {"price": _DateIndex(price), "prem": _DateIndex(prem), "pbond": _DateIndex(pbond)}
            with_index = _rank(price, prem, asof, 3, index=index, **kwargs)
            self.assertEqual(without, with_index)
            self.assertTrue(without)

    def test_index_helpers_match_the_linear_scans(self):
        from stockquant.convertible.double_low import _DateIndex, _last, _last_date

        series = {"X": {"2024-01-02": 1.0, "2024-01-05": 2.0, "2024-01-09": 3.0}}
        index = _DateIndex(series)
        for asof in ("2024-01-01", "2024-01-02", "2024-01-06", "2024-01-09", "2024-02-01"):
            self.assertEqual(index.last("X", asof), _last(series["X"], asof))
            self.assertEqual(index.last_date("X", asof), _last_date(series["X"], asof))
        self.assertEqual(index.count_upto("X", "2024-01-05"), 2)
        self.assertIsNone(index.last("MISSING", "2024-01-05"))
