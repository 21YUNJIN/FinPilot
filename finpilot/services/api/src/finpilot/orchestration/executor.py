"""统一执行器：所有写操作经同一入口，逐步重检（V5 5.4、API-02、PLAN-04、SYS-05）。

- 只执行已登记的计划步骤与工具目录内的工具。
- 每步提交前重新判定策略、资金、授权与版本；等待授权时保留可恢复状态。
- 领域写入由领域处理器完成，资金效果只能经内核端口产生。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..models import Grant, Operation, Plan, PlanStep, Task
from ..security.policy import (
    GREEN,
    PROVIDER_TOOLS,
    evaluate_policy,
)
from ..security.scope import describe_scope
from ..timeutil import utcnow
from ..kernel.grants import create_scoped_grant
from ..kernel.identity import AuthContext
from ..kernel.state_machine import task_status_for_outcome
from ..kernel.submission import OperationDraft, prepare_operation, submit_operation
from ..kernel.verifier import issue_challenge
from ..workers.outbox import drain
from . import task_repository as repo

logger = logging.getLogger(__name__)

READ_ONLY_TOOLS = frozenset(
    {
        "bill.report",
        "wealth.compare",
        "wealth.status",
        "card.status",
        "mandate.status",
        "merchant.quote",
        "merchant.status",
        "quant.risk_report",
        "aa.status",
        "schedule.status",
        "account.balance",
        "payee.resolve",
        "transfer.status",
        "subscription.list",
    }
)

_DOMAIN_MODULES = (
    # 领域包（__init__ 导出 TOOL_HANDLERS，覆盖各自 service 的注册）
    "finpilot.domains.cards",
    "finpilot.domains.aa",
    "finpilot.domains.subscriptions",
    "finpilot.domains.billing",
    "finpilot.domains.wealth_rules",
    "finpilot.domains.wealth_rules.assessment_ops",
    "finpilot.domains.wealth_orders",
    "finpilot.domains.merchant_orders",
    # 具体服务模块（若包未导出时兜底）
    "finpilot.domains.cards.service",
    "finpilot.domains.aa.service",
    "finpilot.domains.subscriptions.service",
    "finpilot.domains.billing.service",
    "finpilot.domains.wealth_rules.compare",
)


def tool_handlers() -> dict:
    """收集各领域登记的工具处理器（未实现的领域保持缺失，不伪造成功）。"""
    handlers: dict = {}
    for module_name in _DOMAIN_MODULES:
        try:
            module = __import__(module_name, fromlist=["TOOL_HANDLERS", "READ_HANDLERS"])
        except Exception:  # pragma: no cover - 领域尚未实现时跳过
            continue
        handlers.update(getattr(module, "TOOL_HANDLERS", {}) or {})
        handlers.update(getattr(module, "READ_HANDLERS", {}) or {})
    from .read_tools import READ_HANDLERS as _builtin_read_handlers

    handlers.update(_builtin_read_handlers)
    return handlers


def ensure_plan_grant(session: Session, ctx: AuthContext, task: Task, plan: Plan, steps: list[PlanStep]) -> dict:
    """确保计划具备可用授权；RED 返回挑战，YELLOW 直接签发范围授权。"""
    if plan.required_level == GREEN:
        return {"grant_id": None, "level": GREEN}

    existing = session.execute(
        select(Grant).where(
            Grant.owner_user_id == ctx.user_id,
            Grant.task_id == task.task_id,
            Grant.plan_revision == plan.revision,
            Grant.status == "active",
        )
    ).scalars().first()
    if existing is not None:
        return {"grant_id": existing.grant_id, "level": plan.required_level}

    # 授权范围必须与用户确认的 plan.scope 完全一致（哈希同源），不得在此处修补字段
    scope = dict(plan.scope)
    if plan.required_level == "RED":
        challenge = issue_challenge(
            session,
            ctx,
            scope=scope,
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
        )
        return {
            "grant_id": None,
            "challenge_id": challenge.challenge_id,
            "scope_hash": challenge.operation_scope_hash,
            "level": "RED",
            "scope_view": describe_scope(scope),
            "expires_at": challenge.expires_at.isoformat(),
        }

    grant = create_scoped_grant(
        session,
        ctx,
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        scope=scope,
        required_level=plan.required_level,
        max_uses=int(scope.get("max_uses", 1)),
        max_total_minor=int(scope.get("max_total_minor", 1)),
        expires_at=_deadline(scope),
    )
    session.flush()
    return {"grant_id": grant.grant_id, "level": plan.required_level, "scope_view": describe_scope(scope)}


def _deadline(scope: dict):
    from datetime import datetime

    raw = scope.get("deadline")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            pass
    return utcnow() + timedelta(minutes=15)


def ensure_task_reservation(session: Session, ctx: AuthContext, task: Task, plan: Plan, steps: list[PlanStep]) -> dict:
    """为任务预算建立独立资金预留：多步支出必须共享同一预算，不能各自单独通过底线检查。

    - 预留创建不改变 B，只减少可支配金额；捕获时 B 与 H 同减。
    - 计划金额超过当前可支配金额时抛出 PROTECTED_FLOOR_VIOLATION / RESERVATION_EXCEEDED，
      由执行器转为 replanning，绝不擅自下调应急金。
    """
    from ..kernel.reservations import create_reservation
    from ..models import Reservation

    existing = session.execute(
        select(Reservation).where(
            Reservation.task_id == task.task_id,
            Reservation.status.in_(("active", "partially_captured")),
        )
    ).scalars().first()
    if existing is not None:
        return {"reservation_id": existing.reservation_id, "created": False}

    spend_steps = [
        s
        for s in steps
        if s.tool in ("transfer.submit", "merchant.book", "wealth.purchase") and int(s.typed_args.get("amount_minor") or 0) > 0
    ]
    if not spend_steps:
        return {"reservation_id": None, "created": False}

    total = sum(int(s.typed_args.get("amount_minor") or 0) + int(s.typed_args.get("fee_minor") or 0) for s in spend_steps)
    account_id = next(
        (s.typed_args.get("source_account_id") for s in spend_steps if s.typed_args.get("source_account_id")),
        (plan.scope.get("allowed_account_ids") or [None])[0],
    )
    if not account_id:
        return {"reservation_id": None, "created": False}

    reservation = create_reservation(
        session,
        ctx,
        account_id=account_id,
        amount_minor=total,
        task_id=task.task_id,
        purpose="task_budget",
        long_lived=bool(plan.goal_snapshot.get("hard_constraints", {}).get("execute_after")),
        expires_at=None,
    )
    session.flush()
    return {"reservation_id": reservation.reservation_id, "created": True, "amount_minor": total}


def run_task(session: Session, ctx: AuthContext, task: Task, *, max_steps: int | None = None) -> dict:
    """按依赖顺序执行计划；遇到需要授权时停下并返回可确认的范围。"""
    plan = repo.current_plan(session, task)
    if plan is None:
        return {"status": "no_plan", "task_id": task.task_id}
    steps = repo.plan_steps(session, plan.plan_id)
    if not steps:
        repo.transition_task(session, task, "blocked", reason="empty_plan", payload={"plan_id": plan.plan_id})
        return {"status": "blocked", "task_id": task.task_id, "reason": "empty_plan"}

    grant_info = ensure_plan_grant(session, ctx, task, plan, steps)
    if grant_info.get("challenge_id"):
        repo.transition_task(
            session,
            task,
            "awaiting_authorization",
            reason="strong_verification_required",
            payload={"challenge_id": grant_info["challenge_id"]},
        )
        return {
            "status": "awaiting_authorization",
            "task_id": task.task_id,
            "plan_id": plan.plan_id,
            "challenge_id": grant_info["challenge_id"],
            "scope_hash": grant_info["scope_hash"],
            "scope_view": grant_info.get("scope_view"),
        }

    grant_id = grant_info.get("grant_id")
    try:
        reservation_info = ensure_task_reservation(session, ctx, task, plan, steps)
    except FinPilotError as exc:
        repo.transition_task(
            session,
            task,
            "replanning",
            reason="budget_reservation_failed",
            payload={"code": exc.code, "reasons": exc.details},
        )
        return {"status": "replanning", "task_id": task.task_id, "reason_codes": [exc.code], "details": exc.details}

    repo.transition_task(session, task, "running", reason="execution_started", payload={"plan_id": plan.plan_id})

    handlers = tool_handlers()
    completed_nodes = {s.node_id for s in steps if s.status == "completed"}
    waiting_steps: list[PlanStep] = []
    limit = max_steps or len(steps)
    executed = 0

    for step in steps:
        if executed >= limit:
            break
        if step.status in ("completed", "stopped"):
            continue
        if any(dep not in completed_nodes for dep in (step.dependencies or [])):
            continue

        try:
            outcome = _execute_step(session, ctx, task, plan, step, grant_id, handlers)
        except FinPilotError as exc:
            step.status = "failed"
            step.last_error = exc.code
            _settle_failed_operation(session, step.operation_id, exc.code)
            session.flush()
            if exc.code in ("GRANT_REQUIRED", "GRANT_INVALID", "GRANT_SCOPE_VIOLATION"):
                repo.transition_task(
                    session,
                    task,
                    "awaiting_authorization",
                    reason="reauthorization_required",
                    payload={"step": step.node_id, "code": exc.code},
                )
                return {
                    "status": "awaiting_authorization",
                    "task_id": task.task_id,
                    "plan_id": plan.plan_id,
                    "scope_hash": plan.scope_hash,
                    "scope_view": describe_scope(plan.scope),
                    "reason_codes": [exc.code],
                }
            if exc.code in ("PROTECTED_FLOOR_VIOLATION", "INSUFFICIENT_FUNDS"):
                repo.transition_task(
                    session,
                    task,
                    "replanning",
                    reason="constraint_violation",
                    payload={"step": step.node_id, "code": exc.code},
                )
                return {"status": "replanning", "task_id": task.task_id, "reason_codes": [exc.code]}
            continue

        if outcome.get("completed"):
            step.status = "completed"
            completed_nodes.add(step.node_id)
            executed += 1
        elif outcome.get("waiting"):
            # 等待提供方回执的步骤不阻塞其他**无依赖**步骤（V5 5.4）
            step.status = "waiting"
            waiting_steps.append(step)
            session.flush()
        else:
            step.status = "failed"
            step.last_error = outcome.get("reason", "unknown")
            executed += 1
        session.flush()

    if waiting_steps:
        repo.transition_task(
            session,
            task,
            "reconciling",
            reason="waiting_provider",
            payload={"operations": [s.operation_id for s in waiting_steps]},
        )
        return {
            "status": "reconciling",
            "task_id": task.task_id,
            "operation_ids": [s.operation_id for s in waiting_steps],
            "note": "在途结果未确认：先按原 operation_id 查询，不重复发起支付",
        }

    return _finalize(session, ctx, task, plan, steps)


def _settle_failed_operation(session: Session, operation_id: str | None, code: str) -> None:
    """领域处理器抛错时把已创建的 operation 收敛为 failed。

    只标记 step 而不处理 operation，会让 operation 永久停在 ``dispatching``：
    任务显示失败、operation 却像"在途"，既无法重试也无法对账。
    """
    if not operation_id:
        return
    operation = session.get(Operation, operation_id)
    if operation is None or operation.status in ("succeeded", "failed", "cancelled"):
        return
    operation.error_code = operation.error_code or code
    from ..kernel.submission import transition

    transition(session, operation, "failed", reason="handler_error", detail={"code": code})


def _execute_step(
    session: Session,
    ctx: AuthContext,
    task: Task,
    plan: Plan,
    step: PlanStep,
    grant_id: str | None,
    handlers: dict,
) -> dict:
    step.status = "running"
    session.flush()

    decision = evaluate_policy(session, ctx, tool=step.tool, typed_args=step.typed_args, task_id=task.task_id)
    if not decision.allow:
        step.status = "failed"
        step.last_error = "policy_denied"
        session.flush()
        return {"completed": False, "reason": "policy_denied", "reason_codes": decision.reason_codes}

    step.required_level = decision.required_level
    operation_id = step.operation_id or f"op_{task.task_id}_{step.node_id}"
    step.operation_id = operation_id

    existing = session.get(Operation, operation_id)
    if existing is not None and existing.status == "succeeded":
        session.flush()
        return {"completed": True, "operation_id": operation_id}

    draft = OperationDraft(
        tool=step.tool,
        typed_args=dict(step.typed_args),
        operation_id=operation_id,
        task_id=task.task_id,
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        source_account_id=step.typed_args.get("source_account_id"),
        amount_minor=int(step.typed_args.get("amount_minor") or 0),
        fee_minor=int(step.typed_args.get("fee_minor") or 0),
    )
    operation = prepare_operation(session, ctx, draft)
    session.flush()

    if operation.status in ("authorized", "awaiting_authorization"):
        operation = submit_operation(
            session,
            ctx,
            operation_id=operation.operation_id,
            grant_id=grant_id if operation.required_level != GREEN else None,
        )
        session.flush()

    if operation.tool in PROVIDER_TOOLS:
        from ..config import get_settings
        # HTTP provider effects must be dispatched by the worker only after the
        # request transaction (including its Outbox row) commits. A local
        # deterministic test adapter retains its existing synchronous path.
        if get_settings().provider_mode == "local":
            drain(session)
        session.refresh(operation)
        if operation.status == "succeeded":
            return {"completed": True, "operation_id": operation.operation_id}
        if operation.status in ("dispatching", "pending", "unknown"):
            return {"waiting": True, "operation_id": operation.operation_id}
        return {"completed": False, "reason": operation.error_code or operation.status, "operation_id": operation.operation_id}

    handler = handlers.get(operation.tool)
    if handler is None:
        # 领域尚未实现：如实标记能力待交付，不伪造成功
        operation.domain_result = {"type": "unavailable", "reason": "handler_not_implemented"}
        step.status = "failed"
        step.last_error = "capability_pending"
        session.flush()
        return {"completed": False, "reason": "capability_pending", "operation_id": operation.operation_id}

    # Older domain read handlers consume typed_args; write handlers consume Operation.
    # Adapt the declared signature at this single boundary rather than handing an ORM object to dict().
    import inspect
    parameters = list(inspect.signature(handler).parameters)
    argument = operation.typed_args if len(parameters) >= 3 and parameters[2] == "typed_args" else operation
    result = handler(session, ctx, argument)
    operation.domain_result = result or {}
    from ..kernel.submission import transition

    transition(session, operation, "succeeded", reason="domain_handler_completed", detail={"type": (result or {}).get("type")})
    session.flush()
    return {"completed": True, "operation_id": operation.operation_id, "result": result}


def _finalize(session: Session, ctx: AuthContext, task: Task, plan: Plan, steps: list[PlanStep]) -> dict:
    completed = [s for s in steps if s.status == "completed"]
    failed = [s for s in steps if s.status == "failed"]
    pending = [s for s in steps if s.status not in ("completed", "failed", "stopped")]
    goals = plan.goal_snapshot.get("goals", [])
    effect_steps = [s for s in steps if s.tool not in READ_ONLY_TOOLS]
    completed_effects = [s for s in effect_steps if s.status == "completed"]

    # 状态必须合法：running 不能直接进入 blocked（见 kernel/state_machine.py）
    if failed and completed_effects:
        status = "partially_succeeded"
    elif failed:
        status = "failed"
    elif pending:
        status = "awaiting_human" if completed_effects else "replanning"
    else:
        status = task_status_for_outcome(
            completed_goals=len(completed_effects),
            total_goals=len(effect_steps) or len(completed),
            any_effect=bool(completed_effects),
            blocked=False,
        )
        if status == "failed" and completed:
            status = "succeeded"

    summary = {
        "completed_steps": [s.node_id for s in completed],
        "failed_steps": [{"node_id": s.node_id, "reason": s.last_error} for s in failed],
        "pending_steps": [s.node_id for s in pending],
        "goals": goals,
        "completed_goals": sorted({s.expected_effects.get("goal") for s in completed if s.expected_effects.get("goal")}),
        "waiting_reason": _waiting_reason(pending, failed),
        "next_action": _next_action(status, pending, failed),
        "generated_at": utcnow().isoformat(),
    }
    task.result_summary = summary
    repo.transition_task(session, task, status, reason="execution_finished", payload={"summary": summary})
    session.flush()
    return {"status": status, "task_id": task.task_id, "summary": summary, "plan_id": plan.plan_id}


def _waiting_reason(pending: list[PlanStep], failed: list[PlanStep]) -> str | None:
    if pending:
        return f"等待前置步骤或外部结果：{', '.join(s.node_id for s in pending[:3])}"
    if failed:
        return f"存在失败步骤：{', '.join(s.node_id for s in failed[:3])}"
    return None


def _next_action(status: str, pending: list[PlanStep], failed: list[PlanStep]) -> str:
    if status == "succeeded":
        return "查看结果回执"
    if status == "partially_succeeded":
        return "确认已完成效果，并决定是否补偿或继续未完成部分"
    if status in ("blocked",) and pending:
        return "授权或补充信息后继续"
    if failed:
        return "查看失败原因，选择重试、替代方案或人工接管"
    return "无"


def resume_task(session: Session, ctx: AuthContext, task_id: str) -> dict:
    task = repo.get_task(session, ctx, task_id)
    if task.status in ("succeeded", "cancelled", "failed"):
        return {"status": task.status, "task_id": task_id, "resumed": False}
    return run_task(session, ctx, task)
