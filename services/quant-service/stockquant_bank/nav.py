"""NAV series parsing and data-quality checks.

A wrong NAV series is worse than a missing one: it produces a confident risk
number for the wrong product.  Hard problems therefore raise, soft problems
become explicit warnings that travel with the report.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .canonical import iso_now, normalise_nav_series, series_source_hash
from .errors import UnprocessableData

MAX_POINTS_PER_SERIES = 5000
MAX_GAP_DAYS_WARN = 10
STALE_LAG_DAYS_WARN = 7


def _parse_date(text: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(text).strip())
    except ValueError as exc:
        raise UnprocessableData(
            "净值日期必须是 YYYY-MM-DD",
            detail={"field": "date", "value": str(text)[:40]},
        ) from exc


@dataclass
class NavSeries:
    product_id: str
    dates: List[str]
    values: List[float]
    source_hash: str
    quality: Dict[str, Any] = field(default_factory=dict)

    @property
    def points(self) -> List[Dict[str, Any]]:
        return [{"date": d, "value": v} for d, v in zip(self.dates, self.values)]

    @property
    def start(self) -> str:
        return self.dates[0]

    @property
    def end(self) -> str:
        return self.dates[-1]


def validate_series(
    product_id: str,
    raw_points: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> NavSeries:
    if not product_id or not str(product_id).strip():
        raise UnprocessableData("product_id 不能为空", detail={"field": "product_id"})
    if not raw_points:
        raise UnprocessableData(
            "缺少净值序列",
            detail={"field": "nav", "product_id": product_id},
        )
    if len(raw_points) > MAX_POINTS_PER_SERIES:
        raise UnprocessableData(
            "净值序列超过服务上限",
            detail={"field": "nav", "product_id": product_id, "max_points": MAX_POINTS_PER_SERIES},
        )

    as_of_date = _parse_date(as_of)
    rows = normalise_nav_series(raw_points)

    seen: Dict[str, float] = {}
    duplicates: List[str] = []
    for row in rows:
        _parse_date(row["date"])
        if row["date"] in seen:
            duplicates.append(row["date"])
        seen[row["date"]] = row["value"]
    if duplicates:
        raise UnprocessableData(
            "同一日期出现多条净值，无法判断采用哪一条",
            detail={"field": "nav", "product_id": product_id, "duplicate_dates": sorted(set(duplicates))[:10]},
        )

    bad_values = [row["date"] for row in rows if not (row["value"] > 0)]
    if bad_values:
        raise UnprocessableData(
            "净值必须为正数；缺失应留空而不是填 0",
            detail={"field": "nav", "product_id": product_id, "bad_dates": bad_values[:10]},
        )

    last_date = _parse_date(rows[-1]["date"])
    if last_date > as_of_date:
        raise UnprocessableData(
            "净值序列包含 as_of 之后的数据，拒绝用未来数据计算",
            detail={"field": "nav", "product_id": product_id, "last_date": rows[-1]["date"], "as_of": as_of},
        )

    warnings: List[str] = []
    lag_days = (as_of_date - last_date).days
    if lag_days > STALE_LAG_DAYS_WARN:
        warnings.append(f"最新净值日期 {rows[-1]['date']} 距 as_of {lag_days} 天，可能不是最新披露")

    gaps: List[Dict[str, Any]] = []
    previous: Optional[_dt.date] = None
    for row in rows:
        current = _parse_date(row["date"])
        if previous is not None:
            span = (current - previous).days
            if span == 0:
                continue
            if span > MAX_GAP_DAYS_WARN:
                gaps.append({"from": previous.isoformat(), "to": current.isoformat(), "days": span})
        previous = current
    if gaps:
        warnings.append(f"存在 {len(gaps)} 段超过 {MAX_GAP_DAYS_WARN} 天的净值缺口，收益按可得点计算")

    quality = {
        "points": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "as_of": as_of,
        "stale_lag_days": lag_days,
        "gaps": gaps,
        "warnings": warnings,
    }
    return NavSeries(
        product_id=str(product_id),
        dates=[row["date"] for row in rows],
        values=[float(row["value"]) for row in rows],
        source_hash=series_source_hash(rows),
        quality=quality,
    )


def describe_window(series: NavSeries) -> Dict[str, Any]:
    return {
        "start": series.start,
        "end": series.end,
        "points": len(series.dates),
        "computed_at": iso_now(),
    }