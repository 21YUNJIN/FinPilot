"""账单异常识别：可解释规则 + 统计偏离（V5 第 10 节 S2-03、T08）。

边界（必须如实保留）：

- 规则是**确定性阈值规则**，输出的是"值得看一眼"的提示，不是欺诈定论。
  每条异常都带 ``threshold_version``、结构化依据与可读解释，且明确
  ``is_fraud_conclusion: False``。
- 本模块**只读**：不修改卡状态、不撤销交易、不改写流水；侧效果只能由用户或
  风控运营在独立流程里确认。
- 只有合成数据带 ``anomaly_label``；真实来源没有标签时不得用标签子集
  推算的 TP/FP/FN 声称准确率，评估结果里会写明 ``label_source``。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from ...money import format_minor
from ...timeutil import format_iso, to_utc
from . import classification as cls

THRESHOLD_VERSION = "bill-anomaly-2026.10"

#: 规则 1：同商户同额，24 小时内 ≥2 次。
DUPLICATE_WINDOW = timedelta(hours=24)
DUPLICATE_MIN_COUNT = 2
DUPLICATE_HIGH_SEVERITY_MINOR = 100_000  # 单笔 ≥1000 元视为更高影响

#: 规则 2：相同商户，2 小时内 ≥3 次。
REPEAT_WINDOW = timedelta(hours=2)
REPEAT_MIN_COUNT = 3

#: 规则 3：同分类历史均值 ±3 倍 MAD（稳健离群）。
MAD_MULTIPLIER = 3.0
MAD_SCALE = 1.4826  # 正态一致化系数：MAD → 标准差
MIN_HISTORY_SAMPLES = 8
MAD_ZERO_FALLBACK_MULTIPLIER = 3.0

#: 合成标签里表示"正常"的取值（不参与 TP/FP/FN）。
NORMAL_LABEL_VALUES = frozenset({"", "none", "null", "normal", "ok", "clean"})

DISCLAIMER = (
    "异常提示来自确定性规则与统计偏离，只能作为核查线索：不等于欺诈，"
    "也不会因此冻结卡片或撤销交易。"
)


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _posted_at(row: Any) -> datetime:
    value = _get(row, "posted_at")
    if not isinstance(value, datetime):
        raise TypeError("异常识别需要带时区的 posted_at")
    return to_utc(value)


def _bt_id(row: Any) -> str:
    return str(_get(row, "bt_id", "") or "")


def _merchant_key(row: Any) -> str:
    for field in ("merchant_name", "counterparty"):
        text = str(_get(row, field, "") or "").strip()
        if text:
            return text
    return ""


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        raise ValueError("median of empty sequence")
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _sliding_clusters(
    items: list[dict[str, Any]], window: timedelta, min_count: int
) -> list[list[dict[str, Any]]]:
    """按时间切片：返回互不重叠、成员数 ≥ min_count 的极大窗口。"""
    clusters: list[list[dict[str, Any]]] = []
    n = len(items)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1]["_at"] - items[i]["_at"] <= window:
            j += 1
        if j - i + 1 >= min_count:
            clusters.append(items[i : j + 1])
            i = j + 1
        else:
            i += 1
    return clusters


def _candidate_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """扣费类候选：支出、非自己账户互转。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        if cls.direction_of(row) != "out":
            continue
        kind = _get(row, "kind", "")
        if cls.is_internal_transfer(kind):
            continue
        merchant = _merchant_key(row)
        if not merchant:
            continue
        out.append(
            {
                "row": row,
                "bt_id": _bt_id(row),
                "_at": _posted_at(row),
                "merchant": merchant,
                "amount_minor": cls.magnitude_minor(row),
            }
        )
    out.sort(key=lambda item: (item["_at"], item["bt_id"]))
    return out


def detect_duplicate_same_amount(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """同商户同额，24 小时内 ≥2 次（重复扣费嫌疑）。"""
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in _candidate_rows(rows):
        groups[(item["merchant"], item["amount_minor"])].append(item)

    anomalies: list[dict[str, Any]] = []
    for (merchant, amount), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        for cluster in _sliding_clusters(items, DUPLICATE_WINDOW, DUPLICATE_MIN_COUNT):
            first, last = cluster[0]["_at"], cluster[-1]["_at"]
            severity = "high" if amount >= DUPLICATE_HIGH_SEVERITY_MINOR else "medium"
            anomalies.append(
                {
                    "type": "duplicate_same_amount",
                    "bt_ids": [item["bt_id"] for item in cluster],
                    "threshold_version": THRESHOLD_VERSION,
                    "severity": severity,
                    "is_fraud_conclusion": False,
                    "explanation": (
                        f"商户「{merchant}」在 {int(DUPLICATE_WINDOW.total_seconds() // 3600)} 小时内出现 "
                        f"{len(cluster)} 笔同额扣费（每笔 {format_minor(amount)} 元），"
                        f"窗口 {format_iso(first)} ~ {format_iso(last)}。"
                    ),
                    "basis": {
                        "rule": "duplicate_same_amount",
                        "merchant": merchant,
                        "amount_minor": amount,
                        "count": len(cluster),
                        "min_count": DUPLICATE_MIN_COUNT,
                        "window_hours": int(DUPLICATE_WINDOW.total_seconds() // 3600),
                        "window_start": format_iso(first),
                        "window_end": format_iso(last),
                    },
                }
            )
    return anomalies


def detect_merchant_short_window_repeat(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """相同商户 2 小时内 ≥3 次（短时间内集中扣费）。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _candidate_rows(rows):
        groups[item["merchant"]].append(item)

    anomalies: list[dict[str, Any]] = []
    for merchant, items in sorted(groups.items()):
        for cluster in _sliding_clusters(items, REPEAT_WINDOW, REPEAT_MIN_COUNT):
            first, last = cluster[0]["_at"], cluster[-1]["_at"]
            total = sum(item["amount_minor"] for item in cluster)
            anomalies.append(
                {
                    "type": "merchant_short_window_repeat",
                    "bt_ids": [item["bt_id"] for item in cluster],
                    "threshold_version": THRESHOLD_VERSION,
                    "severity": "medium",
                    "is_fraud_conclusion": False,
                    "explanation": (
                        f"商户「{merchant}」在 {int(REPEAT_WINDOW.total_seconds() // 3600)} 小时内出现 "
                        f"{len(cluster)} 笔扣费（合计 {format_minor(total)} 元），"
                        f"窗口 {format_iso(first)} ~ {format_iso(last)}。"
                    ),
                    "basis": {
                        "rule": "merchant_short_window_repeat",
                        "merchant": merchant,
                        "count": len(cluster),
                        "min_count": REPEAT_MIN_COUNT,
                        "window_hours": int(REPEAT_WINDOW.total_seconds() // 3600),
                        "total_minor": total,
                        "window_start": format_iso(first),
                        "window_end": format_iso(last),
                    },
                }
            )
    return anomalies


def detect_history_deviation(
    rows: Sequence[Any], history_rows: Sequence[Any] | None
) -> list[dict[str, Any]]:
    """同分类显著偏离个人历史：以历史中位数与 MAD 为基线。

    样本不足 ``MIN_HISTORY_SAMPLES`` 时不出结论（不猜）。基线只用**本期之前**的
    历史流水，避免本期自己的异常把基线抬高。
    """
    if not history_rows:
        return []

    baselines: dict[str, list[int]] = defaultdict(list)
    for row in history_rows:
        if not cls.is_consumption_candidate(row):
            continue
        category = str(_get(row, "category", "") or "") or cls.DEFAULT_CATEGORY
        baselines[category].append(cls.magnitude_minor(row))

    stats: dict[str, dict[str, float]] = {}
    for category, amounts in baselines.items():
        if len(amounts) < MIN_HISTORY_SAMPLES:
            continue
        median = _median(amounts)
        mad = _median([abs(value - median) for value in amounts])
        stats[category] = {
            "sample_count": float(len(amounts)),
            "median_minor": median,
            "mad_minor": mad,
            "limit_minor": MAD_MULTIPLIER * MAD_SCALE * mad,
        }

    flagged: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not cls.is_consumption_candidate(row):
            continue
        category = str(_get(row, "category", "") or "") or cls.DEFAULT_CATEGORY
        stat = stats.get(category)
        if stat is None:
            continue
        amount = cls.magnitude_minor(row)
        median = stat["median_minor"]
        mad = stat["mad_minor"]
        if mad > 0:
            limit = stat["limit_minor"]
            if abs(amount - median) <= limit:
                continue
            z = 0.6745 * (amount - median) / mad
            severity = "high" if abs(z) >= 8 else ("medium" if abs(z) >= 4 else "low")
            basis_threshold = limit
            basis_kind = "median_plus_minus_3_mad"
        else:
            if median <= 0 or amount <= median * MAD_ZERO_FALLBACK_MULTIPLIER:
                continue
            ratio = amount / median
            severity = "high" if ratio >= 10 else ("medium" if ratio >= 5 else "low")
            basis_threshold = median * MAD_ZERO_FALLBACK_MULTIPLIER
            basis_kind = "median_multiplier_fallback_mad_zero"
        flagged[category].append(
            {
                "bt_id": _bt_id(row),
                "amount_minor": amount,
                "posted_at": format_iso(_posted_at(row)),
                "basis_threshold_minor": round(basis_threshold, 2),
            }
        )

    anomalies: list[dict[str, Any]] = []
    for category, items in sorted(flagged.items()):
        stat = stats[category]
        amounts_text = "、".join(format_minor(item["amount_minor"]) for item in items)
        anomalies.append(
            {
                "type": "deviation_from_personal_history",
                "bt_ids": [item["bt_id"] for item in items],
                "threshold_version": THRESHOLD_VERSION,
                "severity": "low",
                "is_fraud_conclusion": False,
                "explanation": (
                    f"分类「{cls.label_for(category)}」本期有 {len(items)} 笔支出（{amounts_text} 元）"
                    f"偏离个人历史区间：历史中位数 {format_minor(int(stat['median_minor']))} 元、"
                    f"MAD {format_minor(int(stat['mad_minor']))} 元，判定阈值 ±{MAD_MULTIPLIER:g}×MAD"
                    f"（样本 {int(stat['sample_count'])} 笔）。"
                ),
                "basis": {
                    "rule": "deviation_from_personal_history",
                    "category": category,
                    "sample_count": int(stat["sample_count"]),
                    "median_minor": int(stat["median_minor"]),
                    "mad_minor": int(stat["mad_minor"]),
                    "mad_multiplier": MAD_MULTIPLIER,
                    "threshold_kind": basis_kind,
                    "limit_minor": round(stat["limit_minor"], 2),
                    "flagged": items,
                },
            }
        )
    return anomalies


def detect_anomalies(
    rows: Sequence[Any],
    *,
    history_rows: Sequence[Any] | None = None,
    context_rows: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """执行全部异常规则。

    :param rows: 本期流水（报告期内）。
    :param history_rows: 本期之前的同用户历史流水，仅用于统计基线。
    :param context_rows: 紧邻本期之前的一小段流水，用于识别跨账期边界的重复扣费；
        命中时会标注 ``references_rows_outside_period``。
    """
    period_ids = {_bt_id(row) for row in rows}
    scan_rows: list[Any] = list(rows) + list(context_rows or [])

    anomalies = detect_duplicate_same_amount(scan_rows) + detect_merchant_short_window_repeat(scan_rows)
    anomalies.extend(detect_history_deviation(rows, history_rows))

    result: list[dict[str, Any]] = []
    for anomaly in anomalies:
        if not set(anomaly["bt_ids"]) & period_ids:
            continue
        outside = sorted(set(anomaly["bt_ids"]) - period_ids)
        anomaly["references_rows_outside_period"] = outside
        result.append(anomaly)
    result.sort(key=lambda item: (item["type"], item["bt_ids"]))
    return result


def _is_labeled(row: Any) -> bool:
    label = _get(row, "anomaly_label", None)
    if label is None:
        return False
    return str(label).strip().lower() not in NORMAL_LABEL_VALUES


def evaluate_against_labels(
    rows: Sequence[Any], anomalies: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """在有合成标签的子集上计算 TP/FP/FN。

    没有标签的流水不参与：它们既不算 FP 也不算 FN。真实来源数据无标签时
    ``status`` 为 ``not_applicable``，不得据此声称准确率。
    """
    labeled_ids = {_bt_id(row) for row in rows if _is_labeled(row)}
    all_ids = {_bt_id(row) for row in rows if _bt_id(row)}
    flagged_ids: set[str] = set()
    for anomaly in anomalies:
        flagged_ids |= set(anomaly["bt_ids"])
    flagged_in_period = flagged_ids & all_ids

    if not labeled_ids:
        return {
            "status": "not_applicable",
            "label_source": "synthetic",
            "reason": "本期没有带 anomaly_label 的流水；真实来源数据没有标签时不得推算准确率。",
            "tp": None,
            "fp": None,
            "fn": None,
            "precision": None,
            "recall": None,
            "labeled_rows": 0,
            "flagged_rows": len(flagged_in_period),
        }

    tp = len(flagged_in_period & labeled_ids)
    fp = len(flagged_in_period - labeled_ids)
    fn = len(labeled_ids - flagged_in_period)
    return {
        "status": "ok",
        "label_source": "synthetic",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
        "recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
        "labeled_rows": len(labeled_ids),
        "flagged_rows": len(flagged_in_period),
        "note": "仅在带合成标签的子集上计算，不代表真实来源数据的准确率。",
    }


def anomaly_summary(rows: Sequence[Any], anomalies: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = defaultdict(int)
    bt_ids: set[str] = set()
    for anomaly in anomalies:
        by_type[anomaly["type"]] += 1
        bt_ids |= set(anomaly["bt_ids"])
    return {
        "count": len(anomalies),
        "by_type": dict(sorted(by_type.items())),
        "types": sorted(by_type),
        "affected_transaction_count": len(bt_ids),
        "threshold_version": THRESHOLD_VERSION,
        "is_fraud_conclusion": False,
        "changes_card_state": False,
        "cancels_transaction": False,
        "feedback_supported": False,
        "feedback_note": (
            "反馈记录需要写入账单模块的持久表；本模块只输出可解释规则结果，"
            "不擅自改写流水标签，因此反馈入口由账单网关承担。"
        ),
        "labeled_evaluation": evaluate_against_labels(rows, anomalies),
        "note": DISCLAIMER,
    }


__all__ = [
    "DISCLAIMER",
    "DUPLICATE_MIN_COUNT",
    "DUPLICATE_WINDOW",
    "MAD_MULTIPLIER",
    "MIN_HISTORY_SAMPLES",
    "REPEAT_MIN_COUNT",
    "REPEAT_WINDOW",
    "THRESHOLD_VERSION",
    "anomaly_summary",
    "detect_anomalies",
    "detect_duplicate_same_amount",
    "detect_history_deviation",
    "detect_merchant_short_window_repeat",
    "evaluate_against_labels",
]