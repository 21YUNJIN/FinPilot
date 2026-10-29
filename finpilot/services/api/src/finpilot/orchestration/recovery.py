"""增量恢复：变化事实 → 受影响未完成节点 → 新修订（V5 5.4、PLAN-05、S6-04、T28/T44）。

规则：
- 已完成节点不可变，不重新生成具有相同目的的新支付。
- 在途未知节点作为待核验边界，先查询；未知金额按最大承诺额占用剩余预算。
- 局部重规划后，所有保留节点仍要通过新硬约束的全量校验。
- 新修订不能改变历史订单；超出原授权范围的变化必须重新确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..ids import new_id
from ..models import Grant, Operation, Plan, PlanStep, Reservation, Task
from ..money import format_minor
from ..timeutil import utcnow
from ..kernel.identity import AuthContext
from ..kernel.snapshots import get_snapshot
from . import task_repository as repo
from .dag import DagNode, affected_nodes, completable_from
from .planner import build_plan_scope, generate_candidates

INFLIGHT_STATUSES = ("dispatching", "pending", "unknown")
COMPLETED_STEP_STATUSES = ("completed",)


@dataclass
class RepairOutcome:
    plan: Plan
    steps: list[PlanStep]
    affected: list[str]
    retained: list[str]
    blocked: list[str]
    reason: str
    requires_reauthorization: bool
    diff: dict


def changed_facts_from_event(event: dict) -> set[str]:
    """把变化事件映射为 read_set 可匹配的事实键。"""
    facts: set[str] = set()
    kind = event.get("type")
    if kind in ("price_change", "quote_changed"):
        merchant = event.get("merchant_id")
        offer = event.get("offer_code")
        if merchant:
            facts.add(f"merchant:{merchant}")
        if offer:
            facts.add(f"quote:{offer}")
        facts.add("inventory:*")
    elif kind == "out_of_stock":
        facts.add("inventory:*")
        if event.get("merchant_id"):
            facts.add(f"merchant:{event['merchant_id']}")
    elif kind == "balance_change":
        facts.add(f"account:{event.get('account_id')}")
    elif kind == "terms_version_change":
        facts.add(f"product:{event.get('product_id')}")
    elif kind == "policy_change":
        facts.add("policy:daily_limit")
        facts.add("authorization:*")
    elif kind == "budget_change":
        facts.add("goal:budget")
    elif kind == "card_status_change":
        facts.add(f"card:{event.get('card_id')}")
    elif kind == "user_intent_change":
        facts.add("goal:*")
    if event.get("facts"):
        facts.update(event["facts"])
    return facts


def confirmed_costs(session: Session, task_id: str) -> int:
    rows = session.execute(
        select(Operation).where(Operation.task_id == task_id, Operation.status == "succeeded")
    ).scalars().all()
    return sum(op.amount_minor + op.fee_minor for op in rows)


def inflight_max_costs(session: Session, task_id: str) -> int:
    rows = session.execute(
        select(Operation).where(Operation.task_id == task_id, Operation.status.in_(INFLIGHT_STATUSES))
    ).scalars().all()
    return sum(op.amount_minor + op.fee_minor for op in rows)


def remaining_budget_minor(goal_spec: dict, confirmed_cost_minor: int, inflight_max_minor: int) -> int:
    """剩余预算 = 目标预算 − 已确认成本 − 在途最大承诺金额。"""
    budget = int(
        (goal_spec.get("hard_constraints") or {}).get("total_budget_minor")
        or (goal_spec.get("hard_constraints") or {}).get("amount_minor")
        or 0
    )
    return budget - confirmed_cost_minor - inflight_max_minor


def repair_plan(session: Session, ctx: AuthContext, *, task_id: str, event: dict) -> RepairOutcome:
    task = repo.get_task(session, ctx, task_id)
    plan = repo.current_plan(session, task)
    if plan is None:
        raise FinPilotError("PARAM_INVALID", "该任务还没有计划，无法增量修复。")

    steps = repo.plan_steps(session, plan.plan_id)
    nodes = [
        DagNode(node_id=s.node_id, tool=s.tool, dependencies=list(s.dependencies or []), read_set=list(s.read_set or []), write_set=list(s.write_set or []))
        for s in steps
    ]
    completed = {s.node_id for s in steps if s.status in COMPLETED_STEP_STATUSES}
    unfinished = {s.node_id for s in steps if s.status not in COMPLETED_STEP_STATUSES + ("stopped",)}
    inflight = {s.node_id for s in steps if s.status == "waiting"}

    changed = changed_facts_from_event(event)
    affected = affected_nodes(nodes, changed, unfinished=unfinished - inflight)
    retained = sorted(completed | inflight)

    goal_spec = dict(plan.goal_snapshot)
    hard = dict(goal_spec.get("hard_constraints") or {})
    if event.get("type") == "budget_change" and event.get("new_budget_minor") is not None:
        hard["total_budget_minor"] = int(event["new_budget_minor"])
    if event.get("type") == "priority_change" and event.get("preferences"):
        goal_spec["preferences"] = event["preferences"]
    goal_spec["hard_constraints"] = hard

    confirmed = confirmed_costs(session, task_id)
    inflight_cost = inflight_max_costs(session, task_id)
    remaining = remaining_budget_minor(goal_spec, confirmed, inflight_cost)
    snapshot = get_snapshot(session, ctx)

    diff = {
        "event": event,
        "changed_facts": sorted(changed),
        "affected_nodes": affected,
        "retained_nodes": retained,
        "inflight_nodes": sorted(inflight),
        "confirmed_cost_minor": confirmed,
        "inflight_max_cost_minor": inflight_cost,
        "remaining_budget_minor": remaining,
        "previous_plan_id": plan.plan_id,
        "previous_revision": plan.revision,
    }

    budget = hard.get("total_budget_minor")
    if budget is not None and confirmed > budget:
        # 既成事实与新约束不兼容：准确说明，不伪造回退
        new_plan = _persist_revision(session, task, plan, goal_spec, [], status="infeasible")
        new_plan.infeasible_reasons = [f"IRREVERSIBLE_COST_EXCEEDS_BUDGET:{confirmed}>{budget}"]
        new_plan.alternatives = [
            {
                "action": "accept_actual_cost_or_raise_budget",
                "message": f"已发生不可撤销成本 {format_minor(confirmed)} 元，超过新预算 {format_minor(budget)} 元；"
                "系统已停止后续支出，但不会伪造回退。",
            }
        ]
        session.flush()
        return RepairOutcome(
            plan=new_plan,
            steps=[],
            affected=affected,
            retained=retained,
            blocked=[],
            reason="irreversible_cost_exceeds_budget",
            requires_reauthorization=True,
            diff=diff,
        )

    candidates = generate_candidates(session, ctx, task, goal_spec, snapshot)
    keep_nodes = set(affected)
    new_candidates = [c for c in candidates if c.node_id in keep_nodes or not affected]
    if not new_candidates:
        new_candidates = [c for c in candidates if c.node_id not in completed]

    total = sum(c.total_minor() for c in new_candidates)
    if budget is not None and total + confirmed > budget:
        new_plan = _persist_revision(session, task, plan, goal_spec, [], status="infeasible")
        new_plan.infeasible_reasons = [f"REMAINING_BUDGET_INSUFFICIENT:{total + confirmed}>{budget}"]
        new_plan.alternatives = [
            {
                "action": "choose_substitute_or_raise_budget",
                "message": f"剩余可执行动作合计 {format_minor(total)} 元，加上已发生 {format_minor(confirmed)} 元将超过预算。",
            }
        ]
        session.flush()
        return RepairOutcome(
            plan=new_plan,
            steps=[],
            affected=affected,
            retained=retained,
            blocked=[],
            reason="remaining_budget_insufficient",
            requires_reauthorization=True,
            diff=diff,
        )

    new_steps: list[PlanStep] = []
    for seq, candidate in enumerate(sorted(new_candidates, key=lambda c: (len(c.dependencies), c.node_id))):
        new_steps.append(
            PlanStep(
                step_id=new_id("step"),
                plan_id="",
                seq=seq,
                node_id=candidate.node_id,
                tool=candidate.tool,
                typed_args=candidate.typed_args,
                dependencies=[d for d in candidate.dependencies if d in {c.node_id for c in new_candidates}],
                read_set=candidate.read_set,
                write_set=candidate.write_set,
                preconditions=candidate.preconditions,
                expected_effects=candidate.expected_effects,
                compensation_policy=candidate.compensation_policy,
                required_level=candidate.required_level,
                status="pending",
            )
        )

    new_plan = _persist_revision(session, task, plan, goal_spec, new_steps, status="proposed")
    required_level = max((s.required_level for s in new_steps), key=lambda lv: {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(lv, 0), default="GREEN")
    scope = build_plan_scope(task, goal_spec, new_steps, snapshot, required_level=required_level, plan_id=new_plan.plan_id)
    new_plan.scope = scope
    new_plan.scope_hash = scope["scope_hash"]
    new_plan.required_level = required_level
    new_plan.total_estimated_minor = sum(s.typed_args.get("amount_minor") or 0 for s in new_steps)
    new_plan.total_fee_minor = sum(s.typed_args.get("fee_minor") or 0 for s in new_steps)

    session.flush()
    return RepairOutcome(
        plan=new_plan,
        steps=new_steps,
        affected=affected,
        retained=retained,
        blocked=sorted(inflight),
        reason="incremental_replan",
        requires_reauthorization=required_level == "RED" or bool(affected),
        diff=diff,
    )


def _persist_revision(
    session: Session,
    task: Task,
    previous: Plan,
    goal_spec: dict,
    steps: list[PlanStep],
    *,
    status: str,
) -> Plan:
    new_plan = Plan(
        plan_id=new_id("plan"),
        task_id=task.task_id,
        revision=previous.revision + 1,
        status=status,
        goal_snapshot=goal_spec,
        snapshot_ref=previous.snapshot_ref,
        scope={},
        scope_hash="",
        required_level="YELLOW",
        feasible=status != "infeasible",
        infeasible_reasons=[],
        alternatives=[],
        total_estimated_minor=0,
        total_fee_minor=0,
        created_at=utcnow(),
    )
    session.add(new_plan)
    session.flush()
    for step in steps:
        step.plan_id = new_plan.plan_id
        session.add(step)
    task.plan_id = new_plan.plan_id
    repo.bump_revision(session, task, reason="replan", payload={"new_plan_id": new_plan.plan_id, "reason": status})
    session.flush()
    return new_plan


def reconcile_before_repair(session: Session, ctx: AuthContext, task_id: str) -> dict:
    """修复前先用原操作编号核对未知结果，避免重复支出。"""
    try:
        from ..workers.reconciliation import reconcile_unknown_operations
    except Exception:  # pragma: no cover - 对账模块尚未实现
        return {"checked": 0, "note": "reconciliation_unavailable"}
    result = reconcile_unknown_operations(session)
    return result


def can_continue_after_cancel(session: Session, task: Task) -> list[str]:
    """撤销授权后仍可继续的节点（已提交/未知不受撤销影响）。"""
    plan = repo.current_plan(session, task)
    if plan is None:
        return []
    steps = repo.plan_steps(session, plan.plan_id)
    if not steps:
        return []
    nodes = [DagNode(node_id=s.node_id, tool=s.tool, dependencies=list(s.dependencies or [])) for s in steps]
    completed = {s.node_id for s in steps if s.status == "completed"}
    return sorted(completable_from(nodes, completed) - completed)