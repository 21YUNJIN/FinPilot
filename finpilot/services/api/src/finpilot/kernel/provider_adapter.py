"""提供方适配器协议与注册表（V5 8.2、SYS-06、API-04）。

要求：适配器必须支持稳定业务键与结果查询；两者都不支持时关闭自动金融写入。
本地适配器用于确定性测试，HTTP 适配器对接独立沙箱服务进程，两者走同一代码路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..errors import FinPilotError


_STATUS_ALIASES = {
    # 成功类
    "succeeded": "succeeded",
    "success": "succeeded",
    "confirmed": "succeeded",
    "booked": "succeeded",
    "settled": "succeeded",
    "completed": "succeeded",
    "captured": "succeeded",
    # 失败类（业务方明确拒绝）
    "failed": "failed",
    "rejected": "failed",
    "declined": "failed",
    "error": "failed",
    "failure": "failed",
    # 在途类
    "accepted": "pending",
    "received": "pending",
    "processing": "pending",
    "created": "pending",
    "pending": "pending",
    "open": "pending",
    # 撤销类
    "cancelled": "cancelled",
    "canceled": "cancelled",
    # 未知类
    "unknown": "unknown",
    "timeout": "unknown",
    "unconfirmed": "unknown",
}


def normalize_status(status: str | None) -> str:
    """把不同提供方的状态词表归一到内核的 succeeded/failed/pending/cancelled/unknown。

    不同业务方用词不同（商户习惯 confirmed/booked，银行常用 settled/succeeded），
    结算与对账必须按语义归类，不能按字面值判断，也不能把未知当失败。
    """
    key = str(status or "unknown").strip().lower()
    return _STATUS_ALIASES.get(key, key)


@dataclass
class ProviderResult:
    provider_ref: str
    status: str  # 原始状态；判断终态请用 normalized_status / terminal
    raw: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)

    @property
    def normalized_status(self) -> str:
        return normalize_status(self.status)

    @property
    def terminal(self) -> bool:
        return self.normalized_status in ("succeeded", "failed", "cancelled")


@runtime_checkable
class ProviderAdapter(Protocol):
    provider_id: str

    def capabilities(self) -> dict: ...

    def submit(self, *, stable_operation_ref: str, payload: dict) -> ProviderResult: ...

    def query(self, *, stable_operation_ref: str) -> ProviderResult: ...

    def cancel(self, *, stable_operation_ref: str) -> ProviderResult: ...


_ADAPTERS: dict[str, ProviderAdapter] = {}


def default_provider_id(tool: str) -> str:
    """Select explicit local or independent simulated provider; never fall back silently."""
    from ..config import get_settings
    mode = get_settings().provider_mode
    if mode not in {"local", "http"}:
        raise FinPilotError("PARAM_INVALID", "未知模拟提供方模式。")
    prefix = "merchant" if tool.startswith("merchant.") else "bank"
    return f"{prefix}_sandbox_v1" if mode == "http" else f"{prefix}_sandbox_local"


def register_adapter(adapter: ProviderAdapter) -> None:
    _ADAPTERS[adapter.provider_id] = adapter


def get_adapter(provider_id: str | None) -> ProviderAdapter:
    key = provider_id or "bank_sandbox_local"
    adapter = _ADAPTERS.get(key)
    if adapter is None:
        raise FinPilotError(
            "PROVIDER_CAPABILITY_MISSING",
            "提供方适配器未注册，已关闭自动写入。",
            details={"reason_codes": ["adapter_not_registered"], "provider_id": key},
        )
    caps = adapter.capabilities()
    if not caps.get("stable_key") or not caps.get("query"):
        raise FinPilotError(
            "PROVIDER_CAPABILITY_MISSING",
            "提供方不支持稳定业务键或结果查询，已关闭自动写入。",
            details={"reason_codes": ["missing_stable_key_or_query"], "provider_id": key},
        )
    return adapter


def registered_adapters() -> list[str]:
    return sorted(_ADAPTERS)


def clear_adapters() -> None:
    _ADAPTERS.clear()


def event_from_result(result: ProviderResult, *, operation_id: str, event_type: str, event_version: int = 1) -> dict:
    """把适配器结果转成提供方事件（与 HTTP 回调同一结构，走同一处理路径）。"""
    from ..ids import canonical_hash
    from ..timeutil import format_iso_utc, utcnow

    payload: dict[str, Any] = {
        "provider_event_id": f"{result.provider_ref}:{event_type}:{event_version}",
        "operation_id": operation_id,
        "provider_ref": result.provider_ref,
        "event_type": event_type,
        "event_version": event_version,
        "status": normalize_status(result.status),
        "provider_status_raw": result.status,
        "occurred_at": format_iso_utc(utcnow()),
        "payload_hash": canonical_hash(result.raw or {}),
        "raw": result.raw,
    }
    return payload
