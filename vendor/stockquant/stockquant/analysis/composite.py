"""Cross-sectional IC of a composite (equal-weight) factor, with the same
Newey-West / multiple-testing discipline as the single-factor IC analysis.

The composite combiner needs no training, so its IC series over the rebalance
dates is a direct, leakage-free out-of-sample measure.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional

from stockquant.analysis.ic import _forward_return, _rebalance_due, newey_west_t_stat, spearman
from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec


def composite_ic_series(
    market: MarketData,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    factors: Optional[List[FactorSpec]] = None,
    impute_missing: bool = True,
    orthogonalize: bool = False,
) -> Dict[str, object]:
    fset = list(factors) if factors else list(DEFAULT_FACTORS)
    engine = FactorEngine(
        fset,
        min_history=min_history,
        sector_neutral=sector_neutral,
        impute_missing=impute_missing,
        orthogonalize=orthogonalize,
    )
    dates = list(market.dates)
    ic_values: List[float] = []
    by_date: List[Dict[str, object]] = []
    for i in range(0, max(0, len(dates) - horizon)):
        cur = dates[i]
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or not _rebalance_due(cur, nxt, rebalance):
            continue
        future = dates[i + horizon]
        components, diag = engine.components(market, cur)
        if diag.scored_count < 5:
            continue
        symbols = set()
        for mapping in components.values():
            symbols.update(mapping)
        xs: List[float] = []
        ys: List[float] = []
        for symbol in symbols:
            fr = _forward_return(market, symbol, cur, future)
            if fr is None:
                continue
            # equal-weight composite = mean of available factor z-scores
            zs = [components[name][symbol] for name in [f.name for f in fset] if symbol in components.get(name, {})]
            if not zs:
                continue
            xs.append(sum(zs) / len(zs))
            ys.append(fr)
        if len(xs) < 5:
            continue
        ic = spearman(xs, ys)
        if ic is not None:
            ic_values.append(ic)
            by_date.append({"date": cur, "ic": ic, "count": len(xs)})

    if not ic_values:
        return {"observations": 0, "mean_ic": 0.0, "t_stat_nw": 0.0, "p_value": 1.0, "positive_rate": 0.0}

    mean = statistics.mean(ic_values)
    t_nw = newey_west_t_stat(ic_values)
    p = 2.0 * (1.0 - statistics.NormalDist().cdf(abs(t_nw)))
    periods = 52 if rebalance == "weekly" else 252 if rebalance == "daily" else 12
    ic_ir = mean / statistics.pstdev(ic_values) * math.sqrt(periods) if statistics.pstdev(ic_values) > 1e-16 else 0.0
    return {
        "observations": len(ic_values),
        "mean_ic": mean,
        "ic_std": statistics.pstdev(ic_values),
        "ic_ir": ic_ir,
        "t_stat_nw": t_nw,
        "p_value": p,
        "positive_rate": sum(1 for v in ic_values if v > 0) / len(ic_values),
        "min_ic": min(ic_values),
        "max_ic": max(ic_values),
        "by_date": by_date,
    }
