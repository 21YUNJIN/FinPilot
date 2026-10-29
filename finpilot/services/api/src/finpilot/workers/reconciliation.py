"""对账与重启恢复（V5 7.2、STATE-02、MONEY-04、T21/T22/T54）。

- 未确认结果必须用**原 operation_id** 查询，绝不重新发起支付。
- 查询持续失败时保持 unknown 并继续占用资金；只有提供方可验证的拒绝才进入 failed。
- 超过升级阈值仍未确认 → 开人工接管工单。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..kernel.identity import auth_context_for_system
from ..kernel.provider_adapter import event_from_result, get_adapter
from ..kernel.provider_events import handle_provider_event
from ..kernel.reservations import clear_inflight
from ..models import Operation, OperatorCase, Outbox, Reservation, ScheduledJob
from ..timeutil import utcnow
from .outbox import requeue_stuck

logger = logging.getLogger(__name__)

INFLIGHT_STATUSES = ("dispatching", "pending", "unknown")
ESCALATE_AFTER = timedelta(minutes=15)
QUERY_AFTER = timedelta(seconds=1)


def reconcile_unknown_operations(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 50,
    escalate_after: timedelta = ESCALATE_AFTER,
) -> dict:
    moment = now or utcnow()
    rows = session.execute(
        select(Operation)
        .where(
            Operation.status.in_(INFLIGHT_STATUSES),
            Operation.updated_at <= moment - QUERY_AFTER,
        )
        .order_by(Operation.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).scalars().all()

    stats = {"checked": 0, "settled": 0, "still_unknown": 0, "escalated": 0, "errors": 0, "skipped": 0}
    for operation in rows:
        stats["checked"] += 1
        try:
            adapter = get_adapter(operation.provider_id)
        except FinPilotError as exc:
            stats["skipped"] += 1
            operation.error_code = exc.code
            continue

        try:
            result = adapter.query(stable_operation_ref=operation.operation_id)
        except FinPilotError as exc:
            stats["errors"] += 1
            if operation.status != "unknown":
                operation.status = "unknown"
                operation.unknown_since = operation.unknown_since or moment
                operation.version += 1
                operation.updated_at = moment
            _maybe_escalate(session, operation, moment, escalate_after, reason=exc.code)
            continue
        except Exception as exc:  # pragma: no cover - 兜底：绝不写成失败
            logger.warning("reconcile query failed: %s", exc)
            stats["errors"] += 1
            _maybe_escalate(session, operation, moment, escalate_after, reason="query_error")
            continue

        events = result.events or (
            [event_from_result(result, operation_id=operation.operation_id, event_type=f"{operation.tool}.query")]
            if result.terminal
            else []
        )
        if not events:
            stats["still_unknown"] += 1
            if operation.status != "unknown":
                operation.status = "unknown"
                operation.unknown_since = operation.unknown_since or moment
                operation.version += 1
                operation.updated_at = moment
            _maybe_escalate(session, operation, moment, escalate_after, reason="no_terminal_status")
            continue

        for event in events:
            handle_provider_event(
                session,
                provider_id=adapter.provider_id,
                payload=event,
                signature_ok=True,
                source="reconciliation_query",
                require_signature=False,
            )
        session.refresh(operation)
        if operation.status in ("succeeded", "failed", "cancelled"):
            stats["settled"] += 1
            _clear_related_inflight(session, operation)
        else:
            stats["still_unknown"] += 1

    session.flush()
    return stats


def _clear_related_inflight(session: Session, operation: Operation) -> None:
    rows = session.execute(
        select(Reservation).where(
            Reservation.task_id == operation.task_id,
            Reservation.account_id == operation.source_account_id,
            Reservation.inflight_minor > 0,
        )
    ).scalars().all()
    for reservation in rows:
        clear_inflight(session, reservation_id=reservation.reservation_id)


def _maybe_escalate(
    session: Session,
    operation: Operation,
    moment: datetime,
    escalate_after: timedelta,
    *,
    reason: str,
) -> None:
    since = operation.unknown_since or operation.submitted_at
    if since is None or moment - since < escalate_after:
        return
    existing = session.execute(
        select(OperatorCase).where(
            OperatorCase.task_id == operation.task_id,
            OperatorCase.reason == "unknown_escalation",
            OperatorCase.status.in_(("open", "in_progress")),
        )
    ).scalars().first()
    if existing is not None:
        return
    case = OperatorCase(
        case_id=f"case_{operation.operation_id}"[:48],
        task_id=operation.task_id,
        owner_user_id=operation.owner_user_id,
        reason="unknown_escalation",
        status="open",
        handoff={
            "operation_id": operation.operation_id,
            "status": operation.status,
            "amount_minor": operation.amount_minor,
            "provider_id": operation.provider_id,
            "provider_ref": operation.provider_order_ref,
            "last_reason": reason,
            "note": "超过 15 分钟仍未确认；只能按原 operation_id 查询，不得重新发起支付",
        },
        actions=[],
        created_at=moment,
        updated_at=moment,
    )
    session.add(case)
    session.flush()


def restart_recovery(session: Session, *, now: datetime | None = None) -> dict:
    """进程重启后的重建：重排 Outbox、把超时的 dispatching 降级为 unknown、重领过期租约。"""
    moment = now or utcnow()
    requeued = requeue_stuck(session, older_than_seconds=0, now=moment)

    stale = session.execute(
        select(Operation)
        .where(Operation.status == "dispatching", Operation.updated_at <= moment - timedelta(seconds=30))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).scalars().all()
    marked_unknown = 0
    for operation in stale:
        operation.status = "unknown"
        operation.unknown_since = operation.unknown_since or moment
        operation.version += 1
        operation.updated_at = moment
        marked_unknown += 1

    expired_leases = session.execute(
        select(ScheduledJob).where(ScheduledJob.state == "leased", ScheduledJob.lease_until < moment)
    ).scalars().all()
    for job in expired_leases:
        job.state = "scheduled"
        job.updated_at = moment

    outbox_pending = session.execute(
        select(Outbox).where(Outbox.delivery_state == "pending")
    ).scalars().all()

    session.flush()
    return {
        "requeued_outbox": requeued,
        "pending_outbox": len(outbox_pending),
        "dispatching_marked_unknown": marked_unknown,
        "released_job_leases": len(expired_leases),
        "observed_at": moment.isoformat(),
    }