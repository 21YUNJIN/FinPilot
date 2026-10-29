"""提供方回调：验签、去重、版本核验、乱序与冲突处理（V5 7.2、API-04、T33）。

规则：
- 事件按 (provider_id, provider_event_id) 去重；重复事件不产生第二次金融效果。
- 旧版本回执不能覆盖新状态；冲突终态进入隔离并开人工核验工单。
- 时间窗外或验签失败的载荷不静默丢弃，转入查询核验。
"""

from __future__ import annotations

import hmac
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..ids import canonical_hash, hmac_hex
from ..models import Operation, OperatorCase, ProviderEvent
from ..timeutil import parse_iso, utcnow
from .transfers import record_receipt, settle_transfer

SIGNATURE_TOLERANCE = timedelta(minutes=5)


def sign_payload(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    return hmac_hex(secret, f"{timestamp}{nonce}".encode("utf-8") + body)


def verify_callback_signature(
    *,
    headers: dict,
    body: bytes,
    secret: str | None = None,
    now=None,
) -> tuple[bool, str]:
    """校验 HMAC 签名与时间窗；返回 (是否通过, 原因)。"""
    secret = secret or get_settings().provider_callback_secret
    signature = headers.get("x-finpilot-signature") or headers.get("X-FinPilot-Signature")
    timestamp = headers.get("x-finpilot-timestamp") or headers.get("X-FinPilot-Timestamp")
    nonce = headers.get("x-finpilot-nonce") or headers.get("X-FinPilot-Nonce")
    if not signature or not timestamp or not nonce:
        return False, "missing_signature_headers"
    expected = sign_payload(secret, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature):
        return False, "signature_mismatch"
    try:
        moment = parse_iso(timestamp)
    except ValueError:
        return False, "bad_timestamp"
    reference = now or utcnow()
    if abs((reference - moment).total_seconds()) > SIGNATURE_TOLERANCE.total_seconds():
        return False, "timestamp_out_of_window"
    return True, "ok"


def _highest_event_version(session: Session, operation_id: str) -> int:
    rows = session.execute(
        select(ProviderEvent.event_version).where(
            ProviderEvent.operation_id == operation_id, ProviderEvent.verified_status == "verified"
        )
    ).scalars().all()
    return max(rows) if rows else 0


def handle_provider_event(
    session: Session,
    *,
    provider_id: str,
    payload: dict,
    signature_ok: bool = True,
    source: str = "provider_callback",
    require_signature: bool = True,
) -> dict:
    """处理一条提供方事件；返回处理结果与是否改变了业务状态。"""
    if not signature_ok:
        if require_signature:
            raise FinPilotError(
                "POLICY_DENIED",
                "回调签名校验失败。",
                details={"reason_codes": ["signature_invalid"]},
            )

    event_id = str(payload.get("provider_event_id") or "")
    operation_id = str(payload.get("operation_id") or "")
    provider_ref = str(payload.get("provider_ref") or "")
    event_type = str(payload.get("event_type") or "unknown")
    event_version = int(payload.get("event_version") or 1)
    from .provider_adapter import normalize_status

    status = normalize_status(payload.get("status"))
    body_hash = canonical_hash(payload)
    now = utcnow()

    if not event_id or not operation_id:
        raise FinPilotError("PARAM_INVALID", "回调缺少事件编号或操作编号。")

    duplicate = session.execute(
        select(ProviderEvent).where(
            ProviderEvent.provider_id == provider_id, ProviderEvent.provider_event_id == event_id
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        return {"handled": False, "reason": "duplicate_event", "operation_id": operation_id}

    operation = session.execute(
        select(Operation)
        .where(Operation.operation_id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        # 迟到事件：保存以便人工核验，不静默丢弃
        session.add(
            ProviderEvent(
                provider_id=provider_id,
                provider_event_id=event_id,
                operation_id=operation_id,
                provider_ref=provider_ref,
                event_type=event_type,
                event_version=event_version,
                signature_ok=signature_ok,
                verified_status="orphan",
                received_at=now,
                payload_hash=body_hash,
                payload=payload,
                conflict_reason="orphan_operation",
            )
        )
        session.flush()
        return {"handled": False, "reason": "orphan_operation", "operation_id": operation_id}

    last_version = _highest_event_version(session, operation_id)
    conflict_reason: str | None = None
    verified_status = "verified"

    if event_version < last_version:
        verified_status = "stale"
        conflict_reason = "stale_event_version"
    elif operation.status == "succeeded" and status in ("failed", "cancelled"):
        verified_status = "conflict"
        conflict_reason = "terminal_state_conflict"
    elif operation.status in ("failed", "cancelled") and status == "succeeded":
        verified_status = "conflict"
        conflict_reason = "terminal_state_conflict"

    session.add(
        ProviderEvent(
            provider_id=provider_id,
            provider_event_id=event_id,
            operation_id=operation_id,
            provider_ref=provider_ref,
            event_type=event_type,
            event_version=event_version,
            signature_ok=signature_ok,
            verified_status=verified_status,
            received_at=now,
            payload_hash=body_hash,
            payload=payload,
            conflict_reason=conflict_reason,
        )
    )
    session.flush()

    if verified_status != "verified":
        if conflict_reason == "terminal_state_conflict":
            case = OperatorCase(
                task_id=operation.task_id,
                owner_user_id=operation.owner_user_id,
                reason="conflicting_terminal_state",
                status="open",
                handoff={
                    "operation_id": operation_id,
                    "local_status": operation.status,
                    "provider_status": status,
                    "provider_ref": provider_ref,
                },
                actions=[],
                created_at=now,
                updated_at=now,
            )
            session.add(case)
            session.flush()
            return {
                "handled": False,
                "reason": conflict_reason,
                "operator_case_id": case.case_id,
                "operation_id": operation_id,
            }
        return {"handled": False, "reason": conflict_reason or verified_status, "operation_id": operation_id}

    record_receipt(
        session,
        operation=operation,
        provider_id=provider_id,
        provider_ref=provider_ref,
        payload=payload,
        source=source,
        verified_status="verified",
        event_version=event_version,
    )

    settle_transfer(
        session,
        operation=operation,
        status=status,
        provider_ref=provider_ref,
        raw=payload.get("raw") or {},
        source=source,
    )
    session.flush()
    return {"handled": True, "reason": "applied", "operation_id": operation_id, "status": operation.status}