"""按基础任务聚类的配对 bootstrap + 多假设 Holm 校正（EVAL-05）。

为什么必须"配对 + 按基础任务聚类"
----------------------------------
1. **配对**：两个配置跑的是同一批基础任务，比较的是同一任务上的差异，
   而不是两批独立样本的均值差；
2. **按任务聚类**：同一基础任务在重复运行中不是独立观测（同样的措辞、同样的初态），
   因此重采样单位是**任务**而不是"任务 × 重复"。若按运行重采样，会把区间算得过窄。

实现要点（纯 Python，无 numpy）
------------------------------
- 每个基础任务聚合为一个值（默认取该任务在重复中的通过率，即 0/0.5/1 这类）；
- 重采样时**整簇抽取**任务（有放回），重算差值均值；
- 百分位区间给 95% 区间；同时给近似的双侧 p 值（差值分布跨过 0 的比例）；
- 多配置 × 多指标时用 **Holm-Bonferroni** 逐步校正，控制族错误率。

诚实边界
--------
离线模式下这些检验只能说明"工程实现差异是否稳定"，**不能**外推为"模型能力差异"。
样本量小时（例如 12 个任务）区间会很宽，报告必须原样给出宽度，不得只报点估计。
"""

from __future__ import annotations

import random
import statistics
from typing import Any, Callable, Iterable, Sequence

DEFAULT_ITERATIONS = 5000
DEFAULT_SEED = 20261022


def cluster_by_task(records: Sequence[dict], *, metric: Callable[[dict], float]) -> dict[str, float]:
    """把逐运行记录聚合成"每个基础任务一个值"（默认：通过率）。"""
    buckets: dict[str, list[float]] = {}
    for record in records:
        case = str(record.get("test_case_id"))
        buckets.setdefault(case, []).append(float(metric(record)))
    return {case: statistics.fmean(values) for case, values in buckets.items()}


def pass_indicator(record: dict) -> float:
    """默认指标：该次运行是否达到要求终态（与 ``summarize._is_pass`` 同口径）。"""
    from .summarize import _is_pass  # 复用同一口径，避免"两套通过定义"

    return 1.0 if _is_pass(record) else 0.0


def paired_bootstrap_diff(
    baseline: dict[str, float],
    treatment: dict[str, float],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    confidence: float = 0.95,
) -> dict:
    """配对 bootstrap：同一任务上的 ``treatment − baseline`` 差值。"""
    shared = sorted(set(baseline) & set(treatment))
    if not shared:
        raise ValueError("两个配置没有共同的基础任务，无法做配对比较")
    diffs = [treatment[case] - baseline[case] for case in shared]
    observed = statistics.fmean(diffs)
    rng = random.Random(seed)
    n = len(shared)
    samples: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    alpha = (1 - confidence) / 2
    low = samples[max(0, int(alpha * iterations) - 1)]
    high = samples[min(iterations - 1, int((1 - alpha) * iterations))]
    # 双侧 p 值：bootstrap 分布中跨过 0 的比例（保守的两倍单侧比例）
    below = sum(1 for value in samples if value <= 0) / iterations
    above = sum(1 for value in samples if value >= 0) / iterations
    p_value = min(1.0, 2 * min(below, above))
    return {
        "metric": "paired_mean_difference",
        "n_tasks": n,
        "baseline_mean": round(statistics.fmean([baseline[c] for c in shared]), 4),
        "treatment_mean": round(statistics.fmean([treatment[c] for c in shared]), 4),
        "observed_diff": round(observed, 4),
        "ci_low": round(low, 4),
        "ci_high": round(high, 4),
        "confidence": confidence,
        "p_value_approx": round(p_value, 4),
        "iterations": iterations,
        "seed": seed,
        "wins": sum(1 for d in diffs if d > 0),
        "losses": sum(1 for d in diffs if d < 0),
        "ties": sum(1 for d in diffs if d == 0),
        "note": "按基础任务聚类重采样；同一样本内重复不当作独立观测。",
    }


def holm_correction(p_values: Sequence[tuple[str, float]], *, alpha: float = 0.05) -> list[dict]:
    """Holm-Bonferroni 逐步校正；返回按原始顺序排列的校正结果。"""
    items = [(name, float(p)) for name, p in p_values]
    order = sorted(range(len(items)), key=lambda i: items[i][1])
    m = len(items)
    results: list[dict | None] = [None] * m
    previous_reject = True
    for rank, index in enumerate(order):
        name, p = items[index]
        threshold = alpha / (m - rank)
        reject = previous_reject and p <= threshold
        previous_reject = reject
        results[index] = {
            "hypothesis": name,
            "p_value": round(p, 6),
            "rank": rank + 1,
            "holm_threshold": round(threshold, 6),
            "reject_null": bool(reject),
        }
    return [item for item in results if item is not None]


def compare_configs(
    records_by_config: dict[str, Sequence[dict]],
    *,
    baseline: str = "FULL",
    metric: Callable[[dict], float] = pass_indicator,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
) -> dict:
    """一次比较多组配置：先配对 bootstrap，再做 Holm 校正。"""
    if baseline not in records_by_config:
        raise ValueError(f"基线配置 {baseline} 不在输入里")
    baseline_clusters = cluster_by_task(records_by_config[baseline], metric=metric)
    comparisons: list[dict] = []
    raw_p: list[tuple[str, float]] = []
    for name, records in sorted(records_by_config.items()):
        if name == baseline:
            continue
        clusters = cluster_by_task(records, metric=metric)
        result = paired_bootstrap_diff(baseline_clusters, clusters, iterations=iterations, seed=seed)
        result["config"] = name
        comparisons.append(result)
        raw_p.append((name, result["p_value_approx"]))
    corrected = holm_correction(raw_p, alpha=alpha)
    by_name = {item["hypothesis"]: item for item in corrected}
    for comparison in comparisons:
        comparison["holm"] = by_name.get(comparison["config"])
    return {
        "baseline": baseline,
        "alpha": alpha,
        "metric": getattr(metric, "__name__", str(metric)),
        "comparisons": comparisons,
        "multiple_comparison_correction": "holm_bonferroni",
        "note": (
            "离线模式：差异只能读作工程回归；样本量小时区间很宽，"
            "不得只报点估计或据此声称模型能力提升。"
        ),
    }


__all__ = [
    "cluster_by_task",
    "pass_indicator",
    "paired_bootstrap_diff",
    "holm_correction",
    "compare_configs",
]