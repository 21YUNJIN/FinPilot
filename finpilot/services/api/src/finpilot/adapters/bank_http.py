"""Independent bank sandbox adapter; no real bank connectivity is implied."""
from __future__ import annotations

from urllib.parse import quote
import httpx

from ..config import get_settings
from ..errors import FinPilotError
from ..kernel.provider_adapter import ProviderResult, event_from_result


class HttpBankAdapter:
    provider_id = "bank_sandbox_v1"

    def __init__(self, base_url: str | None = None, *, client=None, timeout: float = 5.0):
        self.base_url = (base_url or get_settings().bank_sandbox_base_url).rstrip("/")
        self.client = client or httpx.Client(base_url=self.base_url, timeout=timeout, follow_redirects=False)

    def close(self):
        self.client.close()

    def capabilities(self):
        return {"stable_key": True, "query": True, "cancel": True,
                "async_callback": True, "provider": "http_sandbox", "base_url": self.base_url}

    def _request(self, method: str, path: str, payload=None):
        try:
            response = self.client.request(method, path, json=payload)
        except httpx.ConnectError as exc:
            raise FinPilotError("PROVIDER_UNAVAILABLE", "银行模拟服务未连接。") from exc
        except httpx.HTTPError as exc:
            # A write may already have been accepted; reconcile using the same stable key.
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务结果未知，请按原编号查询。") from exc
        if response.status_code in (408, 504):
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务结果未知，请按原编号查询。")
        if response.status_code >= 500:
            # A server can persist the request before returning 5xx: do not assume no effect.
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务返回服务错误，结果未知，请按原编号查询。")
        try:
            body = response.json()
        except ValueError as exc:
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务响应无法核验，结果未知。") from exc
        if response.status_code >= 400:
            code = "IDEMPOTENCY_CONFLICT" if response.status_code == 409 else "PROVIDER_REJECTED"
            raise FinPilotError(code, "银行模拟服务拒绝请求。")
        if not isinstance(body, dict) or not body.get("provider_ref") or not body.get("status"):
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务响应缺少状态或编号。")
        return body

    def _result(self, body, stable_operation_ref):
        if body.get("stable_operation_ref") != stable_operation_ref:
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务响应编号不匹配，结果未知。")
        status = str(body["status"])
        if status not in {"accepted", "pending", "succeeded", "failed", "cancelled", "unknown"}:
            raise FinPilotError("PROVIDER_TIMEOUT", "银行模拟服务状态无法识别，结果未知。")
        result = ProviderResult(provider_ref=str(body["provider_ref"]), status=status,
                                raw=body, capabilities=self.capabilities())
        if status in {"succeeded", "failed", "cancelled"}:
            result.events = [event_from_result(result, operation_id=stable_operation_ref,
                event_type=f"transfer.{status}", event_version=int(body.get("last_event_version") or 1))]
        return result

    def submit(self, *, stable_operation_ref: str, payload: dict):
        if payload.get("tool") != "transfer.submit":
            raise FinPilotError("PROVIDER_CAPABILITY_MISSING", "银行模拟适配器仅支持转账。")
        args = payload.get("typed_args") or {}
        body = {"stable_operation_ref": stable_operation_ref,
                "owner_user_id": payload.get("owner_user_id"),
                "payee_ref": args.get("payee_id"), "amount_minor": args.get("amount_minor"),
                "fee_minor": args.get("fee_minor", 0), "currency": args.get("currency", "CNY")}
        return self._result(self._request("POST", "/bank/transfers", body), stable_operation_ref)

    def query(self, *, stable_operation_ref: str):
        path = "/bank/transfers/" + quote(stable_operation_ref, safe="")
        return self._result(self._request("GET", path), stable_operation_ref)

    def cancel(self, *, stable_operation_ref: str):
        path = "/bank/transfers/" + quote(stable_operation_ref, safe="") + "/cancel"
        return self._result(self._request("POST", path), stable_operation_ref)
