"""定时任务与未来授权（V5 6.3、7.1、AUTH-07、S1-03）。

- 保存绝对执行时刻、时区、允许执行窗口、稳定操作 ID、未来授权与取消状态。
- 未来授权独立于登录会话，有效期必须覆盖执行时刻；到期重检，旧验证码不能绕过新规则。
- 任务重领仍是同一逻辑操作（operation_id 不变）；窗口外进入 missed，不数天后补转。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..ids import canonical_hash, new_id
from ..models import Operation, ScheduledJob, Task
from ..timeutil import SHANGHAI, format_iso, utcnow
from .identity import AuthContext
from .state_machine import assert_transition

LEASE_SECONDS = 60


def create_job(
    session: Session,
    ctx: AuthContext,
    *,
    job_type: str,
    due_at: datetime,
    payload: dict,
    task_id: str | None = None,
    grant_id: str | None = None,
    operation_id: str | None = None,
    window_minutes: int | None = None,
    dedupe_key: str | None = None,
) -> ScheduledJob:
    ctx.require_customer()
    settings = get_settings()
    now = utcnow()
    if due_at <= now - timedelta(minutes=1):
        raise FinPilotError("PARAM_INVALID", "执行时刻必须晚于当前时间。")
    key = dedupe_key or canonical_hash(
        {"job_type": job_type, "due_at": due_at.isoformat(), "task": task_id, "op": operation_id, "payload": payload}
    )
    existing = session.execute(select(ScheduledJob).where(ScheduledJob.dedupe_key == key)).scalar_one_or_none()
    if existing is not None:
        return existing
    job = ScheduledJob(
        job_id=new_id("job"),
        owner_user_id=ctx.user_id,
        job_type=job_type,
        due_at=due_at,
        window_minutes=window_minutes or settings.schedule_window_minutes,
        timezone="Asia/Shanghai",
        grant_id=grant_id,
        operation_id=operation_id,
        task_id=task_id,
        state="scheduled",
        dedupe_key=key,
        payload=payload,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def cancel_job(session: Session, ctx: AuthContext, *, job_id: str, reason: str = "user_cancelled") -> ScheduledJob:
    job = session.execute(select(ScheduledJob).where(ScheduledJob.job_id == job_id).with_for_update()).scalar_one_or_none()
    if job is None or job.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该定时任务。")
    if job.state in ("completed", "cancelled", "missed", "failed"):
        return job
    assert_transition("job", job.state, "cancelled")
    job.state = "cancelled"
    job.last_error = reason
    job.updated_at = utcnow()
    session.flush()
    return job


def reschedule_job(session: Session, ctx: AuthContext, *, job_id: str, due_at: datetime) -> ScheduledJob:
    """改时间必须重新授权：旧授权作废，任务回到待授权。"""
    job = session.execute(select(ScheduledJob).where(ScheduledJob.job_id == job_id).with_for_update()).scalar_one_or_none()
    if job is None or job.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该定时任务。")
    if job.state not in ("scheduled", "awaiting_authorization", "missed"):
        raise FinPilotError("POLICY_DENIED", "该任务状态不支持修改时间。")
    job.due_at = due_at
    job.state = "awaiting_authorization"
    job.grant_id = None
    job.updated_at = utcnow()
    session.flush()
    return job


def lease_due_jobs(session: Session, *, now: datetime | None = None, limit: int = 20) -> list[ScheduledJob]:
    """租约只防同时处理；真正防重复靠 operation_id 与业务唯一约束。"""
    moment = now or utcnow()
    rows = session.execute(
        select(ScheduledJob)
        .where(
            (ScheduledJob.state == "scheduled") & (ScheduledJob.due_at <= moment)
            | (ScheduledJob.state == "leased") & (ScheduledJob.lease_until.is_not(None)) & (ScheduledJob.lease_until < moment)
        )
        .order_by(ScheduledJob.due_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()
    for job in rows:
        if job.state == "scheduled":
            assert_transition("job", job.state, "leased")
        job.state = "leased"
        job.lease_until = moment + timedelta(seconds=LEASE_SECONDS)
        job.attempts += 1
        job.updated_at = moment
    session.flush()
    return rows


def mark_completed(session: Session, job: ScheduledJob, *, note: str = "") -> ScheduledJob:
    job.state = "completed"
    job.last_error = note or None
    job.updated_at = utcnow()
    session.flush()
    return job


def mark_missed(session: Session, job: ScheduledJob, *, note: str = "out_of_window") -> ScheduledJob:
    job.state = "missed"
    job.last_error = note
    job.updated_at = utcnow()
    session.flush()
    return job


def mark_awaiting_authorization(session: Session, job: ScheduledJob, *, note: str = "") -> ScheduledJob:
    job.state = "awaiting_authorization"
    job.last_error = note or None
    job.updated_at = utcnow()
    session.flush()
    return job


def mark_failed(session: Session, job: ScheduledJob, *, note: str = "") -> ScheduledJob:
    job.state = "failed"
    job.last_error = note or None
    job.updated_at = utcnow()
    session.flush()
    return job


def in_window(job: ScheduledJob, moment: datetime) -> bool:
    return job.due_at <= moment <= job.due_at + timedelta(minutes=job.window_minutes)


def job_view(job: ScheduledJob) -> dict:
    operation = None
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "state": job.state,
        "due_at": format_iso(job.due_at),
        "window_minutes": job.window_minutes,
        "timezone": job.timezone,
        "task_id": job.task_id,
        "operation_id": job.operation_id,
        "grant_id": job.grant_id,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "payload": job.payload,
        "operation": operation,
        "created_at": format_iso(job.created_at),
    }


def list_jobs(session: Session, ctx: AuthContext, *, state: str | None = None, limit: int = 50) -> list[dict]:
    stmt = select(ScheduledJob).where(ScheduledJob.owner_user_id == ctx.user_id)
    if state:
        stmt = stmt.where(ScheduledJob.state == state)
    rows = session.execute(stmt.order_by(ScheduledJob.due_at).limit(limit)).scalars().all()
    return [job_view(job) for job in rows]


def due_at_local(due_at: datetime) -> str:
    return due_at.astimezone(SHANGHAI).isoformat(timespec="minutes")