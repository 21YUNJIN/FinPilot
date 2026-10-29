"""White's Reality Check and Hansen's SPA-style multiple-comparison tests.

When many strategy variants are tried, the best in-sample result is biased
upwards. These tests compare the best candidate against a benchmark while
accounting for that selection, using a stationary bootstrap (Politis & Romano,
1994) to build the null distribution. Pure standard library, deterministic via
an explicit seed.

* :func:`white_reality_check` - White (2000), Econometrica.
* :func:`spa_test` - Hansen (2005) Studentised version (SPA).

Both report the observed statistic and a bootstrap p-value; a p-value above
0.05 means the best candidate's excess is not distinguishable from the best of
skill-less trials.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import List, Sequence


def stationary_bootstrap(
    series: Sequence[float],
    n_bootstrap: int,
    block_mean: float,
    seed: int = 0,
) -> List[List[float]]:
    """Politis-Romano stationary bootstrap replicates (geometric block length)."""
    t = len(series)
    if t < 2 or n_bootstrap < 1:
        return []
    p = 1.0 / max(block_mean, 1.0)
    rng = random.Random(seed)
    out: List[List[float]] = []
    for _ in range(n_bootstrap):
        idx = [rng.randrange(t)]
        while len(idx) < t:
            if rng.random() < p:
                idx.append(rng.randrange(t))
            else:
                idx.append((idx[-1] + 1) % t)
        out.append([series[i] for i in idx])
    return out


def _excess(benchmark: Sequence[float], candidates: Sequence[Sequence[float]]) -> List[List[float]]:
    if not candidates:
        return []
    t = min(len(benchmark), min(len(c) for c in candidates))
    bench = [float(x) for x in benchmark[:t]]
    excess = []
    for cand in candidates:
        c = [float(x) for x in cand[:t]]
        excess.append([c[i] - bench[i] for i in range(t)])
    return excess


def white_reality_check(
    benchmark: Sequence[float],
    candidates: Sequence[Sequence[float]],
    n_bootstrap: int = 1000,
    block_mean: float = 10.0,
    seed: int = 0,
) -> dict:
    """White (2000) Reality Check p-value for the best candidate's mean excess."""
    excess = _excess(benchmark, candidates)
    if not excess:
        return {"statistic": 0.0, "p_value": 1.0, "n_candidates": 0, "n_bootstrap": n_bootstrap}
    t = len(excess[0])
    means = [statistics.mean(s) for s in excess]
    statistic = max(means)
    centered = [[s[i] - means[k] for i in range(t)] for k, s in enumerate(excess)]

    boot = [stationary_bootstrap(centered[k], n_bootstrap, block_mean, seed=seed + k) for k in range(len(centered))]
    count = 0
    for b in range(n_bootstrap):
        v = max(statistics.mean(boot[k][b]) for k in range(len(centered)))
        if v >= statistic:
            count += 1
    p_value = (count + 1.0) / (n_bootstrap + 1.0)
    return {
        "statistic": statistic,
        "p_value": p_value,
        "n_candidates": len(excess),
        "observations": t,
        "n_bootstrap": n_bootstrap,
    }


def spa_test(
    benchmark: Sequence[float],
    candidates: Sequence[Sequence[float]],
    n_bootstrap: int = 1000,
    block_mean: float = 10.0,
    seed: int = 0,
) -> dict:
    """Hansen (2005) SPA test: Reality Check with studentised statistics.

    The studentisation down-weights high-variance candidates, making the test
    less conservative than the plain Reality Check.
    """
    excess = _excess(benchmark, candidates)
    if not excess:
        return {"statistic": 0.0, "p_value": 1.0, "n_candidates": 0, "n_bootstrap": n_bootstrap}
    t = len(excess[0])

    def studentised_stats(series: Sequence[Sequence[float]]) -> List[float]:
        out = []
        for s in series:
            mean = statistics.mean(s)
            std = statistics.pstdev(s)
            out.append(mean / std if std > 1e-16 else 0.0)
        return out

    means = [statistics.mean(s) for s in excess]
    stats = studentised_stats(excess)
    statistic = max(stats)
    centered = [[s[i] - means[k] for i in range(t)] for k, s in enumerate(excess)]

    boot = [stationary_bootstrap(centered[k], n_bootstrap, block_mean, seed=seed + k) for k in range(len(centered))]
    count = 0
    for b in range(n_bootstrap):
        v = max(studentised_stats([boot[k][b] for k in range(len(centered))]))
        if v >= statistic:
            count += 1
    p_value = (count + 1.0) / (n_bootstrap + 1.0)
    return {
        "statistic": statistic,
        "p_value": p_value,
        "n_candidates": len(excess),
        "observations": t,
        "n_bootstrap": n_bootstrap,
    }
