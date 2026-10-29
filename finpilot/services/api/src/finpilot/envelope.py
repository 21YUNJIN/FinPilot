"""统一返回信封（V5 16.2）。

HTTP 200 不等于业务成功；调用方必须读取 status / error 判断。
provenance 标明适配器与观测时间，便于区分模拟、离线重放与在线运行。
"""

from __future__ import annotations

from typing import Any

from . import ids
from .errors import FinPilotError
from .timeutil import format_iso, utcnow

SCHEMA_VERSION = "1.0"

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"


def envelope(
    *,
    data: Any = None,
    status: str = STATUS_OK,
    request_id: str | None = None,
    task_id: str | None = None,
    operation_id: str | None = None,
    error: dict[str, Any] | None = None,
    adapter: str = "finpilot-api",
    extra_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "adapter": adapter,
        "observed_at": format_iso(utcnow()),
        "schema_version": SCHEMA_VERSION,
    }
    if extra_provenance:
        provenance.update(extra_provenance)
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id or ids.new_id("req"),
        "task_id": task_id,
        "operation_id": operation_id,
        "status": status,
        "data": data,
        "error": error,
        "provenance": provenance,
    }


def error_envelope(
    exc: FinPilotError,
    *,
    request_id: str | None = None,
    task_id: str | None = None,
    operation_id: str | None = None,
    adapter: str = "finpilot-api",
) -> dict[str, Any]:
    status = STATUS_FAILED
    if exc.code in {"PROVIDER_TIMEOUT", "RESULT_UNKNOWN"}:
        status = STATUS_UNKNOWN
    elif exc.code in {"POLICY_DENIED", "GRANT_REQUIRED", "FORBIDDEN", "CAPABILITY_NOT_SUPPORTED", "RISK_MISMATCH"}:
        status = STATUS_REJECTED
    return envelope(
        data=None,
        status=status,
        request_id=request_id,
        task_id=task_id,
        operation_id=operation_id,
        error=exc.to_error_object(),
        adapter=adapter,
    )


def ok(data: Any, **kwargs: Any) -> dict[str, Any]:
    return envelope(data=data, status=STATUS_OK, **kwargs)


def is_success(payload: dict[str, Any]) -> bool:
    return payload.get("status") == STATUS_OK and payload.get("error") is None