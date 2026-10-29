"""Error taxonomy aligned with BANKAGENT_SRS_V5 §16.2.

The envelope *is* the business answer: an HTTP 200 carrying ``status=error``
must never be read as a successful risk report.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class QuantError(Exception):
    code = "INTERNAL"
    http_status = 500
    retry_mode = "no_retry"

    def __init__(self, safe_message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.detail = detail or {}

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "safe_message": self.safe_message,
            "retry_mode": self.retry_mode,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


class InvalidRequest(QuantError):
    code = "INVALID_REQUEST"
    http_status = 400
    retry_mode = "fix_request"


class Unauthorized(QuantError):
    code = "UNAUTHORIZED"
    http_status = 403
    retry_mode = "no_retry"


class PayloadTooLarge(QuantError):
    code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    retry_mode = "fix_request"


class RateLimited(QuantError):
    code = "RATE_LIMITED"
    http_status = 429
    retry_mode = "backoff_retry"


class UnprocessableData(QuantError):
    code = "UNPROCESSABLE_DATA"
    http_status = 422
    retry_mode = "fix_request"


class EngineUnavailable(QuantError):
    code = "ENGINE_UNAVAILABLE"
    http_status = 503
    retry_mode = "backoff_retry"