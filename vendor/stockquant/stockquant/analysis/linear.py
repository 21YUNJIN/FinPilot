from __future__ import annotations

import math
import statistics
from datetime import date as Date
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine
from stockquant.analysis.neutralize import MODE_INDUSTRY_BETA, neutralized_labels


def _rebalance_due(cur: str, nxt: str, mode: str) -> bool:
    if mode == "daily":
        return True
    if mode == "weekly":
        return Date.fromisoformat(cur).isocalendar()[:2] != Date.fromisoformat(nxt).isocalendar()[:2]
    if mode == "quarterly":
        return (int(cur[5:7]) - 1) // 3 != (int(nxt[5:7]) - 1) // 3 or cur[:4] != nxt[:4]
    if mode == "semiannual":
        return (int(cur[5:7]) - 1) // 6 != (int(nxt[5:7]) - 1) // 6 or cur[:4] != nxt[:4]
    if mode == "annual":
        return cur[:4] != nxt[:4]
    return cur[:7] != nxt[:7]


def _forward_return(market: MarketData, symbol: str, start: str, end: str) -> Optional[float]:
    a = market.bar(symbol, start)
    b = market.bar(symbol, end)
    if a is None or b is None or a.close <= 0 or b.close <= 0 or a.suspended or b.suspended:
        return None
    return b.close / a.close - 1.0


def collect_factor_panel(
    market: MarketData,
    start: str,
    end: str,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    label_mode: str = MODE_INDUSTRY_BETA,
) -> Tuple[List[str], List[List[float]], List[float], int]:
    factor_names = [f.name for f in DEFAULT_FACTORS]
    engine = FactorEngine(DEFAULT_FACTORS, min_history=min_history, sector_neutral=sector_neutral)
    dates = [d for d in market.dates if start <= d <= end]
    X: List[List[float]] = []
    y: List[float] = []
    periods = 0
    for i in range(0, max(0, len(dates) - horizon)):
        cur = dates[i]
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or not _rebalance_due(cur, nxt, rebalance):
            continue
        future = dates[i + horizon]
        components, diag = engine.components(market, cur)
        if diag.scored_count < 5:
            continue
        targets: Dict[str, float] = {}
        component_symbols = set()
        for mapping in components.values():
            component_symbols.update(mapping)
        for symbol in component_symbols:
            fr = _forward_return(market, symbol, cur, future)
            if fr is not None:
                targets[symbol] = fr
        if len(targets) < 5:
            continue
        targets = neutralized_labels(market, cur, future, list(targets), targets, mode=label_mode)
        for symbol, target in targets.items():
            row = []
            valid = True
            for name in factor_names:
                value = components.get(name, {}).get(symbol)
                if value is None:
                    valid = False
                    break
                row.append(float(value))
            if valid:
                X.append(row)
                y.append(target)
        periods += 1
    return factor_names, X, y, periods


def ridge_factor_weights(
    market: MarketData,
    start: str,
    end: str,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    ridge_lambda: float = 50.0,
    coefficient_cap: float = 0.5,
) -> Dict[str, float]:
    try:
        import numpy as np
    except Exception:
        return {}
    names, X, y, periods = collect_factor_panel(
        market,
        start,
        end,
        min_history=min_history,
        horizon=horizon,
        rebalance=rebalance,
        sector_neutral=sector_neutral,
    )
    if len(X) < max(50, len(names) * 10):
        return {}
    x = np.asarray(X, dtype=float)
    target = np.asarray(y, dtype=float)
    x = np.column_stack([np.ones(len(x)), x])
    penalty = np.eye(x.shape[1]) * ridge_lambda
    penalty[0, 0] = 0.0
    try:
        beta = np.linalg.solve(x.T @ x + penalty, x.T @ target)
    except np.linalg.LinAlgError:
        beta = np.linalg.pinv(x.T @ x + penalty) @ x.T @ target
    raw = {name: float(beta[i + 1]) for i, name in enumerate(names)}
    clipped = {k: max(-coefficient_cap, min(coefficient_cap, v)) for k, v in raw.items()}
    total = sum(abs(v) for v in clipped.values())
    if total <= 1e-12:
        return {}
    return {k: v / total for k, v in clipped.items()}
