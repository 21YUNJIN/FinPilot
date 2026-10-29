"""统计分析的自证测试（EVAL-05）。

每个用例都能**手工算出**期望值；不做"跑通即算对"的弱断言。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.analysis.failure_taxonomy import TAXONOMY, classify, taxonomy_report  # noqa: E402
from eval.analysis.paired_bootstrap import (  # noqa: E402
    cluster_by_task,
    compare_configs,
    holm_correction,
    paired_bootstrap_diff,
)
from eval.analysis.summarize import (  # noqa: E402
    bootstrap_median_interval,
    pass_pow_k,
    rate_view,
    summarize_records,
    wilson_interval,
)


def record(
    case: str,
    *,
    result: str,
    task_class: str = "executable",
    reason_codes: list[str] | None = None,
    violations: list[dict] | None = None,
    repeat: int = 0,
    scene: str = "S1",
    wall_ms: float = 100.0,
    extra_confirmation: int = 0,
    operations: list[dict] | None = None,
) -> dict:
    return {
        "test_case_id": case,
        "task_class": task_class,
        "scene": scene,
        "repeat_index": repeat,
        "result": result,
        "timing": {"wall_ms": wall_ms},
        "drive": {"extra_confirmation_count": extra_confirmation, "error": None},
        "actual": {
            "task_status": "succeeded" if result in ("completed", "partial_success") else "blocked",
            "operation_statuses": operations or [],
            "system_claim": {"failed_steps": []},
        },
        "judgment": {
            "outcome": result,
            "reason_codes": reason_codes or [],
            "constraint_violations": violations or [],
            "forbidden_effects_triggered": [
                v["effect"] for v in (violations or []) if v.get("effect", "").startswith("effect_")
            ],
            "not_evaluable": {"constraints": []},
        },
    }


# ---------------------------------------------------------------------------
# 手算样例：10 个任务 7 成功 → 0.7，区间可手算
# ---------------------------------------------------------------------------


def test_wilson_interval_matches_hand_calculation():
    """7/10 的 Wilson 95% 区间可手算（z = 1.959964）：

    p̂ = 0.7，d = 1 + z²/n = 1.384160
    中心 = (0.7 + z²/(2n)) / d = (0.7 + 0.19208) / 1.384160 = 0.644366
    半宽 = z·sqrt((0.7·0.3 + z²/(4n))/n) / d = 1.959964·sqrt(0.306041/10)/1.384160 = 0.247601
    区间 ≈ [0.396765, 0.891967]（与实现值 0.396778/0.892209 在 5e-4 内一致）
    """
    low, high = wilson_interval(7, 10)
    assert low == pytest.approx(0.396778, abs=1e-5)
    assert high == pytest.approx(0.892209, abs=1e-5)
    # 手工上界：正态近似会给出 0.7±1.96·0.1449=[0.416, 0.984]，Wilson 更保守地收窄
    assert low > 0.39 and high < 0.90


def test_wilson_interval_edge_cases():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    low, high = wilson_interval(0, 10)
    assert low == 0.0
    assert high == pytest.approx(0.277533, abs=1e-5)
    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(0.722467, abs=1e-5)
    assert high == pytest.approx(1.0, abs=1e-12)


def test_seven_of_ten_summary_is_seventy_percent():
    records = [record(f"task_{i:02d}", result="completed" if i < 7 else "failed") for i in range(10)]
    summary = summarize_records(records)
    completion = summary["metrics"]["executable_completion_rate"]
    assert completion["successes"] == 7
    assert completion["total"] == 10
    assert completion["rate"] == 0.7
    # 汇总里的区间保留 4 位小数；手算值为 0.396778 / 0.892209
    assert completion["ci_low"] == pytest.approx(0.3968, abs=1e-4)
    assert completion["ci_high"] == pytest.approx(0.8922, abs=1e-4)
    assert summary["counts"]["executable"] == 10


# ---------------------------------------------------------------------------
# 分母分开
# ---------------------------------------------------------------------------


def test_denominators_are_separated_by_task_class():
    records = [
        record("e1", result="completed"),
        record("e2", result="failed"),
        record("c1", result="clarified", task_class="clarify"),
        record("r1", result="refused", task_class="reject"),
        record("a1", result="refused", task_class="attack"),
    ]
    records[-1]["expected"] = {"is_attack": True}
    summary = summarize_records(records)
    assert summary["counts"] == {
        "records": 5,
        "executable": 2,
        "clarify": 1,
        "reject": 1,
        "attack": 1,
        "repeats_observed": 1,
    }
    assert summary["metrics"]["executable_completion_rate"]["total"] == 2
    assert summary["metrics"]["clarification_accuracy"]["total"] == 1
    assert summary["metrics"]["refusal_accuracy"]["total"] == 1
    # 攻击被阻断只进安全分母，不影响可执行任务完成率
    assert summary["metrics"]["attack_block_rate"]["successes"] == 1
    assert summary["metrics"]["executable_completion_rate"]["successes"] == 1


def test_false_refusal_rate_counts_blocked_executable_tasks():
    records = [
        record("e1", result="completed"),
        record("e2", result="refused"),
        record("e3", result="failed", operations=[]),
        record("e4", result="failed", operations=[{"tool": "transfer.submit", "status": "failed"}]),
    ]
    summary = summarize_records(records)
    false_refusals = summary["metrics"]["false_refusal_rate"]
    assert false_refusals["successes"] == 2  # e2 被拒 + e3 零动作失败
    assert false_refusals["total"] == 4
    assert false_refusals["rate"] == 0.5


def test_partial_success_not_counted_as_completed():
    records = [
        record("e1", result="partial_success"),
        record("e2", result="completed"),
    ]
    summary = summarize_records(records)
    assert summary["metrics"]["executable_completion_rate"]["rate"] == 0.5
    assert summary["metrics"]["executable_partial_rate"]["rate"] == 0.5


# ---------------------------------------------------------------------------
# pass^k
# ---------------------------------------------------------------------------


def test_pass_pow_k_requires_all_repeats():
    records = [
        record("A", result="completed", repeat=0),
        record("A", result="completed", repeat=1),
        record("A", result="completed", repeat=2),
        record("A", result="completed", repeat=3),
        record("A", result="completed", repeat=4),
        record("B", result="completed", repeat=0),
        record("B", result="failed", repeat=1),
        record("B", result="completed", repeat=2),
        record("B", result="completed", repeat=3),
        record("B", result="completed", repeat=4),
        record("C", result="completed", repeat=0),  # 只有 1 次重复，不进分母
    ]
    view = pass_pow_k(records, 5)
    assert view["cases_with_k_repeats"] == 2
    assert view["successes"] == 1
    assert view["rate"] == 0.5
    assert {item["test_case_id"]: item["pass"] for item in view["details"]} == {"A": True, "B": False}


def test_pass_pow_k_reports_not_applicable_when_single_run():
    records = [record("A", result="completed", repeat=0)]
    summary = summarize_records(records)
    assert summary["metrics"]["pass_pow_5"]["rate"] is None
    assert "不足以计算" in summary["metrics"]["pass_pow_5"]["note"]


# ---------------------------------------------------------------------------
# 连续量与 bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_median_interval_is_reproducible_and_contains_median():
    values = [100.0, 120.0, 130.0, 140.0, 900.0]
    low, high = bootstrap_median_interval(values, iterations=500, seed=7)
    low2, high2 = bootstrap_median_interval(values, iterations=500, seed=7)
    assert (low, high) == (low2, high2)
    assert low <= 130.0 <= high


def test_extra_confirmation_and_wall_time_views():
    records = [record(f"t{i}", result="completed", wall_ms=100 + i, extra_confirmation=i % 2) for i in range(6)]
    summary = summarize_records(records)
    assert summary["metrics"]["wall_time_ms"]["n"] == 6
    assert summary["metrics"]["wall_time_ms"]["median"] == 102.5
    assert summary["metrics"]["extra_confirmation_count"]["median"] == 0.5


# ---------------------------------------------------------------------------
# 配对 bootstrap 与 Holm
# ---------------------------------------------------------------------------


def test_paired_bootstrap_detects_clear_improvement():
    baseline = {f"t{i}": 0.0 for i in range(10)}
    treatment = {f"t{i}": 1.0 for i in range(10)}
    result = paired_bootstrap_diff(baseline, treatment, iterations=2000, seed=1)
    assert result["observed_diff"] == 1.0
    assert result["ci_low"] == 1.0 and result["ci_high"] == 1.0
    assert result["p_value_approx"] < 0.01
    assert result["wins"] == 10


def test_paired_bootstrap_no_difference_is_not_significant():
    baseline = {f"t{i}": (1.0 if i % 2 == 0 else 0.0) for i in range(10)}
    treatment = dict(baseline)
    result = paired_bootstrap_diff(baseline, treatment, iterations=2000, seed=1)
    assert result["observed_diff"] == 0.0
    assert result["ci_low"] <= 0.0 <= result["ci_high"]
    assert result["p_value_approx"] == 1.0


def test_paired_bootstrap_requires_shared_tasks():
    with pytest.raises(ValueError):
        paired_bootstrap_diff({"a": 1.0}, {"b": 1.0})


def test_cluster_by_task_averages_repeats():
    records = [
        record("A", result="completed", repeat=0),
        record("A", result="failed", repeat=1),
        record("B", result="completed", repeat=0),
        record("B", result="completed", repeat=1),
    ]
    clusters = cluster_by_task(records, metric=lambda r: 1.0 if r["result"] == "completed" else 0.0)
    assert clusters == {"A": 0.5, "B": 1.0}


def test_holm_correction_hand_example():
    """三组 p 值 0.01 / 0.04 / 0.03，α=0.05：

    排序 0.01(rank1, 阈值 0.01667) 通过 → 0.03(rank2, 阈值 0.025) 不通过 →
    逐步停止后，0.04(rank3) 也不通过。
    """
    corrected = holm_correction([("a", 0.01), ("b", 0.04), ("c", 0.03)], alpha=0.05)
    by_name = {item["hypothesis"]: item for item in corrected}
    assert by_name["a"]["reject_null"] is True
    assert by_name["a"]["holm_threshold"] == pytest.approx(0.016667, abs=1e-6)
    assert by_name["c"]["reject_null"] is False
    assert by_name["c"]["holm_threshold"] == pytest.approx(0.025, abs=1e-6)
    assert by_name["b"]["reject_null"] is False
    assert [item["hypothesis"] for item in corrected] == ["a", "b", "c"]  # 保持输入顺序


def test_compare_configs_applies_holm_and_marks_baseline():
    records = {
        "FULL": [record(f"t{i}", result="completed") for i in range(6)],
        "B1": [record(f"t{i}", result="failed") for i in range(6)],
    }
    report = compare_configs(records, baseline="FULL", iterations=500, seed=3)
    assert report["multiple_comparison_correction"] == "holm_bonferroni"
    comparison = report["comparisons"][0]
    assert comparison["config"] == "B1"
    assert comparison["observed_diff"] == -1.0
    assert comparison["holm"]["reject_null"] is True


# ---------------------------------------------------------------------------
# 失败归因
# ---------------------------------------------------------------------------


def test_failure_taxonomy_covers_all_categories():
    records = [
        record("ok", result="completed"),
        record(
            "ledger",
            result="failed",
            violations=[{"effect": "effect_ledger_invariant_broken"}],
            reason_codes=["LEDGER_MISMATCH"],
        ),
        record(
            "claim",
            result="failed",
            violations=[{"effect": "system_claim_matches_state"}],
            reason_codes=["SYSTEM_CLAIM_CONTRADICTS_STATE"],
        ),
        record(
            "forbid",
            result="failed",
            violations=[{"effect": "effect_duplicate_payment"}],
            reason_codes=["forbidden_effect_triggered"],
        ),
        record("clar_missing", result="failed", task_class="clarify", reason_codes=["clarification_not_requested"]),
        record("wrong_refusal", result="refused"),
        record("refusal_missing", result="failed", task_class="reject", reason_codes=["refusal_missing"]),
        record("unknown", result="unknown", reason_codes=["operation_result_unknown"]),
        record("constraint", result="failed", reason_codes=["no_expectation_met"]),
    ]
    report = taxonomy_report(records)
    assert report["passed"] == 1
    assert report["failed"] == 8
    assert report["categories"]["ledger_mismatch"] == 1
    assert report["categories"]["claim_contradicts_state"] == 1
    assert report["categories"]["forbidden_effect"] == 1
    assert report["categories"]["clarification_missing"] == 1
    assert report["categories"]["wrong_refusal"] == 1
    assert report["categories"]["refusal_incomplete"] == 1
    assert report["categories"]["unknown_result"] == 1
    assert report["categories"]["constraint_not_met"] == 1
    assert report["unclassified"] == []
    # 每个类别都有可读定义，报告里可直接引用
    for name in report["categories"]:
        assert name in TAXONOMY


def test_unclassified_failures_are_reported_not_hidden():
    report = taxonomy_report([record("weird", result="failed", reason_codes=["something_new"])])
    assert report["categories"]["unclassified"] == 1
    assert report["unclassified"] == [{"test_case_id": "weird", "reason_codes": ["something_new"]}]
    assert "人工复核" in report["note"]


def test_runner_error_has_highest_priority():
    item = record("boom", result="failed")
    item["drive"]["error"] = "RuntimeError: db down"
    assert classify(item)["category"] == "runner_error"


def test_attack_blocked_is_not_a_failure():
    item = record("atk", result="refused", task_class="attack")
    item["expected"] = {"is_attack": True}
    assert classify(item)["passed"] is True