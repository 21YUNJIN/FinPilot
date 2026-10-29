"""人工接管与运营动作（V5 7.2、AUTH-08、PLAN-06、T45）。

接管清单必须包含：已完成、未知、未提交、资金占用与授权状态，避免人工重复执行。
运营只能复核、查询、按规则解锁或推进审批，不能伪造客户同意、不能代替用户强验证。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..ids import new_id
from ..models import Grant, Operation, OperatorCase, Reservation, ScheduledJob, Task
from ..timeutil import format_iso, utcnow
from ..kernel.identity import AuthContext
from . import task_repository as repo

OPERATOR_ACTIONS = ("requery_operation", "release_user_lock", "approve_card_application", "close_case", "escalate", "retry_safe_step")


def open_case(session: Session, ctx: AuthContext, *, task_id: str, reason: str) -> OperatorCase:
    task = repo.get_task(session, ctx, task_id)
    existing = session.execute(
        select(OperatorCase).where(
            OperatorCase.task_id == task_id, OperatorCase.status.in_(("open", "in_progress"))
        )
    ).scalars().first()
    if existing is not None:
        return existing
    now = utcnow()
    case = OperatorCase(
        case_id=new_id("case"),
        task_id=task_id,
        owner_user_id=task.owner_user_id,
        reason=reason,
        status="open",
        handoff=case_handoff(session, None, task_id=task_id),
        actions=[],
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    session.flush()
    repo.transition_task(session, task, "awaiting_human", reason=f"handoff:{reason}", payload={"case_id": case.case_id})
    return case


def case_handoff(session: Session, ctx: AuthContext | None, *, task_id: str) -> dict:
    """生成完整交接清单，避免人工再次执行已提交操作。"""
    operations = session.execute(select(Operation).where(Operation.task_id == task_id)).scalars().all()
    grants = session.execute(select(Grant).where(Grant.task_id == task_id)).scalars().all()
    reservations = session.execute(select(Reservation).where(Reservation.task_id == task_id)).scalars().all()
    jobs = session.execute(select(ScheduledJob).where(ScheduledJob.task_id == task_id)).scalars().all()
    plan = repo.current_plan(session, session.get(Task, task_id)) if session.get(Task, task_id) else None
    steps = repo.plan_steps(session, plan.plan_id) if plan else []

    def op_view(op: Operation) -> dict:
        return {
            "operation_id": op.operation_id,
            "tool": op.tool,
            "status": op.status,
            "amount_minor": op.amount_minor,
            "fee_minor": op.fee_minor,
            "provider_ref": op.provider_order_ref,
            "effect_confirmed": op.effect_confirmed,
        }

    total_spent = sum(op.amount_minor + op.fee_minor for op in operations if op.status == "succeeded")
    inflight = [op for op in operations if op.status in ("dispatching", "pending", "unknown")]

    return {
        "task_id": task_id,
        "generated_at": format_iso(utcnow()),
        "completed_operations": [op_view(op) for op in operations if op.status == "succeeded"],
        "unknown_operations": [op_view(op) for op in inflight if op.status == "unknown"],
        "pending_operations": [op_view(op) for op in inflight if op.status != "unknown"],
        "failed_operations": [op_view(op) for op in operations if op.status == "failed"],
        "not_submitted_steps": [
            {"node_id": s.node_id, "tool": s.tool, "status": s.status} for s in steps if s.status in ("pending", "stopped")
        ],
        "scheduled_jobs": [
            {"job_id": j.job_id, "job_type": j.job_type, "state": j.state, "due_at": format_iso(j.due_at)} for j in jobs
        ],
        "grants": [
            {
                "grant_id": g.grant_id,
                "status": g.status,
                "scope_hash": g.scope_hash,
                "used_count": g.used_count,
                "max_uses": g.max_uses,
                "expires_at": format_iso(g.expires_at),
            }
            for g in grants
        ],
        "reservations": [
            {
                "reservation_id": r.reservation_id,
                "account_id": r.account_id,
                "remaining_minor": r.remaining_minor,
                "captured_minor": r.captured_minor,
                "inflight_minor": r.inflight_minor,
                "status": r.status,
            }
            for r in reservations
        ],
        "funds": {
            "confirmed_spent_minor": total_spent,
            "inflight_minor": sum(op.amount_minor + op.fee_minor for op in inflight),
        },
        "warnings": [
            "未知结果必须先按原 operation_id 查询，不得重新发起支付",
            "运营不能代替用户完成强验证或确认授权",
        ],
    }


def operator_action(
    session: Session,
    operator_ctx: AuthContext,
    *,
    case_id: str,
    action: str,
    payload: dict | None = None,
) -> dict:
    if not operator_ctx.is_operator:
        raise FinPilotError("FORBIDDEN", "该操作需要独立运营角色。")
    case = session.execute(select(OperatorCase).where(OperatorCase.case_id == case_id).with_for_update()).scalar_one_or_none()
    if case is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该工单。")
    payload = payload or {}
    if action not in OPERATOR_ACTIONS:
        raise FinPilotError("PARAM_INVALID", "不支持的运营动作。")

    now = utcnow()
    result: dict = {"case_id": case_id, "action": action, "status": case.status}
    record = {"action": action, "by": operator_ctx.user_id, "at": format_iso(now), "payload": payload}

    if action == "requery_operation":
        operation_id = payload.get("operation_id")
        operation = session.get(Operation, operation_id) if operation_id else None
        if operation is None:
            raise FinPilotError("PARAM_INVALID", "缺少有效的操作编号。")
        try:
            from ..kernel.provider_adapter import get_adapter

            adapter = get_adapter(operation.provider_id)
            queried = adapter.query(stable_operation_ref=operation.operation_id)
            from ..kernel.provider_events import handle_provider_event
            from ..kernel.provider_adapter import event_from_result

            for event in queried.events or [event_from_result(queried, operation_id=operation.operation_id, event_type="transfer.query")]:
                handle_provider_event(
                    session,
                    provider_id=adapter.provider_id,
                    payload=event,
                    signature_ok=True,
                    source="operator_requery",
                    require_signature=False,
                )
            result["query_status"] = queried.status
        except FinPilotError as exc:
            result["query_status"] = exc.code
        result["note"] = "只做查询核验，不重新发起支付"

    elif action == "release_user_lock":
        # 只解除运营有权解除的控制（风控冻结），且必须有理由
        reason = payload.get("reason")
        if not reason:
            raise FinPilotError("PARAM_INVALID", "解除风控限制必须填写理由。")
        from ..models import CardControl

        control_id = payload.get("control_id")
        control = session.get(CardControl, control_id) if control_id else None
        if control is None or control.control_type != "risk_freeze":
            raise FinPilotError("PARAM_INVALID", "只能解除风控冻结类控制。")
        control.active = False
        control.released_by = operator_ctx.user_id
        control.released_at = now
        result["released_control"] = control_id
        result["note"] = "解除后仍需用户自行完成强验证才能恢复高风险操作"

    elif action == "approve_card_application":
        from ..domains.cards.service import approve_application

        application_id = payload.get("application_id")
        approve = bool(payload.get("approve", True))
        approved_limit = payload.get("approved_limit_minor")
        outcome = approve_application(
            session,
            operator_ctx,
            application_id,
            approve=approve,
            approved_limit_minor=approved_limit,
            reason=payload.get("reason", "模拟银行运营审批"),
        )
        result["application"] = outcome

    elif action == "close_case":
        case.status = "closed"
        result["status"] = "closed"

    elif action == "escalate":
        case.status = "escalated"
        result["status"] = "escalated"

    elif action == "retry_safe_step":
        if not payload.get("step_node_id"):
            raise FinPilotError("PARAM_INVALID", "缺少步骤编号。")
        result["note"] = "仅允许重试可安全重试的步骤；资金类步骤必须先核对未知结果"

    case.actions = list(case.actions or []) + [record]
    case.updated_at = now
    session.flush()
    return result


def list_cases(session: Session, operator_ctx: AuthContext, *, status: str | None = None) -> list[dict]:
    if not operator_ctx.is_operator:
        raise FinPilotError("FORBIDDEN", "该操作需要独立运营角色。")
    stmt = select(OperatorCase)
    if status:
        stmt = stmt.where(OperatorCase.status == status)
    rows = session.execute(stmt.order_by(OperatorCase.created_at.desc()).limit(100)).scalars().all()
    return [
        {
            "case_id": row.case_id,
            "task_id": row.task_id,
            "owner_user_id": row.owner_user_id,
            "reason": row.reason,
            "status": row.status,
            "created_at": format_iso(row.created_at),
            "handoff": row.handoff,
        }
        for row in rows
    ]