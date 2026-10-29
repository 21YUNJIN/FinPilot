"""Leakage-safe cross-sectional prediction challengers.

The production factor engine currently combines winsorized z-scores.  This
module adds two low-parameter challengers that are useful when the raw factor
distributions are unstable:

* rank averaging: rank each factor inside the date, then average with the
  book's fixed weights;
* challenger blending: blend a validated research sleeve (for example GTJA042)
  with the primary rank score at a pre-declared weight.

The module is deliberately research-only.  It does not mutate
``DEFAULT_FACTORS`` and it never writes to the live execution path.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Mapping, Optional, Sequence

from stockquant.analysis.ic import _forward_return, _rebalance_due, newey_west_t_stat, spearman
from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec


def percentile_rank(values: Mapping[str, float], *, center: bool = True) -> Dict[str, float]:
    """Return tie-aware cross-sectional percentile ranks.

    With ``center=True`` ranks are in [-1, 1], which keeps missing factors at a
    neutral zero when a fixed-denominator score is formed.  A single-name
    cross-section is assigned zero.
    """
    items = sorted(
        ((symbol, float(value)) for symbol, value in values.items() if math.isfinite(float(value))),
        key=lambda item: item[1],
    )
    n = len(items)
    if n == 0:
        return {}
    out: Dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2.0
        value = avg_rank / (n - 1) if n > 1 else 0.5
        if center:
            value = 2.0 * value - 1.0
        for k in range(i, j + 1):
            out[items[k][0]] = value
        i = j + 1
    return out


def _weighted_score(
    component_scores: Mapping[str, Mapping[str, float]],
    factors: Sequence[FactorSpec],
    *,
    transform: str,
    min_coverage: float = 0.0,
) -> Dict[str, float]:
    if transform not in {"zscore", "rank"}:
        raise ValueError(f"unknown score transform: {transform!r}")
    if not 0.0 <= min_coverage <= 1.0:
        raise ValueError("min_coverage must be in [0, 1]")

    mapping: Dict[str, Dict[str, float]] = {}
    for factor in factors:
        raw = component_scores.get(factor.name, {})
        mapping[factor.name] = dict(raw) if transform == "zscore" else percentile_rank(raw)

    symbols = set()
    for values in mapping.values():
        symbols.update(values)
    total_weight = sum(abs(float(factor.weight)) for factor in factors)
    if total_weight <= 0.0:
        return {}

    scores: Dict[str, float] = {}
    for symbol in symbols:
        available_weight = 0.0
        weighted_sum = 0.0
        for factor in factors:
            value = mapping.get(factor.name, {}).get(symbol)
            if value is None:
                continue
            available_weight += abs(float(factor.weight))
            weighted_sum += float(factor.weight) * float(value)
        coverage = available_weight / total_weight
        if coverage + 1e-12 < min_coverage:
            continue
        # Fixed denominator: a missing factor contributes neutral zero rather
        # than silently increasing the influence of the remaining factors.
        scores[symbol] = weighted_sum / total_weight
    return scores


def weighted_z_scores(
    component_scores: Mapping[str, Mapping[str, float]],
    factors: Sequence[FactorSpec],
    *,
    min_coverage: float = 0.0,
) -> Dict[str, float]:
    """Fixed-denominator weighted z-score with an optional coverage gate."""
    return _weighted_score(component_scores, factors, transform="zscore", min_coverage=min_coverage)


def weighted_rank_scores(
    component_scores: Mapping[str, Mapping[str, float]],
    factors: Sequence[FactorSpec],
    *,
    min_coverage: float = 0.0,
) -> Dict[str, float]:
    """Fixed-denominator weighted rank score.

    This is a zero-training challenger to z-score averaging: it is invariant to
    monotone outliers and uses the same economic weights and neutralisation as
    the primary model.
    """
    return _weighted_score(component_scores, factors, transform="rank", min_coverage=min_coverage)


def blend_scores(
    primary: Mapping[str, float],
    challenger: Mapping[str, float],
    *,
    challenger_weight: float,
    min_challenger_coverage: float = 0.0,
) -> Dict[str, float]:
    """Blend aligned scores while keeping unscored names neutral.

    ``min_challenger_coverage`` is useful for diagnostics but defaults to zero:
    the challenger is an optional sleeve, so a missing candidate value dilutes
    rather than renormalises the name's score.
    """
    if not 0.0 <= challenger_weight <= 1.0:
        raise ValueError("challenger_weight must be in [0, 1]")
    if not 0.0 <= min_challenger_coverage <= 1.0:
        raise ValueError("min_challenger_coverage must be in [0, 1]")

    out: Dict[str, float] = {}
    for symbol, primary_value in primary.items():
        challenger_value = challenger.get(symbol)
        coverage = 1.0 if challenger_value is not None else 0.0
        if coverage + 1e-12 < min_challenger_coverage:
            continue
        value = (1.0 - challenger_weight) * float(primary_value)
        if challenger_value is not None:
            value += challenger_weight * float(challenger_value)
        out[symbol] = value
    return out


def _summarize_ics(values: Sequence[float]) -> Dict[str, float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return {
            "observations": 0.0,
            "mean_ic": 0.0,
            "ic_std": 0.0,
            "positive_rate": 0.0,
            "t_stat_nw": 0.0,
        }
    return {
        "observations": float(len(vals)),
        "mean_ic": statistics.mean(vals),
        "ic_std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        "positive_rate": sum(1 for v in vals if v > 0) / len(vals),
        "t_stat_nw": newey_west_t_stat(vals),
    }


def evaluate_prediction_challengers(
    market: MarketData,
    *,
    start: str,
    end: str,
    primary_factors: Optional[Sequence[FactorSpec]] = None,
    challenger_factors: Sequence[FactorSpec] = (),
    challenger_weight: float = 0.20,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    neutralize_by: str = "cap_decile",
    sector_neutral: bool = True,
    min_primary_coverage: float = 0.0,
) -> Dict[str, object]:
    """Compare z-score, rank and rank+challenger prediction scores.

    Every signal on date ``t`` is scored only from bars available through ``t``;
    the label is the return from ``t`` to the next matching horizon.  The
    function reports cross-sectional IC, not portfolio PnL, so transaction costs
    and position limits are intentionally out of scope.
    """
    primary = tuple(primary_factors if primary_factors is not None else DEFAULT_FACTORS)
    challengers = tuple(challenger_factors)
    if not primary:
        raise ValueError("primary_factors must not be empty")
    if challengers and not 0.0 <= challenger_weight <= 1.0:
        raise ValueError("challenger_weight must be in [0, 1]")

    primary_engine = FactorEngine(
        primary,
        min_history=min_history,
        sector_neutral=sector_neutral,
        neutralize_by=neutralize_by,
    )
    challenger_engine = (
        FactorEngine(
            challengers,
            min_history=min_history,
            sector_neutral=sector_neutral,
            neutralize_by=neutralize_by,
        )
        if challengers
        else None
    )

    dates = [d for d in market.dates if start <= d <= end]
    by_date: List[Dict[str, object]] = []
    series: Dict[str, List[float]] = {"weighted_z": [], "weighted_rank": []}
    if challengers:
        series["z_plus_challenger"] = []
        series["rank_plus_challenger"] = []

    for i in range(0, max(0, len(dates) - horizon)):
        cur = dates[i]
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or not _rebalance_due(cur, nxt, rebalance):
            continue
        future = dates[i + horizon]
        primary_components, primary_diag = primary_engine.components(market, cur)
        if primary_diag.scored_count < 5:
            continue

        z_scores = weighted_z_scores(
            primary_components, primary, min_coverage=min_primary_coverage
        )
        rank_scores = weighted_rank_scores(
            primary_components, primary, min_coverage=min_primary_coverage
        )
        score_sets: Dict[str, Dict[str, float]] = {
            "weighted_z": z_scores,
            "weighted_rank": rank_scores,
        }

        if challenger_engine is not None:
            challenger_components, _ = challenger_engine.components(market, cur)
            challenger_scores = weighted_rank_scores(challenger_components, challengers)
            score_sets["z_plus_challenger"] = blend_scores(
                z_scores,
                challenger_scores,
                challenger_weight=challenger_weight,
            )
            score_sets["rank_plus_challenger"] = blend_scores(
                rank_scores,
                challenger_scores,
                challenger_weight=challenger_weight,
            )

        row: Dict[str, object] = {"date": cur, "future_date": future, "ic": {}}
        common = set().union(*(set(values) for values in score_sets.values()))
        forward_returns: Dict[str, float] = {}
        for symbol in common:
            result = _forward_return(market, symbol, cur, future)
            if result is not None:
                forward_returns[symbol] = result
        row["count"] = len(forward_returns)
        if len(forward_returns) < 5:
            continue
        for name, scores in score_sets.items():
            pairs = [
                (scores[symbol], forward_returns[symbol])
                for symbol in forward_returns
                if symbol in scores
            ]
            if len(pairs) < 5:
                continue
            ic = spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])
            if ic is None:
                continue
            row["ic"][name] = ic  # type: ignore[index]
            series[name].append(ic)
        if row["ic"]:
            by_date.append(row)

    return {
        "config": {
            "start": start,
            "end": end,
            "horizon": horizon,
            "rebalance": rebalance,
            "neutralize_by": neutralize_by,
            "sector_neutral": sector_neutral,
            "challenger_weight": challenger_weight,
            "min_primary_coverage": min_primary_coverage,
            "primary_factors": [factor.name for factor in primary],
            "challenger_factors": [factor.name for factor in challengers],
        },
        "summary": {name: _summarize_ics(values) for name, values in series.items()},
        "by_date": by_date,
    }


def segment_summary(result: Mapping[str, object], start: str, end: str) -> Dict[str, Dict[str, float]]:
    """Summarise an existing challenger result over a date segment."""
    rows = [
        row for row in result.get("by_date", [])
        if isinstance(row, Mapping) and start <= str(row.get("date", "")) <= end
    ]
    names = sorted({name for row in rows for name in dict(row.get("ic", {})).keys()})
    out: Dict[str, Dict[str, float]] = {}
    for name in names:
        values = [
            float(dict(row.get("ic", {}))[name])
            for row in rows
            if name in dict(row.get("ic", {}))
        ]
        out[name] = _summarize_ics(values)
    return out
