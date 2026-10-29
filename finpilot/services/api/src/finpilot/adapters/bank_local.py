"""本地银行提供方适配器：确定性、可注入故障，用于开发、测试与离线重放。

它与 HTTP 沙箱适配器实现同一协议，主流程代码路径完全一致（都经 Outbox 与回调处理），
因此"换提供方"不需要改业务逻辑。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..errors import FinPilotError
from ..ids import canonical_hash
from ..kernel.provider_adapter import ProviderResult
from ..timeutil import format_iso_utc, utcnow

PROVIDER_ID = "bank_sandbox_local"


@dataclass
class LocalBankState:
    records: dict[str, dict] = field(default_factory=dict)
    faults: dict = field(default_factory=dict)
    event_counter: int = 0


_STATE = LocalBankState()


def reset() -> None:
    _STATE.records.clear()
    _STATE.faults.clear()
    _STATE.event_counter = 0


def configure_faults(**faults) -> None:
    """故障注入：settle_immediately / fail_next / drop_response / accept_only / query_fail_times。"""
    _STATE.faults.update(faults)


def _provider_ref(stable_ref: str) -> str:
    return "BS" + hashlib.sha256(stable_ref.encode("utf-8")).hexdigest()[:14].upper()


class LocalBankAdapter:
    provider_id = PROVIDER_ID

    def capabilities(self) -> dict:
        return {"stable_key": True, "query": True, "cancel": True, "async_callback": True, "provider": "local_sandbox"}

    def submit(self, *, stable_operation_ref: str, payload: dict) -> ProviderResult:
        faults = _STATE.faults
        if faults.get("unavailable"):
            raise FinPilotError("PROVIDER_UNAVAILABLE", "提供方暂时不可用，未产生资金效果。")
        if faults.get("raise_timeout"):
            faults.pop("raise_timeout", None)
            raise FinPilotError(
                "PROVIDER_TIMEOUT",
                "提供方应答超时，结果未知。",
                details={"reason_codes": ["timeout_no_response"]},
            )

        existing = _STATE.records.get(stable_operation_ref)
        if existing is None:
            status = "accepted"
            if faults.get("fail_next"):
                faults.pop("fail_next", None)
                status = "failed"
            elif faults.get("accept_only"):
                status = "accepted"
            elif faults.get("settle_immediately", True):
                status = "succeeded"
            _STATE.records[stable_operation_ref] = {
                "provider_ref": _provider_ref(stable_operation_ref),
                "status": status,
                "payload": payload,
                "accepted_at": format_iso_utc(utcnow()),
                "event_version": 1,
                "cancel_requested": False,
                "raw": {"sandbox": "local", "note": "确定性本地提供方"},
            }
        record = _STATE.records[stable_operation_ref]
        if record["status"] == "accepted" and not faults.get("accept_only"):
            # 已受理但未结算：下一次查询/推进才结算（模拟异步）
            pass
        result = ProviderResult(
            provider_ref=record["provider_ref"],
            status=record["status"],
            raw=record["raw"],
            capabilities=self.capabilities(),
        )
        if record["status"] in ("succeeded", "failed"):
            result.events = [self._event(stable_operation_ref, record)]
        return result

    def _event(self, stable_ref: str, record: dict) -> dict:
        event_type = "transfer.settled" if record["status"] == "succeeded" else "transfer.rejected"
        return {
            "provider_event_id": f"{record['provider_ref']}:{event_type}:{record['event_version']}",
            "operation_id": stable_ref,
            "provider_ref": record["provider_ref"],
            "event_type": event_type,
            "event_version": record["event_version"],
            "status": record["status"],
            "occurred_at": format_iso_utc(utcnow()),
            "payload_hash": canonical_hash(record["raw"]),
            "raw": record["raw"],
        }

    def query(self, *, stable_operation_ref: str) -> ProviderResult:
        faults = _STATE.faults
        if faults.get("query_fail_times", 0) > 0:
            faults["query_fail_times"] -= 1
            raise FinPilotError("PROVIDER_TIMEOUT", "查询超时，结果仍未确认。")
        record = _STATE.records.get(stable_operation_ref)
        if record is None:
            raise FinPilotError("PROVIDER_REJECTED", "该业务键在提供方不存在。")
        return ProviderResult(
            provider_ref=record["provider_ref"],
            status=record["status"],
            raw=record["raw"],
            capabilities=self.capabilities(),
            events=[self._event(stable_operation_ref, record)] if record["status"] in ("succeeded", "failed") else [],
        )

    def cancel(self, *, stable_operation_ref: str) -> ProviderResult:
        record = _STATE.records.get(stable_operation_ref)
        if record is None:
            raise FinPilotError("PROVIDER_REJECTED", "该业务键在提供方不存在。")
        if record["status"] == "succeeded":
            return ProviderResult(
                provider_ref=record["provider_ref"],
                status="cancel_rejected",
                raw={"reason": "already_settled"},
                capabilities=self.capabilities(),
            )
        record["status"] = "cancelled"
        record["event_version"] += 1
        return ProviderResult(
            provider_ref=record["provider_ref"],
            status="cancelled",
            raw={"reason": "cancelled_before_settlement"},
            capabilities=self.capabilities(),
            events=[
                {
                    "provider_event_id": f"{record['provider_ref']}:transfer.cancelled:{record['event_version']}",
                    "operation_id": stable_operation_ref,
                    "provider_ref": record["provider_ref"],
                    "event_type": "transfer.cancelled",
                    "event_version": record["event_version"],
                    "status": "cancelled",
                    "occurred_at": format_iso_utc(utcnow()),
                    "payload_hash": canonical_hash(record["raw"]),
                    "raw": record["raw"],
                }
            ],
        )

    def advance(self, *, stable_operation_ref: str, settle: bool = True) -> ProviderResult:
        """测试辅助：推进异步受理到结算。"""
        record = _STATE.records.get(stable_operation_ref)
        if record is None:
            raise FinPilotError("PROVIDER_REJECTED", "该业务键在提供方不存在。")
        if record["status"] == "accepted":
            record["status"] = "succeeded" if settle else "failed"
            record["event_version"] += 1
        return ProviderResult(
            provider_ref=record["provider_ref"],
            status=record["status"],
            raw=record["raw"],
            capabilities=self.capabilities(),
            events=[self._event(stable_operation_ref, record)],
        )


def build_adapter() -> LocalBankAdapter:
    return LocalBankAdapter()