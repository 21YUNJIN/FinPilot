"""提供方回调入口：验签、去重、版本核验与对账入队（V5 16.1、API-04）。

时间窗外或验签失败的载荷不静默丢弃：记录为待查询核验，避免丢掉真实成功。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from ..envelope import ok
from ..errors import FinPilotError
from ..kernel.provider_events import handle_provider_event, verify_callback_signature
from ..security.audit import append_event
from .deps import get_db

router = APIRouter(prefix="/internal/provider-events", tags=["callbacks"])

KNOWN_PROVIDERS = ("bank_sandbox_v1", "merchant_sandbox_v1", "bank_sandbox_local")


@router.post("/{provider_id}")
async def receive_provider_event(
    provider_id: str,
    request: Request,
    x_finpilot_signature: str | None = Header(default=None),
    x_finpilot_timestamp: str | None = Header(default=None),
    x_finpilot_nonce: str | None = Header(default=None),
    session: Session = Depends(get_db),
):
    if provider_id not in KNOWN_PROVIDERS:
        raise FinPilotError("PROVIDER_CAPABILITY_MISSING", "未知的业务提供方。")
    raw = await request.body()
    headers = {
        "x-finpilot-signature": x_finpilot_signature or "",
        "x-finpilot-timestamp": x_finpilot_timestamp or "",
        "x-finpilot-nonce": x_finpilot_nonce or "",
    }
    signature_ok, reason = verify_callback_signature(headers=headers, body=raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinPilotError("PARAM_INVALID", "回调载荷不是合法 JSON。") from exc

    if not signature_ok:
        # 不静默丢弃：记录并转查询核验
        session.add(__import__("finpilot.models", fromlist=["ProviderEvent"]).ProviderEvent(
            provider_id=provider_id,
            provider_event_id=str(payload.get("provider_event_id") or "unknown"),
            operation_id=payload.get("operation_id"),
            provider_ref=payload.get("provider_ref"),
            event_type=str(payload.get("event_type") or "unknown"),
            event_version=int(payload.get("event_version") or 1),
            signature_ok=False,
            verified_status="unverified",
            received_at=__import__("finpilot.timeutil", fromlist=["utcnow"]).utcnow(),
            payload_hash=__import__("finpilot.ids", fromlist=["canonical_hash"]).canonical_hash(payload),
            payload=payload,
            conflict_reason=reason,
        ))
        session.flush()
        raise FinPilotError(
            "POLICY_DENIED",
            "回调验签失败，已记录待人工核验。",
            details={"reason_codes": [reason]},
        )

    result = handle_provider_event(
        session,
        provider_id=provider_id,
        payload=payload,
        signature_ok=True,
        source="provider_callback",
    )
    append_event(
        session,
        event_type="provider.event_received",
        actor=provider_id,
        actor_role="provider",
        operation_id=payload.get("operation_id"),
        payload={"result": result, "event_type": payload.get("event_type")},
    )
    return ok(result, operation_id=payload.get("operation_id"), adapter=provider_id)