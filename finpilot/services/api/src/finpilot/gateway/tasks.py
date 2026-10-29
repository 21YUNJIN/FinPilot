"""任务、计划确认、强验证、取消与恢复（V5 16.1）。

约定：
- 写端点只接受服务端登记的 operation 引用与 scope_hash；客户端不能自带业务规则。
- 确认入口与验证入口只对已认证用户开放（x-agent-exposed: false）。
- 用户改意产生新修订，旧修订提交返回 VERSION_CONFLICT。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..envelope import ok
from ..errors import FinPilotError
from ..intent.compiler import IntentContext, blocking_clarifications, compile_goal, merge_clarification_answer
from ..kernel.grants import create_scoped_grant, revoke_grant
from ..kernel.identity import AuthContext
from ..kernel.snapshots import get_snapshot
from ..models import Grant, Operation, Plan, PlanStep, Task
from ..orchestration import task_repository as repo
from ..orchestration.executor import run_task
from ..orchestration.planner import build_plan, build_plan_scope
from ..orchestration.recovery import repair_plan
from ..security.audit import append_event
from ..security.scope import describe_scope
from ..timeutil import current_business_day, format_iso, parse_iso, utcnow
from .deps import current_auth, get_db

router = APIRouter(tags=["tasks"])


class CreateTaskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    channel: str = Field(default="app", pattern="^(app|im)$")
    reference_time: str | None = None


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)


class ConfirmRequest(BaseModel):
    scope_hash: str = Field(min_length=8, max_length=128)
    expected_revision: int = Field(ge=1)


class VerifyChallengeRequest(BaseModel):
    factor_response: str = Field(min_length=6, max_length=8)


class RepairRequest(BaseModel):
    event: dict
    expected_revision: int = Field(ge=1)


def _task_payload(session: Session, task: Task, *, extra: dict | None = None) -> dict:
    view = repo.task_view(session, task)
    view["messages"] = repo.task_messages(session, task.task_id)
    if extra:
        view.update(extra)
    return view


def _summarize_level(session: Session, ctx: AuthContext) -> dict:
    snapshot = get_snapshot(session, ctx)
    from ..kernel.limits import daily_usage

    return {"snapshot": snapshot, "daily_usage": daily_usage(session, ctx)}


def _compile_for_mode(session: Session, ctx: AuthContext, message: str, history: list, reference, *, task_id: str, mode: str) -> dict:
    context = IntentContext(session=session, auth=ctx, mode=mode)
    if mode == "mock":
        return compile_goal(context, message, history, reference)
    if mode != "live":
        raise FinPilotError("CAPABILITY_NOT_SUPPORTED", "冻结轨迹回放尚未实现。")
    from ..intent.model_client import classify_intent
    result = classify_intent(message, get_settings())
    goal = compile_goal(context, message, history, reference, model_hint=result.candidate)
    provenance = {"source": "online_model_candidate", "model": result.model,
                  "prompt_version": result.prompt_version, "prompt_sha256": result.prompt_sha256,
                  "elapsed_ms": result.elapsed_ms, "usage": result.usage,
                  "validated_by": "deterministic_compiler_and_security_kernel"}
    goal["interpretation"] = provenance
    append_event(session, event_type="model.intent_candidate", actor=ctx.user_id, actor_role="customer",
                 task_id=task_id, model_version=result.model,
                 payload={"scene": result.candidate.scene, "action": result.candidate.action,
                          "uncertain": result.candidate.uncertain, **provenance})
    return goal


@router.post("/v1/tasks")
def create_task(body: CreateTaskRequest, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    if get_settings().app_mode not in {"mock", "live"}:
        raise FinPilotError("CAPABILITY_NOT_SUPPORTED", "冻结轨迹回放尚未实现。")
    reference = parse_iso(body.reference_time) if body.reference_time else utcnow()
    task = repo.create_task(
        session,
        ctx,
        message=body.message,
        channel=body.channel,
        reference_time=reference,
        mode=get_settings().app_mode,
    )
    history = repo.task_messages(session, task.task_id)
    goal = _compile_for_mode(session, ctx, body.message, history, reference, task_id=task.task_id, mode=task.mode)
    goal["task_id"] = task.task_id
    goal["revision"] = task.revision
    task.goal_spec = goal
    task.clarifications = goal.get("unresolved", [])
    repo.add_message(session, task, role="assistant", channel=body.channel, content=goal.get("summary", ""), structured={"goal_spec": goal})
    session.flush()

    if "unsupported_request" in goal.get("goals", []):
        repo.transition_task(session, task, "blocked", reason="capability_not_supported", payload={"reason": goal.get("facts", {}).get("unsupported_reason")})
        return ok(_task_payload(session, task), task_id=task.task_id)

    if blocking_clarifications(goal):
        repo.transition_task(session, task, "clarifying", reason="missing_critical_fields")
        return ok(
            _task_payload(session, task),
            task_id=task.task_id,
            extra_provenance={"clarifications": len(blocking_clarifications(goal))},
        )

    return _plan_and_maybe_run(session, ctx, task)


def _plan_and_maybe_run(session: Session, ctx: AuthContext, task: Task, *, run: bool = True) -> dict:
    repo.transition_task(session, task, "planning", reason="goal_compiled")
    plan, steps = build_plan(session, ctx, task=task, goal_spec=task.goal_spec)
    repo.save_plan(session, task, plan, steps)
    if not plan.feasible:
        repo.transition_task(
            session,
            task,
            "blocked",
            reason="infeasible",
            payload={"reasons": plan.infeasible_reasons, "alternatives": plan.alternatives},
        )
        append_event(
            session,
            event_type="plan.infeasible",
            actor=ctx.user_id,
            actor_role="customer",
            task_id=task.task_id,
            payload={"reasons": plan.infeasible_reasons},
        )
        return ok(_task_payload(session, task), task_id=task.task_id)

    if plan.required_level == "GREEN":
        repo.transition_task(session, task, "ready", reason="no_authorization_needed")
        if run:
            result = run_task(session, ctx, task)
            payload = _task_payload(session, task, extra={"execution": result})
            return ok(payload, task_id=task.task_id)

    repo.transition_task(session, task, "awaiting_authorization", reason="authorization_needed")
    scope_view = describe_scope(plan.scope)
    append_event(
        session,
        event_type="plan.proposed",
        actor=ctx.user_id,
        actor_role="customer",
        task_id=task.task_id,
        payload={"plan_id": plan.plan_id, "scope_hash": plan.scope_hash, "level": plan.required_level},
    )
    return ok(
        _task_payload(session, task, extra={"scope_view": scope_view}),
        task_id=task.task_id,
        extra_provenance={"required_level": plan.required_level, "scope_hash": plan.scope_hash},
    )


@router.get("/v1/tasks")
def list_tasks(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    return ok({"items": repo.list_tasks(session, ctx)})


@router.get("/v1/tasks/{task_id}")
def get_task(task_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    task = repo.get_task(session, ctx, task_id)
    payload = _task_payload(session, task)
    operations = session.execute(select(Operation).where(Operation.task_id == task_id)).scalars().all()
    payload["operations"] = [
        {
            "operation_id": op.operation_id,
            "tool": op.tool,
            "status": op.status,
            "amount_minor": op.amount_minor,
            "fee_minor": op.fee_minor,
            "effect_confirmed": op.effect_confirmed,
            "provider_ref": op.provider_order_ref,
            "required_level": op.required_level,
            "pending_items": ["结果未知，正在查询原订单"] if op.status == "unknown" else [],
        }
        for op in operations
    ]
    payload["scene_summary"] = _summarize_level(session, ctx)
    return ok(payload, task_id=task_id)


@router.post("/v1/tasks/{task_id}/messages")
def append_message(
    task_id: str,
    body: MessageRequest,
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    task = repo.get_task(session, ctx, task_id)
    if task.mode != get_settings().app_mode:
        raise FinPilotError("CAPABILITY_NOT_SUPPORTED", "当前运行模式与任务创建模式不一致，请切回原模式或创建新任务。")
    repo.assert_expected_revision(task, body.expected_revision)
    repo.add_message(session, task, role="user", channel=task.channel, content=body.message)

    # 先尝试把它当作澄清回答
    goal = dict(task.goal_spec or {})
    answered = False
    pending = blocking_clarifications(goal)
    if pending and len(pending) == 1:
        candidate = pending[0]
        if candidate.get("options"):
            option_values = {str(o.get("value")) for o in candidate["options"]}
            if body.message.strip() in option_values:
                goal = merge_clarification_answer(goal, candidate["clarification_id"], body.message.strip())
                answered = True
        if not answered and candidate.get("field") in ("amount_minor", "total_budget_minor", "requested_limit_minor", "final_cost_minor"):
            from ..money import parse_amount_minor

            try:
                minor = parse_amount_minor(body.message)
                goal = merge_clarification_answer(goal, candidate["clarification_id"], minor)
                answered = True
            except FinPilotError:
                answered = False

    if answered:
        repo.bump_revision(session, task, reason="clarification_answered", payload={"field": pending[0].get("field")})
        goal["revision"] = task.revision
        task.goal_spec = goal
        task.clarifications = goal.get("unresolved", [])
        session.flush()
        if blocking_clarifications(goal):
            repo.transition_task(session, task, "clarifying", reason="still_missing_fields")
            return ok(_task_payload(session, task), task_id=task_id)
        if task.status == "clarifying":
            repo.transition_task(session, task, "planning", reason="clarification_complete")
        return _plan_and_maybe_run(session, ctx, task)

    # 否则视为改意：重新编译，保留已完成效果
    history = repo.task_messages(session, task_id)
    new_goal = _compile_for_mode(session, ctx, body.message, history, task.reference_time,
                                 task_id=task.task_id, mode=task.mode)
    repo.bump_revision(session, task, reason="user_changed_intent", payload={"new_goals": new_goal.get("goals")})
    new_goal["task_id"] = task.task_id
    new_goal["revision"] = task.revision
    task.goal_spec = new_goal
    task.clarifications = new_goal.get("unresolved", [])
    repo.add_message(session, task, role="assistant", channel=task.channel, content=new_goal.get("summary", ""), structured={"goal_spec": new_goal})
    session.flush()
    if "unsupported_request" in new_goal.get("goals", []):
        repo.transition_task(session, task, "blocked", reason="capability_not_supported", payload={"reason": new_goal.get("facts", {}).get("unsupported_reason")})
        return ok(_task_payload(session, task), task_id=task_id)
    if blocking_clarifications(new_goal):
        if task.status in {"awaiting_authorization", "blocked"}:
            repo.transition_task(session, task, "planning", reason="intent_changed_before_clarification")
        repo.transition_task(session, task, "clarifying", reason="missing_critical_fields_after_change")
        return ok(_task_payload(session, task), task_id=task_id)
    return _plan_and_maybe_run(session, ctx, task)


@router.post("/v1/plans/{plan_id}/confirm")
def confirm_plan(
    plan_id: str,
    body: ConfirmRequest,
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该计划。")
    task = repo.get_task(session, ctx, plan.task_id)
    repo.assert_expected_revision(task, body.expected_revision)

    if task.plan_id != plan.plan_id or plan.revision != task.revision:
        raise FinPilotError("VERSION_CONFLICT", "该计划已过期，请查看并确认当前计划。",
                            details={"reason_codes": ["stale_plan"]})

    if plan.scope_hash != body.scope_hash:
        raise FinPilotError(
            "VERSION_CONFLICT",
            "确认内容与服务器计划不一致，请刷新后重新确认。",
            details={"reason_codes": ["scope_hash_mismatch"]},
        )
    if plan.required_level == "GREEN":
        return ok({"grant_id": None, "level": "GREEN", "note": "纯查询计划不需要额外确认"}, task_id=task.task_id)

    steps = repo.plan_steps(session, plan.plan_id)
    from ..orchestration.executor import ensure_plan_grant

    grant_info = ensure_plan_grant(session, ctx, task, plan, steps)
    append_event(
        session,
        event_type="plan.confirmed",
        actor=ctx.user_id,
        actor_role="customer",
        task_id=task.task_id,
        payload={
            "plan_id": plan.plan_id,
            "scope_hash": plan.scope_hash,
            "level": plan.required_level,
            "grant_id": grant_info.get("grant_id"),
            "challenge_id": grant_info.get("challenge_id"),
        },
    )

    if grant_info.get("challenge_id"):
        return ok(
            {
                "status": "challenge_required",
                "challenge_id": grant_info["challenge_id"],
                "scope_hash": grant_info["scope_hash"],
                "expires_at": grant_info["expires_at"],
                "level": "RED",
                "scope_view": grant_info.get("scope_view"),
                "note": "需要完成强验证（独立 TOTP 因子），验证码不进入对话与日志",
            },
            task_id=task.task_id,
        )

    repo.transition_task(session, task, "ready", reason="authorization_granted", payload={"grant_id": grant_info.get("grant_id")})
    result = run_task(session, ctx, task)
    return ok(
        _task_payload(session, task, extra={"execution": result, "grant_id": grant_info.get("grant_id")}),
        task_id=task.task_id,
    )


@router.post("/v1/auth/challenges/{challenge_id}/verify")
def verify_challenge_endpoint(
    challenge_id: str,
    body: VerifyChallengeRequest,
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    from ..kernel.verifier import verify_challenge
    from ..models import AuthChallenge

    result = verify_challenge(session, ctx, challenge_id=challenge_id, factor_response=body.factor_response)
    challenge = session.get(AuthChallenge, challenge_id)
    append_event(
        session,
        event_type="auth.challenge_verified",
        actor=ctx.user_id,
        actor_role="customer",
        task_id=(challenge.scope or {}).get("task_id") if challenge else None,
        grant_id=result["grant"]["grant_id"],
        payload={"challenge_id": challenge_id, "factor": "totp", "scope_hash": result["grant"]["scope_hash"]},
    )
    task_id = (challenge.scope or {}).get("task_id") if challenge else None
    extra: dict = {}
    if task_id:
        task = repo.get_task(session, ctx, task_id)
        repo.transition_task(session, task, "ready", reason="strong_verification_passed")
        extra["execution"] = run_task(session, ctx, task)
        extra["task"] = _task_payload(session, task)
    return ok({"verification": result, **extra}, task_id=task_id)


@router.post("/v1/tasks/{task_id}/resume")
def resume_task_endpoint(task_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..orchestration.executor import resume_task

    result = resume_task(session, ctx, task_id)
    task = repo.get_task(session, ctx, task_id)
    return ok(_task_payload(session, task, extra={"execution": result}), task_id=task_id)


@router.post("/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    task = repo.get_task(session, ctx, task_id)
    inflight = session.execute(
        select(Operation).where(Operation.task_id == task_id, Operation.status.in_(("dispatching", "pending", "unknown")))
    ).scalars().all()
    stopped_grant_effects: list[dict] = []
    grants = session.execute(select(Grant).where(Grant.task_id == task_id, Grant.status.in_(("active", "pending")))).scalars().all()
    for grant in grants:
        stopped_grant_effects.append(revoke_grant(session, ctx, grant_id=grant.grant_id))

    plan = repo.current_plan(session, task)
    stopped_steps: list[str] = []
    if plan:
        for step in repo.plan_steps(session, plan.plan_id):
            if step.status == "pending":
                step.status = "stopped"
                stopped_steps.append(step.node_id)

    if inflight:
        repo.transition_task(session, task, "reconciling", reason="cancel_with_inflight_effects", payload={"inflight": [op.operation_id for op in inflight]})
        status = "reconciling"
    else:
        repo.transition_task(session, task, "cancelled", reason="user_cancelled")
        status = "cancelled"

    append_event(
        session,
        event_type="task.cancelled",
        actor=ctx.user_id,
        actor_role="customer",
        task_id=task_id,
        payload={"inflight": [op.operation_id for op in inflight], "stopped_steps": stopped_steps},
    )
    return ok(
        {
            "task_id": task_id,
            "status": status,
            "stopped_steps": stopped_steps,
            "inflight_operations": [
                {"operation_id": op.operation_id, "status": op.status, "amount_minor": op.amount_minor} for op in inflight
            ],
            "note": "撤销只停止尚未提交的动作；已提交或结果未知的操作继续核验，不承诺撤回成功",
        },
        task_id=task_id,
    )


@router.post("/v1/grants/{grant_id}/revoke")
def revoke_grant_endpoint(grant_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    result = revoke_grant(session, ctx, grant_id=grant_id)
    append_event(
        session,
        event_type="grant.revoked",
        actor=ctx.user_id,
        actor_role="customer",
        grant_id=grant_id,
        payload={"stopped_steps": result["stopped_steps"], "inflight": result["inflight_operations"]},
    )
    return ok(result)


@router.post("/v1/tasks/{task_id}/repair")
def repair_task(
    task_id: str,
    body: RepairRequest,
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    task = repo.get_task(session, ctx, task_id)
    repo.assert_expected_revision(task, body.expected_revision)
    outcome = repair_plan(session, ctx, task_id=task_id, event=body.event)
    repo.transition_task(session, task, "planning", reason="incremental_replan", payload={"affected": outcome.affected})
    payload = _task_payload(
        session,
        task,
        extra={
            "repair": {
                "affected_nodes": outcome.affected,
                "retained_nodes": outcome.retained,
                "blocked_nodes": outcome.blocked,
                "reason": outcome.reason,
                "requires_reauthorization": outcome.requires_reauthorization,
                "diff": outcome.diff,
            }
        },
    )
    return ok(payload, task_id=task_id)


@router.get("/v1/operations/{operation_id}")
def get_operation_endpoint(operation_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..kernel.submission import operation_view

    return ok(operation_view(session, ctx, operation_id), operation_id=operation_id)


@router.get("/v1/operations")
def list_operations(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db), limit: int = 50):
    rows = session.execute(
        select(Operation).where(Operation.owner_user_id == ctx.user_id).order_by(Operation.created_at.desc()).limit(min(limit, 200))
    ).scalars().all()
    return ok(
        {
            "items": [
                {
                    "operation_id": op.operation_id,
                    "tool": op.tool,
                    "status": op.status,
                    "amount_minor": op.amount_minor,
                    "fee_minor": op.fee_minor,
                    "created_at": format_iso(op.created_at),
                }
                for op in rows
            ]
        }
    )


@router.get("/v1/receipts/{receipt_id}")
def get_receipt(receipt_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..models import Receipt

    receipt = session.get(Receipt, receipt_id)
    if receipt is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该回执。")
    operation = session.get(Operation, receipt.operation_id)
    if operation is None or (ctx.is_customer and operation.owner_user_id != ctx.user_id):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该回执。")
    return ok(
        {
            "receipt_id": receipt.receipt_id,
            "operation_id": receipt.operation_id,
            "provider_ref": receipt.provider_ref,
            "source": receipt.source,
            "verified_status": receipt.verified_status,
            "received_at": format_iso(receipt.received_at),
            "payload_hash": receipt.payload_hash,
        }
    )


@router.get("/v1/meta/tools")
def list_tools():
    from ..security.policy import policy_catalog, POLICY_VERSION

    catalog = policy_catalog()
    return ok({"policy_version": POLICY_VERSION, "tools": catalog, "agent_exposed": [t["tool"] for t in catalog if t["agent_exposed"]]})


@router.get("/v1/meta/policy")
def policy_meta():
    from ..config import get_settings

    settings = get_settings()
    return ok(
        {
            "policy_version": "policy-2026.10",
            "transfer_daily_yellow_limit_minor": settings.transfer_daily_yellow_limit_minor,
            "challenge_ttl_seconds": settings.challenge_ttl_seconds,
            "challenge_max_attempts": settings.challenge_max_attempts,
            "grant_exact_ttl_seconds": settings.grant_exact_ttl_seconds,
            "mode": settings.app_mode,
            "notes": [
                "1000 元是验证等级边界，不是转账最高限额",
                "恰好 100000 分为 YELLOW，超过为 RED；手续费不计入本金累计",
                "订阅查询保留官方 YELLOW 例外",
            ],
        }
    )
