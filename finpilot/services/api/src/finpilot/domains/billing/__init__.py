"""账单领域（S2 账单分析）：分类、异常识别、月／年／自选账期报表。

对上层只暴露确定性接口：

- ``bill_report``：月报／年报／自选区间报表（消费分析 + 现金流），
  每个汇总数字带 ``source_ids`` 与 ``formula``。
- ``correct_category``：分类纠正（只写纠正记录，不改原流水）。
- ``classify``：确定性分类规则 ``(category, rule_id, confidence)``。
- ``detect_anomalies``：可解释异常规则（不是欺诈判定）。
- ``TOOL_HANDLERS``：按 ``domains/__init__.py`` 约定导出的领域处理器。

``bill.report`` 是 GREEN 只读工具、无资金效果，因此处理器天然幂等。
``correct_category`` 不在 ``TOOL_HANDLERS`` 里：它写纠正记录但不挂
``operation_id``，需要调用方（网关／编排）自行保证幂等后再调用。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...timeutil import business_day, utcnow
from .anomalies import (
    THRESHOLD_VERSION,
    anomaly_summary,
    detect_anomalies,
    evaluate_against_labels,
)
from .classification import (
    CATEGORIES,
    CATEGORY_LABELS,
    CLASSIFIER_VERSION,
    classify,
    explain_classification,
    label_for,
)
from .service import (
    BASES,
    PERIOD_KINDS,
    REPORT_VERSION,
    bill_report,
    categories_catalog,
    correct_category,
    resolve_period,
)

__all__ = [
    "BASES",
    "CATEGORIES",
    "CATEGORY_LABELS",
    "CLASSIFIER_VERSION",
    "PERIOD_KINDS",
    "REPORT_VERSION",
    "THRESHOLD_VERSION",
    "TOOL_HANDLERS",
    "anomaly_summary",
    "bill_report",
    "bill_report_handler",
    "categories_catalog",
    "classify",
    "correct_category",
    "detect_anomalies",
    "evaluate_against_labels",
    "explain_classification",
    "label_for",
    "resolve_period",
]


def _operation_args(operation: Any) -> dict[str, Any]:
    """``Operation``、``{"typed_args": {...}}`` 包装或扁平 dict → 参数视图。"""
    if operation is None:
        return {}
    if isinstance(operation, Mapping):
        nested = operation.get("typed_args")
        args: dict[str, Any] = dict(nested) if isinstance(nested, Mapping) else {}
        args.update({key: value for key, value in operation.items() if key != "typed_args"})
        return args
    args = {}
    typed = getattr(operation, "typed_args", None)
    if isinstance(typed, Mapping):
        args.update(typed)
    for name in ("tool", "operation_id", "task_id"):
        value = getattr(operation, name, None)
        if value is not None and name not in args:
            args[name] = value
    return args


def bill_report_handler(session: Any, ctx: Any, operation: Any) -> dict[str, Any]:
    """``bill.report`` 领域处理器：只读，无资金效果。

    未指定账期时默认当月（按 Asia/Shanghai 业务日），避免模型漏填参数后报错。
    """
    args = _operation_args(operation)
    period = args.get("period")
    if not period:
        today = business_day(utcnow())
        period = {"kind": "month", "anchor": f"{today.year:04d}-{today.month:02d}"}
    basis = str(args.get("basis") or "both")
    report = bill_report(session, ctx, period=period, basis=basis)
    return {
        "type": "bill_report",
        "data": report,
        "source": "billing",
        "version": REPORT_VERSION,
        "warnings": report["warnings"],
    }


#: 领域处理器注册表（只含只读、天然幂等的工具）
TOOL_HANDLERS = {"bill.report": bill_report_handler}