"""任务仓储：任务生命周期、修订与事件（V5 7.1、PLAN-02、PLAN-06）。

修订规则：用户改意产生新 revision；历史修订不改写；旧修订提交返回 VERSION_CONFLICT。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..ids import new_id
from ..models import Plan, PlanStep, Task, TaskEvent, TaskMessage
from ..timeutil import format_iso, utcnow
from ..kernel.identity import AuthContext
from ..kernel.state_machine import assert_transition


def create_task(
    session: Session,
    ctx: AuthContext,
    *,
    message: str,
    channel: str,
    reference_time: datetime,
    mode: str = "mock",
) -> Task:
    now = utcnow()
    task = Task(
        task_id=new_id("task"),
        owner_user_id=ctx.user_id,
        channel=channel,
        status="draft",
        revision=1,
        reference_time=reference_time,
        goal_spec={},
        clarifications=[],
        preferences=[],
        result_summary={},
        mode=mode,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    add_message(session, task, role="user", channel=channel, content=message)
    record_event(session, task, None, "draft", "created", {})
    return task


def get_task(session: Session, ctx: AuthContext, task_id: str, *, allow_operator: bool = False) -> Task:
    task = session.get(Task, task_id)
    if task is None or (task.owner_user_id != ctx.user_id and not (allow_operator and ctx.is_operator)):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该任务。")
    return task


def add_message(
    session: Session,
    task: Task,
    *,
    role: str,
    channel: str,
    content: str,
    structured: dict | None = None,
) -> TaskMessage:
    message = TaskMessage(
        task_id=task.task_id,
        role=role,
        channel=channel,
        content=content,
        structured=structured or {},
        revision=task.revision,
        created_at=utcnow(),
    )
    session.add(message)
    session.flush()
    return message


def record_event(session: Session, task: Task, from_status: str | None, to_status: str, reason: str, payload: dict) -> TaskEvent:
    event = TaskEvent(
        task_id=task.task_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        payload=payload,
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()
    return event


def transition_task(session: Session, task: Task, to_status: str, *, reason: str, payload: dict | None = None) -> Task:
    if task.status == to_status:
        return task
    assert_transition("task", task.status, to_status)
    from_status = task.status
    task.status = to_status
    task.updated_at = utcnow()
    record_event(session, task, from_status, to_status, reason, payload or {})
    session.flush()
    return task


def bump_revision(session: Session, task: Task, *, reason: str, payload: dict | None = None) -> Task:
    """用户改意：产生新修订，保留历史。"""
    task.revision += 1
    task.updated_at = utcnow()
    record_event(session, task, task.status, task.status, f"revision:{reason}", payload or {})
    session.flush()
    return task


def assert_expected_revision(task: Task, expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    if expected_revision != task.revision:
        raise FinPilotError(
            "VERSION_CONFLICT",
            "任务已被其他操作更新，请刷新后重试。",
            details={"reason_codes": ["stale_revision"], "current_revision": task.revision},
        )


def current_plan(session: Session, task: Task) -> Plan | None:
    if not task.plan_id:
        return None
    return session.get(Plan, task.plan_id)


def plan_steps(session: Session, plan_id: str) -> list[PlanStep]:
    return list(
        session.execute(select(PlanStep).where(PlanStep.plan_id == plan_id).order_by(PlanStep.seq)).scalars()
    )


def save_plan(session: Session, task: Task, plan: Plan, steps: list[PlanStep]) -> Plan:
    session.add(plan)
    session.flush()
    for step in steps:
        step.plan_id = plan.plan_id
        session.add(step)
    task.plan_id = plan.plan_id
    session.flush()
    return plan


def task_view(session: Session, task: Task) -> dict:
    plan = current_plan(session, task)
    steps = plan_steps(session, plan.plan_id) if plan else []
    clarifications = [
        {
            "clarification_id": c.get("clarification_id"),
            "question": c.get("question"),
            "field": c.get("field"),
            "options": c.get("options", []),
            "answered": c.get("answered", False),
        }
        for c in (task.clarifications or [])
    ]
    return {
        "task_id": task.task_id,
        "owner_user_id": task.owner_user_id,
        "channel": task.channel,
        "status": task.status,
        "revision": task.revision,
        "reference_time": format_iso(task.reference_time),
        "mode": task.mode,
        "goal": task.goal_spec or {},
        "preferences": task.preferences or [],
        "clarifications": clarifications,
        "blocked_reason": task.blocked_reason,
        "plan": plan_view(session, plan, steps) if plan else None,
        "result": task.result_summary or {},
        "created_at": format_iso(task.created_at),
        "updated_at": format_iso(task.updated_at),
        "steps": [step_view(step) for step in steps],
    }


def step_view(step: PlanStep) -> dict:
    return {
        "step_id": step.step_id,
        "node_id": step.node_id,
        "seq": step.seq,
        "tool": step.tool,
        "typed_args": step.typed_args,
        "dependencies": step.dependencies,
        "status": step.status,
        "required_level": step.required_level,
        "operation_id": step.operation_id,
        "expected_effects": step.expected_effects,
        "last_error": step.last_error,
    }


def plan_view(session: Session, plan: Plan, steps: list[PlanStep]) -> dict:
    return {
        "plan_id": plan.plan_id,
        "task_id": plan.task_id,
        "revision": plan.revision,
        "status": plan.status,
        "feasible": plan.feasible,
        "infeasible_reasons": plan.infeasible_reasons,
        "required_level": plan.required_level,
        "scope": plan.scope,
        "scope_hash": plan.scope_hash,
        "total_estimated_minor": plan.total_estimated_minor,
        "total_fee_minor": plan.total_fee_minor,
        "alternatives": plan.alternatives,
        "steps": [step_view(step) for step in steps],
        "created_at": format_iso(plan.created_at),
    }


def task_messages(session: Session, task_id: str, limit: int = 100) -> list[dict]:
    rows = session.execute(
        select(TaskMessage).where(TaskMessage.task_id == task_id).order_by(TaskMessage.created_at).limit(limit)
    ).scalars().all()
    return [
        {
            "message_id": row.message_id,
            "role": row.role,
            "channel": row.channel,
            "content": row.content,
            "structured": row.structured,
            "revision": row.revision,
            "created_at": format_iso(row.created_at),
        }
        for row in rows
    ]


def list_tasks(session: Session, ctx: AuthContext, *, limit: int = 50) -> list[dict]:
    rows = session.execute(
        select(Task).where(Task.owner_user_id == ctx.user_id).order_by(Task.updated_at.desc()).limit(limit)
    ).scalars().all()
    return [
        {
            "task_id": row.task_id,
            "status": row.status,
            "revision": row.revision,
            "channel": row.channel,
            "summary": (row.goal_spec or {}).get("summary"),
            "updated_at": format_iso(row.updated_at),
        }
        for row in rows
    ]