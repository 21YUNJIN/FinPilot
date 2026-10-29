"""调度器工作者：到点在窗口内重检并执行，窗口外 missed（V5 S1-03、AUTH-07、T05）。

到期重检顺序：策略等级（执行日的日额度）→ 授权有效性（独立未来授权）→ 资金 →
业务前置条件。任何一项不再满足都进入 awaiting_authorization 或 failed，不静默执行。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..kernel.identity import AuthContext
from ..kernel.scheduled_jobs import (
    in_window,
    lease_due_jobs,
    mark_awaiting_authorization,
    mark_completed,
    mark_failed,
    mark_missed,
)
from ..kernel.submission import OperationDraft, prepare_operation, submit_operation
from ..models import Operation, ScheduledJob
from ..timeutil import utcnow
from .outbox import drain

logger = logging.getLogger(__name__)

# 领域作业处理器注册点：领域模块导出 JOB_HANDLERS = {job_type: callable(session, job, now) -> outcome}
# 调度器只负责租约、窗口与重检，具体业务效果由领域实现，避免在调度器里重写业务规则。
_JOB_HANDLER_MODULES = (
    "finpilot.domains.subscriptions.reminders",
    "finpilot.domains.cards.service",
    "finpilot.domains.wealth_orders.service",
)


def job_handlers() -> dict:
    handlers: dict = {}
    for module_name in _JOB_HANDLER_MODULES:
        try:
            module = __import__(module_name, fromlist=["JOB_HANDLERS"])
        except Exception:  # pragma: no cover - 领域尚未实现时跳过
            continue
        handlers.update(getattr(module, "JOB_HANDLERS", {}) or {})
    return handlers


def run_due_jobs(session: Session, *, now: datetime | None = None, limit: int = 20, dispatch: bool = True) -> dict:
    moment = now or utcnow()
    jobs = lease_due_jobs(session, now=moment, limit=limit)
    executed = 0
    missed = 0
    waiting = 0
    failed = 0

    for job in jobs:
        try:
            if not in_window(job, moment):
                mark_missed(session, job, note="out_of_window")
                missed += 1
                continue

            outcome = _execute_job(session, job, moment)
            if outcome == "completed":
                mark_completed(session, job)
                executed += 1
            elif outcome == "awaiting_authorization":
                mark_awaiting_authorization(session, job, note="authorization_required_on_due")
                waiting += 1
            elif outcome == "missed":
                mark_missed(session, job, note="out_of_window")
                missed += 1
            else:
                mark_failed(session, job, note=outcome)
                failed += 1
        except FinPilotError as exc:
            if exc.code in ("GRANT_REQUIRED", "GRANT_INVALID", "GRANT_SCOPE_VIOLATION"):
                mark_awaiting_authorization(session, job, note=exc.code)
                waiting += 1
            elif exc.code in ("INSUFFICIENT_FUNDS", "PROTECTED_FLOOR_VIOLATION", "RESERVATION_EXCEEDED"):
                mark_failed(session, job, note=exc.code)
                failed += 1
            else:
                mark_failed(session, job, note=exc.code)
                failed += 1
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("scheduled job failed: %s", exc)
            mark_failed(session, job, note="unexpected_error")
            failed += 1

    if dispatch:
        drain(session)
    session.flush()
    return {"leased": len(jobs), "executed": executed, "waiting": waiting, "missed": missed, "failed": failed}


def _execute_job(session: Session, job: ScheduledJob, moment: datetime) -> str:
    payload = job.payload or {}

    # 领域作业（提醒、续费通知等）：优先走领域处理器，不经过资金提交路径
    domain_handler = job_handlers().get(job.job_type)
    if domain_handler is not None:
        outcome = domain_handler(session, job, moment)
        if isinstance(outcome, dict):
            return str(outcome.get("outcome") or "completed")
        return str(outcome or "completed")

    tool = payload.get("tool")
    typed_args = dict(payload.get("typed_args") or {})
    if not tool:
        return "missing_tool"

    ctx = AuthContext(user_id=job.owner_user_id, roles=("customer",), channel="scheduler", session_id="scheduler")

    operation_id = job.operation_id or payload.get("operation_id")
    operation: Operation | None = session.get(Operation, operation_id) if operation_id else None

    if operation is None:
        draft = OperationDraft(
            tool=tool,
            typed_args=typed_args,
            operation_id=operation_id or "",
            task_id=job.task_id,
            plan_id=payload.get("plan_id"),
            plan_revision=int(payload.get("plan_revision", 1)),
            source_account_id=typed_args.get("source_account_id"),
            amount_minor=int(typed_args.get("amount_minor") or 0),
            fee_minor=int(typed_args.get("fee_minor") or 0),
            provider_id=payload.get("provider_id"),
        )
        operation = prepare_operation(session, ctx, draft)
        job.operation_id = operation.operation_id
        payload["operation_id"] = operation.operation_id
        job.payload = payload

    if operation.status in ("succeeded", "failed", "cancelled"):
        return "completed" if operation.status == "succeeded" else "failed"

    if operation.status in ("dispatching", "pending", "unknown"):
        return "completed"

    submit_operation(
        session,
        ctx,
        operation_id=operation.operation_id,
        grant_id=job.grant_id,
        expected_version=None,
    )
    session.flush()
    return "completed"


def run_once(session: Session, *, now: datetime | None = None) -> dict:
    return run_due_jobs(session, now=now)


def run_job_forever(session_factory, interval_seconds: int = 5) -> None:  # pragma: no cover - 常驻入口
    import time

    while True:
        with session_factory() as session:
            try:
                run_due_jobs(session)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("scheduler loop error")
        time.sleep(interval_seconds)