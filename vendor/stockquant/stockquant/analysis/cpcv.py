"""Combinatorial Purged Cross-Validation (CPCV).

López de Prado, *Advances in Financial Machine Learning* (2018), ch. 7 & 12.

A single train/test split gives one noisy number. CPCV partitions the timeline
into ``n_groups`` contiguous blocks, uses every combination of ``n_test_groups``
blocks as the test set, and purges/embargoes the training set so no label
overlaps the test period. The result is a *distribution* of out-of-sample
performance across C(n_groups, n_test_groups) paths.

This is what turns "IC went from 0.005 to 0.056" into either a finding or a
single-split artefact.
"""

from __future__ import annotations

import math
import statistics
from itertools import combinations
from typing import Callable, Dict, List, Optional, Sequence, Tuple


def _groups(n_items: int, n_groups: int) -> List[List[int]]:
    bounds = [round(i * n_items / n_groups) for i in range(n_groups + 1)]
    out = []
    for i in range(n_groups):
        start, end = bounds[i], bounds[i + 1]
        if end > start:
            out.append(list(range(start, end)))
    return out


def combinatorial_splits(
    n_dates: int,
    n_groups: int = 6,
    n_test_groups: int = 2,
    purge_bars: int = 20,
    embargo_bars: int = 5,
) -> List[Tuple[List[int], List[int]]]:
    """Return ``(train_date_indices, test_date_indices)`` for every combination."""
    if n_groups < 2 or n_test_groups < 1 or n_test_groups >= n_groups:
        raise ValueError("require 2 <= n_groups and 1 <= n_test_groups < n_groups")
    blocks = _groups(n_dates, n_groups)
    out: List[Tuple[List[int], List[int]]] = []
    for combo in combinations(range(len(blocks)), n_test_groups):
        test_idx = sorted(i for b in combo for i in blocks[b])
        test_set = set(test_idx)
        train_idx = [i for i in range(n_dates) if i not in test_set]

        # Purge: a training label at index i spans [i, i + purge_bars]; drop it
        # if that window reaches into any test block.  Embargo: also drop the
        # bars right after each test block.
        keep: List[int] = []
        for i in train_idx:
            conflicted = False
            for b in combo:
                block = blocks[b]
                lo, hi = block[0], block[-1]
                if i <= hi and i + purge_bars >= lo:   # label overlaps the test block
                    conflicted = True
                    break
                if hi < i <= hi + embargo_bars:        # embargo after the block
                    conflicted = True
                    break
            if not conflicted:
                keep.append(i)
        out.append((keep, test_idx))
    return out


def _rank(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg
        i = j + 1
    return ranks


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 5:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 1e-16 or vy <= 1e-16:
        return None
    return cov / math.sqrt(vx * vy)


def _oos_ic_by_date(dates: Sequence[str], preds: Sequence[float], actuals: Sequence[float]) -> List[float]:
    buckets: Dict[str, List[Tuple[float, float]]] = {}
    for d, p, a in zip(dates, preds, actuals):
        buckets.setdefault(str(d), []).append((float(p), float(a)))
    out = []
    for values in buckets.values():
        if len(values) < 5:
            continue
        ic = _spearman([p for p, _ in values], [a for _, a in values])
        if ic is not None:
            out.append(ic)
    return out


def cpcv_evaluate(
    dates: Sequence[str],
    X: Sequence[Sequence[float]],
    y: Sequence[float],
    fit_predict: Callable[[Sequence[Sequence[float]], Sequence[float], Sequence[Sequence[float]]], Sequence[float]],
    n_groups: int = 6,
    n_test_groups: int = 2,
    purge_bars: int = 20,
    embargo_bars: int = 5,
) -> Dict[str, object]:
    """Run CPCV and return the distribution of out-of-sample IC.

    ``fit_predict(X_train, y_train, X_test) -> predictions``.
    """
    unique_dates = sorted(set(str(d) for d in dates))
    date_index = {d: i for i, d in enumerate(unique_dates)}
    row_date_idx = [date_index[str(d)] for d in dates]

    splits = combinatorial_splits(len(unique_dates), n_groups, n_test_groups, purge_bars, embargo_bars)
    path_ics: List[float] = []
    per_path: List[Dict[str, object]] = []
    for path_no, (train_dates, test_dates) in enumerate(splits):
        train_set, test_set = set(train_dates), set(test_dates)
        train_rows = [i for i, di in enumerate(row_date_idx) if di in train_set]
        test_rows = [i for i, di in enumerate(row_date_idx) if di in test_set]
        if len(train_rows) < 50 or len(test_rows) < 50:
            continue
        x_train = [X[i] for i in train_rows]
        y_train = [y[i] for i in train_rows]
        x_test = [X[i] for i in test_rows]
        preds = list(fit_predict(x_train, y_train, x_test))
        ic_list = _oos_ic_by_date([dates[i] for i in test_rows], preds, [y[i] for i in test_rows])
        if not ic_list:
            continue
        mean_ic = statistics.mean(ic_list)
        path_ics.append(mean_ic)
        per_path.append({
            "path": path_no,
            "test_groups": len(test_dates),
            "train_rows": len(train_rows),
            "test_rows": len(test_rows),
            "mean_ic": mean_ic,
        })

    if not path_ics:
        return {"available": False, "error": "no valid CPCV paths"}

    pos = sum(1 for v in path_ics if v > 0)
    return {
        "available": True,
        "paths": len(path_ics),
        "combinations": len(splits),
        "n_groups": n_groups,
        "n_test_groups": n_test_groups,
        "purge_bars": purge_bars,
        "embargo_bars": embargo_bars,
        "mean_ic": statistics.mean(path_ics),
        "std_ic": statistics.pstdev(path_ics) if len(path_ics) > 1 else 0.0,
        "min_ic": min(path_ics),
        "max_ic": max(path_ics),
        "median_ic": statistics.median(path_ics),
        "positive_path_rate": pos / len(path_ics),
        "t_stat": (statistics.mean(path_ics) / statistics.pstdev(path_ics) * math.sqrt(len(path_ics)))
                    if len(path_ics) > 1 and statistics.pstdev(path_ics) > 1e-16 else 0.0,
        "path_ics": path_ics,
        "paths_detail": per_path,
    }
