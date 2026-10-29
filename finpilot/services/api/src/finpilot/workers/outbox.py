"""Outbox 工作者：外部效果只能在本事务提交后发生（V5 8.2、MONEY-04、T21/T22）。

- 领取采用 SKIP LOCKED，避免多工作者重复处理。
- 调用超时/连接失败：结果未知时保留 unknown 与资金占用，绝不写成失败。
- 只有提供方可验证的拒绝才进入 failed。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..kernel.provider_adapter import get_adapter
from ..kernel.provider_events import handle_provider_event
from ..kernel.state_machine import assert_transition
from ..models import Operation, Outbox
from ..timeutil import utcnow

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (1, 2, 5, 10, 30)
MAX_ATTEMPTS = 6


def _backoff(attempts: int) -> timedelta:
    index = min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
    return timedelta(seconds=BACKOFF_SECONDS[index])


def process_pending(session: Session, *, limit: int = 20, now: datetime | None = None) -> dict:
    moment = now or utcnow()
    rows = session.execute(
        select(Outbox)
        .where(Outbox.delivery_state == "pending", Outbox.next_attempt_at <= moment)
        .order_by(Outbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    delivered = 0
    retried = 0
    unknown = 0
    for row in rows:
        outcome = _dispatch(session, row, moment)
        if outcome == "delivered":
            delivered += 1
        elif outcome == "unknown":
            unknown += 1
        else:
            retried += 1
    session.flush()
    return {"picked": len(rows), "delivered": delivered, "retried": retried, "unknown": unknown}


def _dispatch(session: Session, row: Outbox, moment: datetime) -> str:
    # 必须强制重新读取并加锁：工作者长事务或调用方会话可能缓存了旧状态
    operation = session.execute(
        select(Operation)
        .where(Operation.operation_id == row.aggregate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        row.delivery_state = "failed"
        row.last_error = "operation_missing"
        return "retried"

    try:
        adapter = get_adapter(operation.provider_id or row.payload.get("provider_id"))
    except FinPilotError as exc:
        row.delivery_state = "failed"
        row.last_error = exc.code
        if operation.status == "dispatching":
            assert_transition("operation", operation.status, "failed")
            operation.status = "failed"
            operation.error_code = exc.code
            operation.version += 1
            operation.updated_at = moment
        return "retried"

    row.attempts += 1
    try:
        result = adapter.submit(stable_operation_ref=operation.operation_id, payload=row.payload)
    except FinPilotError as exc:
        if exc.code == "PROVIDER_TIMEOUT":
            # 外部可能已受理：保留 unknown 与占用，改用原键查询
            if operation.status == "dispatching":
                assert_transition("operation", operation.status, "unknown")
                operation.status = "unknown"
                operation.unknown_since = moment
                operation.version += 1
                operation.updated_at = moment
            row.next_attempt_at = moment + _backoff(row.attempts)
            row.last_error = exc.code
            return "unknown"
        if exc.code in ("PROVIDER_UNAVAILABLE",):
            # 确定未产生效果：可重试，金额仍占用直到确认
            row.next_attempt_at = moment + _backoff(row.attempts)
            row.last_error = exc.code
            if row.attempts >= MAX_ATTEMPTS:
                row.delivery_state = "failed"
            return "retried"
        row.delivery_state = "failed"
        row.last_error = exc.code
        if operation.status in ("dispatching", "pending"):
            assert_transition("operation", operation.status, "failed")
            operation.status = "failed"
            operation.error_code = exc.code
            operation.version += 1
            operation.updated_at = moment
        return "retried"
    except Exception as exc:  # pragma: no cover - 兜底：未知一律不写成失败
        logger.warning("provider dispatch failed: %s", exc)
        if operation.status == "dispatching":
            operation.status = "unknown"
            operation.unknown_since = moment
            operation.version += 1
            operation.updated_at = moment
        row.next_attempt_at = moment + _backoff(row.attempts)
        row.last_error = "unexpected_error"
        return "unknown"

    operation.provider_id = adapter.provider_id
    operation.provider_order_ref = result.provider_ref
    operation.submitted_at = operation.submitted_at or moment

    if result.status in ("accepted", "pending"):
        if operation.status == "dispatching":
            assert_transition("operation", operation.status, "pending")
            operation.status = "pending"
            operation.version += 1
            operation.updated_at = moment

    for event in result.events:
        handle_provider_event(
            session,
            provider_id=adapter.provider_id,
            payload=event,
            signature_ok=True,
            source="outbox_dispatch",
            require_signature=False,
        )

    row.delivery_state = "delivered"
    row.delivered_at = moment
    return "delivered"


def drain(session: Session, *, max_rounds: int = 8, limit: int = 50) -> dict:
    """在同一请求内排空待投递事件（本地开发与测试用；生产由常驻工作者执行）。

    仍然走 Outbox 代码路径，不绕过持久化提交意图。
    """
    total = {"picked": 0, "delivered": 0, "retried": 0, "unknown": 0}
    for _ in range(max_rounds):
        result = process_pending(session, limit=limit)
        for key in total:
            total[key] += result[key]
        if result["picked"] == 0:
            break
    return total


def requeue_stuck(session: Session, *, older_than_seconds: int = 300, now: datetime | None = None) -> int:
    """把长时间未投递的事件重新排入队列（重启恢复）。"""
    moment = now or utcnow()
    rows = session.execute(
        select(Outbox).where(
            Outbox.delivery_state == "pending",
            Outbox.next_attempt_at <= moment - timedelta(seconds=older_than_seconds),
        )
    ).scalars().all()
    for row in rows:
        row.next_attempt_at = moment
    session.flush()
    return len(rows)