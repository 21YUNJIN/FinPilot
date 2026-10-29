from __future__ import annotations

import math
import random
import statistics
from itertools import combinations
from typing import Dict, List, Sequence


def _sharpe(returns: Sequence[float], annualization: int = 252) -> float:
    if len(returns) < 2:
        return 0.0
    volatility = statistics.pstdev(returns)
    if volatility <= 1e-16:
        return 0.0
    return statistics.mean(returns) / volatility * math.sqrt(annualization)


def _average_ranks(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = rank
        i = j + 1
    return ranks


def _block_bounds(observations: int, splits: int) -> List[tuple[int, int]]:
    bounds = []
    for i in range(splits):
        start = round(i * observations / splits)
        end = round((i + 1) * observations / splits)
        if end <= start:
            raise ValueError("each CSCV block must contain at least one observation")
        bounds.append((start, end))
    return bounds


def probability_of_backtest_overfitting(
    return_matrix: Sequence[Sequence[float]],
    n_splits: int = 10,
    annualization: int = 252,
    max_combinations: int = 20_000,
    seed: int = 0,
    strategy_names: Sequence[str] | None = None,
) -> Dict[str, object]:
    """Estimate PBO with combinatorially symmetric cross-validation.

    ``return_matrix`` is a strategy-by-period matrix. The function ranks the
    in-sample winner of each split in the complementary out-of-sample block.
    PBO is the frequency with which that winner falls below the median
    out-of-sample strategy.
    """
    matrix = [[float(value) for value in row] for row in return_matrix]
    strategy_count = len(matrix)
    if strategy_count < 2:
        raise ValueError("PBO requires at least two strategies")
    if strategy_names is not None and len(strategy_names) != strategy_count:
        raise ValueError("strategy_names must match the return matrix")
    if not matrix or not matrix[0]:
        raise ValueError("PBO requires a non-empty return matrix")
    lengths = {len(row) for row in matrix}
    if len(lengths) != 1:
        raise ValueError("all strategy return series must have equal length")
    observations = len(matrix[0])
    if observations < 8:
        raise ValueError("PBO requires at least eight observations")

    splits = min(int(n_splits), observations)
    if splits % 2:
        splits -= 1
    if splits < 4:
        raise ValueError("PBO requires at least four split blocks")
    while splits >= 4 and math.comb(splits, splits // 2) > max_combinations:
        splits -= 2
    if splits < 4:
        raise ValueError("max_combinations is too small for PBO")

    bounds = _block_bounds(observations, splits)
    block_indices = [list(range(start, end)) for start, end in bounds]
    combinations_total = math.comb(splits, splits // 2)
    rng = random.Random(seed)
    if combinations_total <= max_combinations:
        selected_combinations = list(combinations(range(splits), splits // 2))
    else:  # pragma: no cover - defensive fallback for very large split counts
        selected_combinations = rng.sample(list(combinations(range(splits), splits // 2)), max_combinations)

    logits: List[float] = []
    oos_ranks: List[float] = []
    selection_counts: Dict[str, int] = {}
    for in_blocks in selected_combinations:
        in_set = set(in_blocks)
        in_indices = [idx for block, indices in enumerate(block_indices) if block in in_set for idx in indices]
        out_indices = [idx for block, indices in enumerate(block_indices) if block not in in_set for idx in indices]
        in_scores = [_sharpe([row[idx] for idx in in_indices], annualization) for row in matrix]
        out_scores = [_sharpe([row[idx] for idx in out_indices], annualization) for row in matrix]
        winner = max(range(strategy_count), key=lambda idx: (in_scores[idx], -idx))
        rank = _average_ranks(out_scores)[winner]
        logit = math.log(rank / (strategy_count + 1.0 - rank))
        logits.append(logit)
        oos_ranks.append(rank)
        winner_name = str(strategy_names[winner]) if strategy_names is not None else str(winner)
        selection_counts[winner_name] = selection_counts.get(winner_name, 0) + 1

    total = len(selected_combinations)
    return {
        "pbo": sum(1 for value in logits if value <= 0.0) / total,
        "splits": float(splits),
        "combinations": float(total),
        "strategies": float(strategy_count),
        "observations": float(observations),
        "median_oos_rank": float(statistics.median(oos_ranks)),
        "median_logit": float(statistics.median(logits)),
        "selection_frequency": {key: value / total for key, value in sorted(selection_counts.items())},
    }
