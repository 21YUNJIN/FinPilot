"""评测分析包（EVAL-05）。

- :mod:`eval.analysis.summarize` —— 分分母聚合、Wilson 95% 区间、pass^k；
- :mod:`eval.analysis.paired_bootstrap` —— 按基础任务聚类的配对 bootstrap 与 Holm 校正；
- :mod:`eval.analysis.failure_taxonomy` —— 失败归因分类。

全部为纯 Python 实现（本仓库不依赖 numpy）。
"""

from __future__ import annotations

from .failure_taxonomy import TAXONOMY, classify, taxonomy_report
from .paired_bootstrap import (
    cluster_by_task,
    compare_configs,
    holm_correction,
    paired_bootstrap_diff,
    pass_indicator,
)
from .summarize import (
    bootstrap_median_interval,
    compare_configs as summarize_compare,
    load_jsonl,
    pass_pow_k,
    rate_view,
    summarize_file,
    summarize_records,
    wilson_interval,
)

__all__ = [
    "TAXONOMY",
    "classify",
    "taxonomy_report",
    "cluster_by_task",
    "compare_configs",
    "holm_correction",
    "paired_bootstrap_diff",
    "pass_indicator",
    "bootstrap_median_interval",
    "summarize_compare",
    "load_jsonl",
    "pass_pow_k",
    "rate_view",
    "summarize_file",
    "summarize_records",
    "wilson_interval",
]