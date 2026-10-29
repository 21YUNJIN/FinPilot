"""账单报表服务（V5 第 10 节 S2-01～S2-04、T06／T07／T08／T43／T50）。

契约要点：

- 账期三种：自然月 ``{"kind":"month","anchor":"2026-10"}``、自然年
  ``{"kind":"year","anchor":"2026"}``、自选区间
  ``{"kind":"custom","start":...,"end":...}``（**起点闭、终点开**）。
  业务日一律用 Asia/Shanghai 换算，边界在时区上正确（含跨年与空月份）。
- ``basis``：``consumption``（消费分析）／``cashflow``（现金流）／``both``。
- 每个汇总数字都带 ``source_ids``（bt_id 列表）与 ``formula``；百分比分母为 0
  时返回 ``None`` + ``status="not_applicable"``，绝不返回无穷大。
- 口径：自己账户互转在现金流入可见、合并消费中排除；理财申购／赎回是资产调拨，
  既不算消费也不算收入；手续费单列；跨期退款"本期现金流记退款入账 + 消费报告另列
  前期消费退款调整"，不静默改写已冻结报告；无法关联原交易的退款标
  ``pending_verification``。
- 报告带 ``as_of``（数据截至时刻）、``warnings``、``timezone``、``period_label``。
- 分类纠正是**叠加层**：``correct_category`` 只写 ``CategoryCorrection``，
  原 ``BillingTransaction.category`` 不变；报告读取时应用并标注 ``corrected: true``。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...ids import new_id
from ...models import BillingTransaction, CategoryCorrection, Operation
from ...money import CURRENCY
from ...timeutil import SHANGHAI, UTC, business_day, format_iso, parse_iso, to_utc, utcnow
from . import anomalies as anomaly_rules
from . import classification as cls

REPORT_VERSION = "bill-report-2026.10"
BASES: tuple[str, ...] = ("consumption", "cashflow", "both")
PERIOD_KINDS: tuple[str, ...] = ("month", "year", "custom")

#: 异常基线回溯窗口（用于"显著偏离个人历史"）。
HISTORY_LOOKBACK_DAYS = 365

#: 用于识别"同一商户跨账期边界重复扣费"的上下文回溯窗口。
CONTEXT_LOOKBACK = timedelta(hours=24)

_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

#: Operation 未终结的状态：出现在账单里的这类交易必须单列。
INCOMPLETE_OPERATION_STATUSES = ("created", "awaiting_authorization", "authorized", "dispatching", "pending", "unknown")


# ---------------------------------------------------------------------------
# 账期
# ---------------------------------------------------------------------------


def _parse_month_anchor(anchor: str) -> tuple[int, int]:
    match = _MONTH_RE.match(str(anchor).strip())
    if not match:
        raise FinPilotError(
            "PARAM_INVALID",
            "月份账期格式应为 YYYY-MM。",
            details={"reason_codes": ["bad_month_anchor"], "anchor": str(anchor)},
        )
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise FinPilotError(
            "PARAM_INVALID",
            "月份必须在 1—12 之间。",
            details={"reason_codes": ["bad_month_anchor"], "anchor": str(anchor)},
        )
    return year, month


def _parse_year_anchor(anchor: str) -> int:
    match = _YEAR_RE.match(str(anchor).strip())
    if not match:
        raise FinPilotError(
            "PARAM_INVALID",
            "年份账期格式应为 YYYY。",
            details={"reason_codes": ["bad_year_anchor"], "anchor": str(anchor)},
        )
    return int(match.group(1))


def _local_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start, end) 的上海本地零点换算为 UTC 瞬时。"""
    start_local = datetime.combine(start, time(0, 0, 0), tzinfo=SHANGHAI)
    end_local = datetime.combine(end, time(0, 0, 0), tzinfo=SHANGHAI)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _shift_year(value: date, delta: int) -> date:
    try:
        return value.replace(year=value.year + delta)
    except ValueError:  # 2 月 29 日
        return value.replace(year=value.year + delta, day=28)


def _period_bounds(kind: str, start: date, end: date) -> dict[str, Any]:
    start_utc, end_utc = _local_bounds(start, end)
    return {
        "start_business_day": start,
        "end_business_day_exclusive": end,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "iso_start": format_iso(start_utc),
        "iso_end": format_iso(end_utc),
    }


def resolve_period(period: Mapping[str, Any]) -> dict[str, Any]:
    """把账期描述解析为 UTC 边界 + 上海业务日边界 + 上年同期账期。"""
    if not isinstance(period, Mapping):
        raise FinPilotError("PARAM_INVALID", "period 必须是对象。")
    kind = str(period.get("kind", "") or "").strip()
    if kind not in PERIOD_KINDS:
        raise FinPilotError(
            "PARAM_INVALID",
            "period.kind 只能是 month / year / custom。",
            details={"reason_codes": ["bad_period_kind"], "kind": kind},
        )

    if kind == "month":
        anchor = period.get("anchor")
        if anchor is None:
            raise FinPilotError("PARAM_INVALID", "月报需要 anchor（YYYY-MM）。")
        year, month = _parse_month_anchor(str(anchor))
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        previous_start = _shift_year(start, -1)
        previous_end = _shift_year(end, -1)
        label = f"{year}年{month}月"
        previous_label = f"{year - 1}年{month}月"
    elif kind == "year":
        anchor = period.get("anchor")
        if anchor is None:
            raise FinPilotError("PARAM_INVALID", "年报需要 anchor（YYYY）。")
        year = _parse_year_anchor(str(anchor))
        start, end = date(year, 1, 1), date(year + 1, 1, 1)
        previous_start, previous_end = _shift_year(start, -1), _shift_year(end, -1)
        label = f"{year}年"
        previous_label = f"{year - 1}年"
    else:
        raw_start, raw_end = period.get("start"), period.get("end")
        if not raw_start or not raw_end:
            raise FinPilotError("PARAM_INVALID", "自选账期需要 start 与 end。")
        start_utc = _parse_iso_boundary(raw_start, "start")
        end_utc = _parse_iso_boundary(raw_end, "end")
        if end_utc <= start_utc:
            raise FinPilotError(
                "PARAM_INVALID",
                "自选账期要求起点闭、终点开且终点晚于起点。",
                details={"reason_codes": ["empty_or_reversed_range"]},
            )
        local_start = start_utc.astimezone(SHANGHAI)
        local_end = end_utc.astimezone(SHANGHAI)
        previous_start_utc = start_utc.replace(year=start_utc.year - 1) if start_utc.month != 2 or start_utc.day != 29 else start_utc.replace(year=start_utc.year - 1, day=28)
        previous_end_utc = end_utc.replace(year=end_utc.year - 1) if end_utc.month != 2 or end_utc.day != 29 else end_utc.replace(year=end_utc.year - 1, day=28)
        return {
            "kind": kind,
            "label": f"{local_start.date().isoformat()} 至 {local_end.date().isoformat()}（起点闭、终点开）",
            "timezone": "Asia/Shanghai",
            "start_utc": start_utc,
            "end_utc": end_utc,
            "start_business_day": local_start.date(),
            "end_business_day_exclusive": local_end.date(),
            "iso_start": format_iso(start_utc),
            "iso_end": format_iso(end_utc),
            "previous_year": {
                "kind": "custom",
                "label": f"{previous_start_utc.astimezone(SHANGHAI).date().isoformat()} 至 "
                f"{previous_end_utc.astimezone(SHANGHAI).date().isoformat()}（上年同期）",
                "timezone": "Asia/Shanghai",
                "start_utc": previous_start_utc,
                "end_utc": previous_end_utc,
                "start_business_day": previous_start_utc.astimezone(SHANGHAI).date(),
                "end_business_day_exclusive": previous_end_utc.astimezone(SHANGHAI).date(),
                "iso_start": format_iso(previous_start_utc),
                "iso_end": format_iso(previous_end_utc),
            },
        }

    bounds = _period_bounds(kind, start, end)
    previous_bounds = _period_bounds(kind, previous_start, previous_end)
    return {
        "kind": kind,
        "anchor": period.get("anchor"),
        "label": label,
        "timezone": "Asia/Shanghai",
        **bounds,
        "previous_year": {
            "kind": kind,
            "anchor": previous_start.year if kind == "year" else f"{previous_start.year:04d}-{previous_start.month:02d}",
            "label": previous_label,
            "timezone": "Asia/Shanghai",
            **previous_bounds,
        },
    }


def _parse_iso_boundary(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise FinPilotError("PARAM_INVALID", f"period.{field} 必须带时区偏移。")
        return to_utc(value)
    try:
        return parse_iso(str(value))
    except ValueError as exc:
        raise FinPilotError(
            "PARAM_INVALID",
            f"period.{field} 必须是带时区的 ISO 8601 时间。",
            details={"reason_codes": ["bad_iso_boundary"], "field": field},
        ) from exc


# ---------------------------------------------------------------------------
# 取数与归一化
# ---------------------------------------------------------------------------


def _fetch_corrections(session: Session, user_id: str) -> dict[str, CategoryCorrection]:
    rows = session.execute(
        select(CategoryCorrection)
        .where(CategoryCorrection.owner_user_id == user_id)
        .order_by(CategoryCorrection.created_at.asc(), CategoryCorrection.correction_id.asc())
    ).scalars()
    latest: dict[str, CategoryCorrection] = {}
    for correction in rows:
        latest[correction.bt_id] = correction  # 后写覆盖先写 = 最新纠正生效
    return latest


def _fetch_rows(session: Session, user_id: str, start: datetime, end: datetime) -> list[BillingTransaction]:
    return list(
        session.execute(
            select(BillingTransaction)
            .where(
                BillingTransaction.owner_user_id == user_id,
                BillingTransaction.posted_at >= start,
                BillingTransaction.posted_at < end,
            )
            .order_by(BillingTransaction.posted_at.asc(), BillingTransaction.bt_id.asc())
        ).scalars()
    )


def _normalize(row: BillingTransaction, corrections: Mapping[str, CategoryCorrection]) -> dict[str, Any]:
    kind = cls.normalize_kind(row.kind)
    base_category, rule_id, confidence = cls.classify(row)
    correction = corrections.get(row.bt_id)
    if correction is not None and cls.is_known_category(correction.to_category):
        category = correction.to_category
        rule_id = cls.CORRECTION_RULE
        confidence = 1.0
        corrected = True
    else:
        category = base_category
        corrected = False
    computed_day = business_day(row.posted_at)
    return {
        "bt_id": row.bt_id,
        "account_id": row.account_id,
        "operation_id": row.operation_id,
        "posted_at": to_utc(row.posted_at),
        "business_day": computed_day,
        "stored_business_day": row.business_day,
        "business_day_matches": row.business_day == computed_day,
        "amount_minor": cls.magnitude_minor(row),
        "signed_minor": cls.signed_minor(row),
        "direction": cls.direction_of(row),
        "kind": kind,
        "raw_kind": cls.raw_kind(row.kind),
        "category": category,
        "base_category": base_category,
        "category_rule_id": rule_id,
        "category_confidence": confidence,
        "category_source": "correction" if corrected else ("stored" if rule_id == cls.STORED_CATEGORY_RULE else "rule"),
        "corrected": corrected,
        "merchant_name": row.merchant_name,
        "counterparty": row.counterparty,
        "memo": row.memo,
        "linked_bt_id": row.linked_bt_id,
        "anomaly_label": row.anomaly_label,
        "source": row.source,
        "legs": {
            "internal_transfer": cls.is_internal_transfer(kind),
            "asset_allocation": cls.is_asset_allocation(kind),
            "fee": cls.is_fee(kind),
            "refund": cls.is_refund_row(row),
            "incomplete": cls.is_incomplete(kind),
            "consumption": cls.is_consumption_candidate(row),
            "inflow": cls.direction_of(row) == "in",
            "outflow": cls.direction_of(row) == "out",
        },
    }


def _in_range(row: Mapping[str, Any], period: Mapping[str, Any]) -> bool:
    return period["start_utc"] <= row["posted_at"] < period["end_utc"]


# ---------------------------------------------------------------------------
# 指标构造
# ---------------------------------------------------------------------------


def _metric(
    value: Any,
    *,
    unit: str,
    formula: str,
    source_ids: Sequence[str],
    status: str = "ok",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "value": value,
        "unit": unit,
        "formula": formula,
        "source_ids": list(source_ids),
        "status": status,
    }
    payload.update(extra)
    return payload


def _percent_metric(
    numerator: int,
    denominator: int,
    *,
    formula: str,
    source_ids: Sequence[str],
    reason_code: str = "zero_denominator",
) -> dict[str, Any]:
    if denominator == 0:
        return _metric(
            None,
            unit="percent",
            formula=formula,
            source_ids=source_ids,
            status="not_applicable",
            reason_code=reason_code,
            note="分母为零，百分比不适用；不返回无穷大。",
        )
    return _metric(round(numerator / denominator * 100, 4), unit="percent", formula=formula, source_ids=source_ids)


# ---------------------------------------------------------------------------
# 消费分析
# ---------------------------------------------------------------------------


def _resolve_refunds(
    period_rows: Sequence[Mapping[str, Any]],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    period: Mapping[str, Any],
) -> dict[str, Any]:
    """把退款分成：同期冲减 / 跨期调整 / 待核实。不做启发式关联。"""
    same_period: list[dict[str, Any]] = []
    prior_period: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for row in period_rows:
        if not row["legs"]["refund"] or row["direction"] != "in":
            continue
        origin_id = row["linked_bt_id"]
        item: dict[str, Any] = {
            "bt_id": row["bt_id"],
            "amount_minor": row["amount_minor"],
            "linked_bt_id": origin_id,
            "posted_at": format_iso(row["posted_at"]),
            "merchant_name": row["merchant_name"],
        }
        if not origin_id:
            item["reason_code"] = "origin_not_linked"
            item["status"] = "pending_verification"
            item["note"] = "退款未关联原交易，标记待核实；不进入任何合计。"
            pending.append(item)
            continue
        origin = rows_by_id.get(origin_id)
        if origin is None:
            item["reason_code"] = "origin_not_found"
            item["status"] = "pending_verification"
            item["note"] = "关联的原交易不属于当前用户或不存在，标记待核实。"
            pending.append(item)
            continue
        if not cls.is_consumption_candidate(origin):
            item["reason_code"] = "origin_not_consumption"
            item["status"] = "pending_verification"
            item["note"] = "原交易不是消费（互转／资产调拨／手续费），不进消费调整。"
            pending.append(item)
            continue
        item["origin_bt_id"] = origin["bt_id"]
        item["origin_business_day"] = origin["business_day"].isoformat()
        item["category"] = origin["category"]
        item["category_label"] = cls.label_for(origin["category"])
        item["origin_amount_minor"] = origin["amount_minor"]
        if period["start_utc"] <= origin["posted_at"] < period["end_utc"]:
            item["scope"] = "same_period"
            same_period.append(item)
        elif origin["posted_at"] < period["start_utc"]:
            item["scope"] = "prior_period"
            item["note"] = "本期现金流记退款入账；消费报告另列前期消费退款调整，不改写已冻结报告。"
            prior_period.append(item)
        else:
            item["reason_code"] = "origin_after_period"
            item["status"] = "pending_verification"
            item["note"] = "关联的原交易晚于本期，口径无法确定，标记待核实。"
            pending.append(item)

    return {"same_period": same_period, "prior_period": prior_period, "pending": pending}


def _consumption_section(
    period_rows: Sequence[Mapping[str, Any]],
    previous_rows: Sequence[Mapping[str, Any]],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    period: Mapping[str, Any],
) -> dict[str, Any]:
    refunds = _resolve_refunds(period_rows, rows_by_id, period)

    consumption_rows = [row for row in period_rows if row["legs"]["consumption"]]
    refund_by_category: dict[str, int] = defaultdict(int)
    same_period_refund_ids: list[str] = []
    for item in refunds["same_period"]:
        refund_by_category[item["category"]] += item["amount_minor"]
        same_period_refund_ids.append(item["bt_id"])

    gross_total = sum(row["amount_minor"] for row in consumption_rows)
    same_period_refund_total = sum(item["amount_minor"] for item in refunds["same_period"])
    prior_period_adjustment_total = sum(item["amount_minor"] for item in refunds["prior_period"])

    categories: list[dict[str, Any]] = []
    for category in cls.CATEGORIES:
        rows = [row for row in consumption_rows if row["category"] == category]
        gross = sum(row["amount_minor"] for row in rows)
        refund = refund_by_category.get(category, 0)
        net = gross - refund
        source_ids = [row["bt_id"] for row in rows] + [
            item["bt_id"] for item in refunds["same_period"] if item["category"] == category
        ]
        if not rows and not refund:
            categories.append(
                {
                    "category": category,
                    "label": cls.label_for(category),
                    "amount_minor": _metric(0, unit="minor", formula=f"SUM(amount_minor) WHERE category='{category}'",
                                            source_ids=[]),
                    "same_period_refund_minor": _metric(
                        0, unit="minor",
                        formula="SUM(refund.amount_minor) WHERE origin.category='%s'" % category, source_ids=[]),
                    "net_amount_minor": _metric(
                        0, unit="minor",
                        formula=f"category_gross[{category}] - category_same_period_refund[{category}]", source_ids=[]),
                    "share_percent": _percent_metric(
                        0, gross_total, formula=f"net_amount_minor['{category}'] / total_consumption_gross * 100",
                        source_ids=[], reason_code="zero_denominator"),
                }
            )
            continue
        categories.append(
            {
                "category": category,
                "label": cls.label_for(category),
                "amount_minor": _metric(
                    gross,
                    unit="minor",
                    formula=f"SUM(amount_minor) WHERE direction='out' AND category='{category}' "
                            "AND NOT internal_transfer AND NOT asset_allocation AND NOT fee AND NOT refund",
                    source_ids=[row["bt_id"] for row in rows],
                ),
                "same_period_refund_minor": _metric(
                    refund,
                    unit="minor",
                    formula=f"SUM(refund.amount_minor) WHERE refund.origin.category='{category}' AND origin IN period",
                    source_ids=[item["bt_id"] for item in refunds["same_period"] if item["category"] == category],
                ),
                "net_amount_minor": _metric(
                    net,
                    unit="minor",
                    formula=f"category_gross['{category}'] - category_same_period_refund['{category}']",
                    source_ids=source_ids,
                ),
                "share_percent": _percent_metric(
                    net,
                    gross_total,
                    formula=f"net_amount_minor['{category}'] / total_consumption_gross * 100",
                    source_ids=source_ids,
                    reason_code="no_consumption_in_period",
                ),
            }
        )

    net_after_same_period = gross_total - same_period_refund_total
    net_consumption = net_after_same_period - prior_period_adjustment_total

    previous_consumption_rows = [row for row in previous_rows if row["legs"]["consumption"]]
    previous_refunds = _resolve_refunds(previous_rows, rows_by_id, period["previous_year"])
    previous_refund_total = sum(item["amount_minor"] for item in previous_refunds["same_period"])
    previous_gross = sum(row["amount_minor"] for row in previous_consumption_rows)
    previous_net = previous_gross - previous_refund_total

    current_ids = [row["bt_id"] for row in consumption_rows]
    yoy_status = "ok" if previous_consumption_rows else "missing_data"
    yoy: dict[str, Any] = {
        "current_minor": _metric(
            gross_total,
            unit="minor",
            formula="SUM(amount_minor) WHERE consumption_leg",
            source_ids=current_ids,
        ),
        "previous_minor": _metric(
            previous_gross,
            unit="minor",
            formula="SUM(amount_minor) WHERE consumption_leg AND 上年同期账期",
            source_ids=[row["bt_id"] for row in previous_consumption_rows],
            status="ok" if previous_consumption_rows else "missing_data",
            reason_code=None if previous_consumption_rows else "missing_data",
        ),
        "change_minor": _metric(
            gross_total - previous_gross if previous_consumption_rows else None,
            unit="minor",
            formula="current_minor - previous_minor",
            source_ids=current_ids,
            status=yoy_status,
            reason_code=None if previous_consumption_rows else "missing_data",
        ),
        "change_percent": (
            _percent_metric(
                gross_total - previous_gross,
                previous_gross,
                formula="(current_minor - previous_minor) / previous_minor * 100",
                source_ids=current_ids,
            )
            if previous_consumption_rows
            else _metric(
                None,
                unit="percent",
                formula="(current_minor - previous_minor) / previous_minor * 100",
                source_ids=current_ids,
                status="missing_data",
                reason_code="missing_data",
                note="缺上年同期数据，无法计算同比。",
            )
        ),
        "status": yoy_status,
        "period_label": period["previous_year"]["label"],
    }

    excluded_rows = [row for row in period_rows if row["direction"] == "out" and not row["legs"]["consumption"]]
    fee_rows = [row for row in period_rows if row["legs"]["fee"]]
    internal_rows = [row for row in period_rows if row["legs"]["internal_transfer"] and row["direction"] == "out"]
    allocation_rows = [row for row in period_rows if row["legs"]["asset_allocation"] and row["direction"] == "out"]

    return {
        "total_consumption_gross_minor": _metric(
            gross_total,
            unit="minor",
            formula="SUM(amount_minor) WHERE direction='out' AND NOT internal_transfer AND NOT asset_allocation "
                    "AND NOT fee AND NOT refund",
            source_ids=current_ids,
        ),
        "same_period_refund_minor": _metric(
            same_period_refund_total,
            unit="minor",
            formula="SUM(refund.amount_minor) WHERE refund IN period AND origin IN period",
            source_ids=same_period_refund_ids,
        ),
        "prior_period_refund_adjustment_minor": _metric(
            prior_period_adjustment_total,
            unit="minor",
            formula="SUM(refund.amount_minor) WHERE refund IN period AND origin BEFORE period",
            source_ids=[item["bt_id"] for item in refunds["prior_period"]],
            note="单独列示，不静默改写上期已冻结的消费报告。",
        ),
        "net_consumption_minor": _metric(
            net_consumption,
            unit="minor",
            formula="total_consumption_gross - same_period_refund - prior_period_refund_adjustment",
            source_ids=current_ids + same_period_refund_ids + [item["bt_id"] for item in refunds["prior_period"]],
        ),
        "fee_minor": _metric(
            sum(row["amount_minor"] for row in fee_rows),
            unit="minor",
            formula="SUM(amount_minor) WHERE kind='fee'（手续费单列，不计入消费分类）",
            source_ids=[row["bt_id"] for row in fee_rows],
        ),
        "categories": categories,
        "same_period_refunds": refunds["same_period"],
        "prior_period_refund_adjustments": refunds["prior_period"],
        "pending_verification_refunds": refunds["pending"],
        "excluded_from_consumption": {
            "internal_transfer_out_minor": _metric(
                sum(row["amount_minor"] for row in internal_rows),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind='transfer_internal' AND direction='out'",
                source_ids=[row["bt_id"] for row in internal_rows],
                note="自己账户之间互转：在现金流入可见，合并消费中排除。",
            ),
            "asset_allocation_out_minor": _metric(
                sum(row["amount_minor"] for row in allocation_rows),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind LIKE 'wealth_%' AND direction='out'",
                source_ids=[row["bt_id"] for row in allocation_rows],
                note="理财申购是资产调拨，不是消费。",
            ),
            "fee_out_minor": _metric(
                sum(row["amount_minor"] for row in fee_rows),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind='fee'",
                source_ids=[row["bt_id"] for row in fee_rows],
            ),
            "source_ids": [row["bt_id"] for row in excluded_rows],
        },
        "year_over_year": yoy,
    }


# ---------------------------------------------------------------------------
# 现金流分析
# ---------------------------------------------------------------------------


def _cashflow_section(period_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    inflows = [row for row in period_rows if row["direction"] == "in"]
    outflows = [row for row in period_rows if row["direction"] == "out"]

    internal_in = [row for row in inflows if row["legs"]["internal_transfer"]]
    internal_out = [row for row in outflows if row["legs"]["internal_transfer"]]
    allocation_in = [row for row in inflows if row["legs"]["asset_allocation"]]
    allocation_out = [row for row in outflows if row["legs"]["asset_allocation"]]
    fee_out = [row for row in outflows if row["legs"]["fee"]]
    refund_in = [row for row in inflows if row["legs"]["refund"]]

    inflow_total = sum(row["amount_minor"] for row in inflows)
    outflow_total = sum(row["amount_minor"] for row in outflows)

    by_kind: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in period_rows:
        by_kind[(row["kind"], row["direction"])].append(row)

    by_category: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in outflows:
        by_category[row["category"]].append(row)

    return {
        "inflow_total_minor": _metric(
            inflow_total,
            unit="minor",
            formula="SUM(amount_minor) WHERE direction='in'",
            source_ids=[row["bt_id"] for row in inflows],
        ),
        "outflow_total_minor": _metric(
            outflow_total,
            unit="minor",
            formula="SUM(amount_minor) WHERE direction='out'",
            source_ids=[row["bt_id"] for row in outflows],
        ),
        "net_cashflow_minor": _metric(
            inflow_total - outflow_total,
            unit="minor",
            formula="inflow_total - outflow_total",
            source_ids=[row["bt_id"] for row in period_rows],
        ),
        "income_minor": _metric(
            inflow_total
            - sum(row["amount_minor"] for row in internal_in)
            - sum(row["amount_minor"] for row in allocation_in)
            - sum(row["amount_minor"] for row in refund_in),
            unit="minor",
            formula="inflow_total - internal_transfer_in - asset_allocation_in - refund_inflow"
                    "（理财赎回与内部互转不算收入）",
            source_ids=[row["bt_id"] for row in inflows
                        if not row["legs"]["internal_transfer"]
                        and not row["legs"]["asset_allocation"]
                        and not row["legs"]["refund"]],
        ),
        "external_outflow_minor": _metric(
            outflow_total
            - sum(row["amount_minor"] for row in internal_out)
            - sum(row["amount_minor"] for row in allocation_out),
            unit="minor",
            formula="outflow_total - internal_transfer_out - asset_allocation_out（含手续费）",
            source_ids=[row["bt_id"] for row in outflows
                        if not row["legs"]["internal_transfer"] and not row["legs"]["asset_allocation"]],
        ),
        "consumption_outflow_minor": _metric(
            sum(row["amount_minor"] for row in outflows if row["legs"]["consumption"]),
            unit="minor",
            formula="SUM(amount_minor) WHERE consumption_leg（与消费分析口径一致，另不含手续费）",
            source_ids=[row["bt_id"] for row in outflows if row["legs"]["consumption"]],
        ),
        "fee_outflow_minor": _metric(
            sum(row["amount_minor"] for row in fee_out),
            unit="minor",
            formula="SUM(amount_minor) WHERE kind='fee'（单列）",
            source_ids=[row["bt_id"] for row in fee_out],
        ),
        "refund_inflow_minor": _metric(
            sum(row["amount_minor"] for row in refund_in),
            unit="minor",
            formula="SUM(amount_minor) WHERE refund AND direction='in'（本期现金流记退款入账）",
            source_ids=[row["bt_id"] for row in refund_in],
        ),
        "internal_transfers": {
            "in_minor": _metric(
                sum(row["amount_minor"] for row in internal_in),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind='transfer_internal' AND direction='in'",
                source_ids=[row["bt_id"] for row in internal_in],
            ),
            "out_minor": _metric(
                sum(row["amount_minor"] for row in internal_out),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind='transfer_internal' AND direction='out'",
                source_ids=[row["bt_id"] for row in internal_out],
            ),
            "note": "自己账户之间互转在账户现金流可见，但用户合并消费中排除。",
        },
        "asset_allocation": {
            "in_minor": _metric(
                sum(row["amount_minor"] for row in allocation_in),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind LIKE 'wealth_%' AND direction='in'",
                source_ids=[row["bt_id"] for row in allocation_in],
            ),
            "out_minor": _metric(
                sum(row["amount_minor"] for row in allocation_out),
                unit="minor",
                formula="SUM(amount_minor) WHERE kind LIKE 'wealth_%' AND direction='out'",
                source_ids=[row["bt_id"] for row in allocation_out],
            ),
            "note": "理财申购／赎回是资产调拨：不算消费，也不算收入。",
        },
        "by_kind": [
            {
                "kind": kind,
                "direction": direction,
                "count": len(rows),
                "amount_minor": _metric(
                    sum(row["amount_minor"] for row in rows),
                    unit="minor",
                    formula=f"SUM(amount_minor) WHERE kind='{kind}' AND direction='{direction}'",
                    source_ids=[row["bt_id"] for row in rows],
                ),
            }
            for (kind, direction), rows in sorted(by_kind.items())
        ],
        "by_category": [
            {
                "category": category,
                "label": cls.label_for(category),
                "amount_minor": _metric(
                    sum(row["amount_minor"] for row in rows),
                    unit="minor",
                    formula=f"SUM(amount_minor) WHERE direction='out' AND category='{category}'",
                    source_ids=[row["bt_id"] for row in rows],
                ),
            }
            for category, rows in sorted(by_category.items(), key=lambda kv: cls.CATEGORY_INDEX.get(kv[0], 99))
        ],
    }


# ---------------------------------------------------------------------------
# 警告与未完成交易
# ---------------------------------------------------------------------------


def _incomplete_rows(
    session: Session, period_rows: Sequence[Mapping[str, Any]]
) -> tuple[list[Mapping[str, Any]], set[str]]:
    """未完成交易：kind 未终结，或关联的 Operation 尚未终结。"""
    by_kind = [row for row in period_rows if row["legs"]["incomplete"]]
    operation_ids = {row["operation_id"] for row in period_rows if row["operation_id"]}
    unfinished_operations: set[str] = set()
    if operation_ids:
        rows = session.execute(
            select(Operation.operation_id, Operation.status).where(Operation.operation_id.in_(sorted(operation_ids)))
        ).all()
        unfinished_operations = {
            operation_id for operation_id, status in rows if status in INCOMPLETE_OPERATION_STATUSES
        }
    by_operation = [row for row in period_rows if row["operation_id"] in unfinished_operations]
    merged: dict[str, Mapping[str, Any]] = {row["bt_id"]: row for row in by_kind}
    merged.update({row["bt_id"]: row for row in by_operation})
    return list(merged.values()), unfinished_operations


def _warnings(
    period_rows: Sequence[Mapping[str, Any]],
    all_rows: Sequence[Mapping[str, Any]],
    consumption: Mapping[str, Any] | None,
    pending_refunds: Sequence[Mapping[str, Any]],
    incomplete: Sequence[Mapping[str, Any]],
    unfinished_operations: set[str],
    period: Mapping[str, Any],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if not period_rows:
        warnings.append(
            {
                "code": "empty_period",
                "severity": "info",
                "message": f"{period['label']} 无流水；合计为零，百分比为 not_applicable。",
                "source_ids": [],
            }
        )
    if incomplete:
        warnings.append(
            {
                "code": "incomplete_transactions",
                "severity": "warning",
                "message": "本期存在尚未终结的交易（未完成或关联操作非终态），已在报告中单列。",
                "source_ids": [row["bt_id"] for row in incomplete],
                "operation_ids": sorted(unfinished_operations),
            }
        )
    if pending_refunds:
        warnings.append(
            {
                "code": "unverified_refunds",
                "severity": "warning",
                "message": "本期有无法关联原交易的退款，标记 pending_verification，未计入任何消费合计。",
                "source_ids": [item["bt_id"] for item in pending_refunds],
            }
        )
    mismatched = [row for row in all_rows if not row["business_day_matches"]]
    if mismatched:
        warnings.append(
            {
                "code": "business_day_mismatch",
                "severity": "warning",
                "message": "部分流水的存储业务日与按 Asia/Shanghai 换算的业务日不一致，报表按换算结果归期。",
                "source_ids": [row["bt_id"] for row in mismatched],
            }
        )
    if consumption is not None and consumption["year_over_year"]["status"] != "ok":
        warnings.append(
            {
                "code": "missing_data",
                "severity": "warning",
                "message": f"缺少上年同期（{period['previous_year']['label']}）消费数据，同比不适用。",
                "source_ids": [],
                "detail": {"comparison": "year_over_year", "period_label": period["previous_year"]["label"]},
            }
        )
    return warnings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def bill_report(
    session: Session,
    ctx: Any,
    *,
    period: Mapping[str, Any],
    basis: str = "both",
) -> dict[str, Any]:
    """生成账单报告（消费分析／现金流／两者）。只读，不产生任何资金效果。"""
    ctx.require_customer()
    if basis not in BASES:
        raise FinPilotError(
            "PARAM_INVALID",
            "basis 只能是 consumption / cashflow / both。",
            details={"reason_codes": ["bad_basis"], "basis": str(basis)},
        )

    resolved = resolve_period(period)
    previous = resolved["previous_year"]
    range_start = min(resolved["start_utc"], previous["start_utc"]) - timedelta(days=HISTORY_LOOKBACK_DAYS)
    range_end = max(resolved["end_utc"], previous["end_utc"])

    corrections = _fetch_corrections(session, ctx.user_id)
    raw_rows = _fetch_rows(session, ctx.user_id, range_start, range_end)
    rows = [_normalize(row, corrections) for row in raw_rows]
    rows_by_id = {row["bt_id"]: row for row in rows}

    # 退款关联的原交易可能落在取数窗口之外，按 ID 补齐（归属仍然过滤）。
    linked_ids = sorted(
        {row["linked_bt_id"] for row in rows if row["linked_bt_id"] and row["linked_bt_id"] not in rows_by_id}
    )
    if linked_ids:
        for chunk_start in range(0, len(linked_ids), 500):
            chunk = linked_ids[chunk_start : chunk_start + 500]
            extra = session.execute(
                select(BillingTransaction).where(
                    BillingTransaction.owner_user_id == ctx.user_id,
                    BillingTransaction.bt_id.in_(chunk),
                )
            ).scalars()
            for row in extra:
                normalized = _normalize(row, corrections)
                rows_by_id.setdefault(normalized["bt_id"], normalized)

    period_rows = [row for row in rows if _in_range(row, resolved)]
    previous_rows = [row for row in rows if _in_range(row, previous)]
    history_rows = [row for row in rows if row["posted_at"] < resolved["start_utc"]]
    context_rows = [row for row in history_rows if row["posted_at"] >= resolved["start_utc"] - CONTEXT_LOOKBACK]

    detected = anomaly_rules.detect_anomalies(period_rows, history_rows=history_rows, context_rows=context_rows)
    summary = anomaly_rules.anomaly_summary(period_rows, detected)

    incomplete_rows, unfinished_operations = _incomplete_rows(session, period_rows)

    consumption = (
        _consumption_section(period_rows, previous_rows, rows_by_id, resolved)
        if basis in ("consumption", "both")
        else None
    )
    cashflow = _cashflow_section(period_rows) if basis in ("cashflow", "both") else None

    pending_refunds = consumption["pending_verification_refunds"] if consumption else []
    warnings = _warnings(
        period_rows,
        rows,
        consumption,
        pending_refunds,
        incomplete_rows,
        unfinished_operations,
        resolved,
    )

    latest_posted = max((row["posted_at"] for row in rows), default=None)
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "classifier_version": cls.CLASSIFIER_VERSION,
        "basis": basis,
        "timezone": "Asia/Shanghai",
        "as_of": format_iso(utcnow()),
        "data_through": format_iso(latest_posted) if latest_posted else None,
        "period": {
            "kind": resolved["kind"],
            "label": resolved["label"],
            "anchor": resolved.get("anchor"),
            "start": resolved["iso_start"],
            "end": resolved["iso_end"],
            "start_business_day": resolved["start_business_day"].isoformat(),
            "end_business_day_exclusive": resolved["end_business_day_exclusive"].isoformat(),
            "boundary": "start_closed_end_open",
            "previous_year_period": {
                "label": previous["label"],
                "start": previous["iso_start"],
                "end": previous["iso_end"],
            },
        },
        "period_label": resolved["label"],
        "currency": CURRENCY,
        "account_ids": sorted({row["account_id"] for row in rows}),
        "transaction_count": len(period_rows),
        "source_ids": [row["bt_id"] for row in period_rows],
        "consumption": consumption,
        "cashflow": cashflow,
        "pending_items": {
            "incomplete_transactions": [
                {
                    "bt_id": row["bt_id"],
                    "kind": row["kind"],
                    "amount_minor": row["amount_minor"],
                    "direction": row["direction"],
                    "operation_id": row["operation_id"],
                    "reason_code": "kind_not_final" if row["legs"]["incomplete"] else "operation_not_final",
                }
                for row in incomplete_rows
            ],
            "pending_verification_refunds": list(pending_refunds),
            "note": "未完成交易与未核实退款单列；仍按实际已记账金额参与合计。",
        },
        "anomalies": detected,
        "anomaly_summary": summary,
        "corrections_applied": [
            {
                "bt_id": row["bt_id"],
                "corrected": True,
                "to_category": row["category"],
                "to_category_label": cls.label_for(row["category"]),
                "base_category": row["base_category"],
                "category_rule_id": row["category_rule_id"],
                "transaction_category_unchanged": True,
            }
            for row in period_rows
            if row["corrected"]
        ],
        "warnings": warnings,
        "empty": not period_rows,
    }
    return report


def correct_category(
    session: Session,
    ctx: Any,
    bt_id: str,
    to_category: str,
    reason: str = "",
) -> dict[str, Any]:
    """写入分类纠正记录；**不修改原流水分类**。

    报告读取时叠加最新纠正并标注 ``corrected: true``。
    """
    ctx.require_customer()
    if not cls.is_known_category(to_category):
        raise FinPilotError(
            "PARAM_INVALID",
            "目标分类不在允许列表内。",
            details={"reason_codes": ["unknown_category"], "allowed": list(cls.CATEGORIES)},
        )
    row = session.get(BillingTransaction, str(bt_id))
    if row is None or row.owner_user_id != ctx.user_id:
        # 不泄露他人对象是否存在
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该流水。")

    original_category = row.category
    if original_category == to_category:
        raise FinPilotError(
            "PARAM_INVALID",
            "目标分类与当前分类相同，无需纠正。",
            details={"reason_codes": ["no_change"], "category": to_category},
        )

    correction = CategoryCorrection(
        correction_id=new_id("cc"),
        bt_id=row.bt_id,
        owner_user_id=ctx.user_id,
        from_category=original_category,
        to_category=to_category,
        reason=(reason or "")[:255],
        created_at=utcnow(),
    )
    session.add(correction)
    session.flush()

    return {
        "correction_id": correction.correction_id,
        "bt_id": row.bt_id,
        "from_category": original_category,
        "from_category_label": cls.label_for(original_category),
        "to_category": to_category,
        "to_category_label": cls.label_for(to_category),
        "reason": correction.reason,
        "recorded_at": format_iso(correction.created_at),
        "transaction_category": row.category,
        "transaction_category_unchanged": True,
        "classifier_version": cls.CLASSIFIER_VERSION,
    }


def categories_catalog() -> list[dict[str, Any]]:
    """报表分类目录（供前端与工具目录共用）。"""
    return [{"category": name, "label": cls.label_for(name)} for name in cls.CATEGORIES]


__all__ = [
    "BASES",
    "HISTORY_LOOKBACK_DAYS",
    "PERIOD_KINDS",
    "REPORT_VERSION",
    "bill_report",
    "categories_catalog",
    "correct_category",
    "resolve_period",
]