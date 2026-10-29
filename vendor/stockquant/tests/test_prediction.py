import math
import unittest

from stockquant.analysis.prediction import (
    blend_scores,
    evaluate_prediction_challengers,
    percentile_rank,
    segment_summary,
    weighted_rank_scores,
    weighted_z_scores,
)
from stockquant.domain import Bar, MarketData
from stockquant.factors import FactorSpec


def make_market(days=220, symbols=12):
    rows = []
    for d in range(days):
        date = f"2024-{1 + d // 28:02d}-{1 + d % 28:02d}"
        for i in range(symbols):
            drift = 0.01 + 0.001 * i
            px = 10.0 + i * 0.3 + d * drift + 0.03 * math.sin(d / 4.0 + i)
            rows.append(
                Bar(
                    date=date,
                    symbol=f"S{i:02d}",
                    open=px * 0.999,
                    high=px * 1.01,
                    low=px * 0.99,
                    close=px,
                    volume=100_000 + i * 1_000 + d * 10,
                    sector="Tech" if i % 2 else "Bank",
                    market_cap=1e9 * (i + 1),
                )
            )
    return MarketData.from_rows([row.__dict__ for row in rows])


def trend(market, symbol, bars):
    return bars[-1].close / bars[-20].close - 1.0


def low_vol(market, symbol, bars):
    returns = [bars[i].close / bars[i - 1].close - 1.0 for i in range(1, len(bars))]
    return -sum(r * r for r in returns[-20:])


def liquidity(market, symbol, bars):
    return math.log1p(sum(bar.close * bar.volume for bar in bars[-20:]) / 20.0)


class RankScoreTests(unittest.TestCase):
    def test_percentile_rank_is_tie_aware_and_centered(self):
        ranked = percentile_rank({"a": 1.0, "b": 1.0, "c": 3.0})
        self.assertAlmostEqual(ranked["a"], -0.5)
        self.assertAlmostEqual(ranked["b"], -0.5)
        self.assertAlmostEqual(ranked["c"], 1.0)

    def test_fixed_denominator_and_coverage_gate(self):
        factors = (
            FactorSpec("a", 0.5, lambda m, s, b: None, "a"),
            FactorSpec("b", 0.5, lambda m, s, b: None, "b"),
        )
        components = {"a": {"s1": 1.0, "s2": 2.0}, "b": {"s1": 1.0}}
        z = weighted_z_scores(components, factors)
        rank = weighted_rank_scores(components, factors)
        self.assertAlmostEqual(z["s1"], 1.0)
        self.assertAlmostEqual(z["s2"], 1.0)
        self.assertEqual(set(rank), {"s1", "s2"})
        gated = weighted_rank_scores(components, factors, min_coverage=0.75)
        self.assertEqual(set(gated), {"s1"})

    def test_blend_keeps_missing_challenger_neutral(self):
        out = blend_scores({"a": 1.0, "b": 0.5}, {"a": -1.0}, challenger_weight=0.2)
        self.assertAlmostEqual(out["a"], 0.6)
        self.assertAlmostEqual(out["b"], 0.4)


class PredictionEvaluationTests(unittest.TestCase):
    def test_challenger_report_has_three_series(self):
        market = make_market()
        primary = (
            FactorSpec("trend", 0.5, trend, "trend"),
            FactorSpec("low_vol", 0.3, low_vol, "low vol"),
            FactorSpec("liquidity", 0.2, liquidity, "liquidity"),
        )
        challenger = (FactorSpec("challenger", 1.0, trend, "challenger"),)
        result = evaluate_prediction_challengers(
            market,
            start=market.dates[45],
            end=market.dates[-1],
            primary_factors=primary,
            challenger_factors=challenger,
            min_history=40,
            horizon=20,
        )
        self.assertIn("weighted_z", result["summary"])
        self.assertIn("weighted_rank", result["summary"])
        self.assertIn("z_plus_challenger", result["summary"])
        self.assertIn("rank_plus_challenger", result["summary"])
        self.assertGreater(result["summary"]["weighted_z"]["observations"], 0)
        segmented = segment_summary(result, market.dates[45], market.dates[-1])
        self.assertIn("weighted_rank", segmented)
