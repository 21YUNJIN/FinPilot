"""Canonical serialisation, stable hashing and time/id helpers.

Every number that leaves this service is accompanied by a hash of the exact
input it was computed from.  The hash must be reproducible by the caller, so
the canonical form is fixed here and covered by tests.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

BUSINESS_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, fixed offset (no tzdata dependency)
METHOD_VERSION = "sq-wealth-risk/1.0.0"
SCHEMA_VERSION = "1.0"


def now_business() -> datetime:
    return datetime.now(tz=BUSINESS_TZ)


def iso_now() -> str:
    return now_business().isoformat(timespec="seconds")


def iso_date(value: datetime) -> str:
    return value.date().isoformat()


def new_request_id() -> str:
    return "req_sq_" + secrets.token_hex(8)


def canonical_json(value: Any) -> str:
    """Stable JSON used for hashing: sorted keys, no whitespace, UTF-8 kept."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def _round(value: float, digits: int) -> float:
    return round(float(value), digits)


def normalise_nav_series(points: Iterable[Mapping[str, Any]]) -> list:
    """Return the hashing view of a NAV series: date + value rounded to 6 dp.

    Rounding happens before hashing so that a caller sending ``1.0`` and
    ``1.000000`` produces the same hash, while a genuinely different value
    changes it.
    """
    rows = []
    for point in points:
        rows.append({
            "date": str(point["date"]).strip(),
            "value": _round(float(point["value"]), 6),
        })
    rows.sort(key=lambda row: row["date"])
    return rows


def series_source_hash(points: Sequence[Mapping[str, Any]]) -> str:
    return sha256_json(normalise_nav_series(points))
