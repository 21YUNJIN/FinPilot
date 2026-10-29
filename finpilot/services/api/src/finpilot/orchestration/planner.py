"""候选计划生成、约束求解与可解释排序（V5 5.3、PLAN-03、PLAN-04、S6-01）。

流程：
1. 由 GoalSpec 生成候选操作（受工具目录限制，不创造不存在的工具）。
2. 把依赖、预算（含费用）、应急金底线、到账期限、适当性、对象归属与授权范围写成可检查条件。
3. 有限候选上做确定性搜索（默认 24 节点 / 128 候选 / 2 秒）；超限返回 PLANNING_LIMIT，
   不得声称"数学上无解"。
4. 先排除违反硬约束的候选；剩余按（用户明确优先级、目标完成数、总费用、必要确认次数、
   计划变更量）字典序比较，并输出被排除方案的原因。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..ids import canonical_hash, new_id, scope_hash as compute_scope_hash
from ..models import (
    Card,
    CardControl,
    MerchantQuote,
    Plan,
    PlanStep,
    ProductNav,
    RiskProfile,
    Task,
    WealthProduct,
)
from ..money import format_minor
from ..security.policy import LEVEL_RANK, POLICY_VERSION, evaluate_policy
from ..timeutil import format_iso, utcnow
from ..kernel.identity import AuthContext
from ..kernel.snapshots import get_snapshot
from .dag import DagNode, check_dag, topological_order

PLANNER_VERSION = "planner-2026.10"


@dataclass
class Candidate:
    node_id: str
    tool: str
    typed_args: dict
    dependencies: list[str] = field(default_factory=list)
    read_set: list[str] = field(default_factory=list)
    write_set: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected_effects: dict = field(default_factory=dict)
    compensation_policy: dict = field(default_factory=dict)
    cost_minor: int = 0
    fee_minor: int = 0
    required_level: str = "GREEN"
    goal: str = ""
    optional: bool = False

    def total_minor(self) -> int:
        return self.cost_minor + self.fee_minor

    def to_node(self) -> DagNode:
        return DagNode(
            node_id=self.node_id,
            tool=self.tool,
            dependencies=list(self.dependencies),
            read_set=list(self.read_set),
            write_set=list(self.write_set),
        )


@dataclass
class PlanSolution:
    candidates: list[Candidate]
    completed_goals: list[str]
    total_minor: int
    total_fee_minor: int
    confirmations: int
    score: tuple
    trace: dict


DEFAULT_MERCHANT_OFFERS = [
    {"merchant_id": "merchant_flowers", "offer_code": "flowers_standard", "title": "生日鲜花（标准花束）", "category": "flowers", "amount_minor": 30_000, "fee_minor": 1_000, "cancel_fee_minor": 0, "refundable": True},
    {"merchant_id": "merchant_flowers", "offer_code": "flowers_deluxe", "title": "生日鲜花（豪华花束）", "category": "flowers", "amount_minor": 45_000, "fee_minor": 1_000, "cancel_fee_minor": 0, "refundable": True},
    {"merchant_id": "merchant_cake", "offer_code": "cake_standard", "title": "生日蛋糕（标准款）", "category": "cake", "amount_minor": 40_000, "fee_minor": 1_000, "cancel_fee_minor": 10_000, "refundable": False},
    {"merchant_id": "merchant_cake", "offer_code": "cake_deluxe", "title": "生日蛋糕（豪华款）", "category": "cake", "amount_minor": 52_000, "fee_minor": 1_000, "cancel_fee_minor": 15_000, "refundable": False},
    {"merchant_id": "merchant_restaurant", "offer_code": "restaurant_table", "title": "餐厅生日聚餐（6 人桌）", "category": "restaurant", "amount_minor": 30_000, "fee_minor": 0, "cancel_fee_minor": 5_000, "refundable": False},
]


def _merchant_offers(session: Session, category: str | None = None) -> list[dict]:
    """商户报价来源：优先使用提供方目录（唯一真源），再回退到库内报价缓存。

    计划只使用提供方真实存在的 (merchant_id, offer_code)，避免规划出无法执行的预订。
    """
    offers: list[dict] = []
    try:
        from ..adapters.merchant.client import DEFAULT_OFFERS

        offers = [
            {
                "merchant_id": str(item.get("merchant_id")),
                "offer_code": str(item.get("offer_code")),
                "title": str(item.get("title", "")),
                "category": str(item.get("category", "")),
                "amount_minor": int(item.get("amount_minor", 0)),
                "fee_minor": int(item.get("fee_minor", 0)),
                "cancel_fee_minor": int(item.get("cancel_fee_minor", 0)),
                "refundable": bool(item.get("refundable", False)),
                "inventory": int(item.get("inventory", 0)),
                "terms_version": str(item.get("terms_version", "2026.10")),
                "untrusted_text": item.get("untrusted_text"),
            }
            for item in DEFAULT_OFFERS
            if int(item.get("inventory", 0)) > 0
        ]
    except Exception:  # pragma: no cover - 适配器不可用时回退
        offers = []

    if not offers:
        quotes = session.execute(select(MerchantQuote).where(MerchantQuote.inventory > 0)).scalars().all()
        offers = [
            {
                "merchant_id": q.merchant_id,
                "offer_code": q.offer_code,
                "title": q.title,
                "category": (q.raw or {}).get("category", ""),
                "amount_minor": q.amount_minor,
                "fee_minor": q.fee_minor,
                "cancel_fee_minor": q.cancel_fee_minor,
                "refundable": q.refundable,
                "inventory": q.inventory,
                "terms_version": q.terms_version,
            }
            for q in quotes
        ]
    if not offers:
        offers = [dict(offer) for offer in DEFAULT_MERCHANT_OFFERS]
    if category:
        filtered = [offer for offer in offers if offer.get("category") == category]
        if filtered:
            return filtered
    return offers


def _service_goal_map(goal_spec: dict) -> dict[str, list[str]]:
    goals = goal_spec.get("goals", [])
    mapping: dict[str, list[str]] = {}
    if "book_flowers" in goals:
        mapping["flowers"] = ["book_flowers"]
    if "book_cake" in goals:
        mapping["cake"] = ["book_cake"]
    if "book_restaurant" in goals:
        mapping["restaurant"] = ["book_restaurant"]
    if not mapping and "plan_event" in goals:
        mapping = {"flowers": ["book_flowers"], "cake": ["book_cake"]}
    return mapping


def generate_candidates(session: Session, ctx: AuthContext, task: Task, goal_spec: dict, snapshot: dict) -> list[Candidate]:
    goals = set(goal_spec.get("goals", []))
    hard = goal_spec.get("hard_constraints", {})
    facts = goal_spec.get("facts", {})
    candidates: list[Candidate] = []
    account_id = (goal_spec.get("funding_sources") or [None])[0]

    def add(candidate: Candidate) -> None:
        candidates.append(candidate)

    if "transfer" in goals:
        amount = int(hard.get("amount_minor") or 0)
        payee_id = facts.get("payee_id")
        when = hard.get("execute_after")
        if when:
            add(
                Candidate(
                    node_id="schedule_transfer",
                    tool="schedule.create",
                    typed_args={
                        "payee_id": payee_id,
                        "source_account_id": account_id,
                        "amount_minor": amount,
                        "fee_minor": 0,
                        "execute_after": when,
                        "timezone": "Asia/Shanghai",
                        "memo": facts.get("memo", ""),
                    },
                    read_set=[f"account:{account_id}", "authorization:schedule", f"payee:{payee_id}"],
                    write_set=[f"schedule:{amount}", f"funds:{account_id}"],
                    preconditions=["future_authorization_valid", "daily_limit_recheck_on_due"],
                    expected_effects={"scheduled_transfer": True, "amount_minor": amount},
                    compensation_policy={"cancellable_before_dispatch": True, "fee_reservation": False},
                    cost_minor=amount,
                    goal="transfer",
                )
            )
        else:
            add(
                Candidate(
                    node_id="transfer",
                    tool="transfer.submit",
                    typed_args={
                        "payee_id": payee_id,
                        "source_account_id": account_id,
                        "amount_minor": amount,
                        "fee_minor": 0,
                        "memo": facts.get("memo", ""),
                    },
                    read_set=[f"account:{account_id}", f"payee:{payee_id}", "policy:daily_limit"],
                    write_set=[f"funds:{account_id}", f"ledger:transfer:{amount}"],
                    preconditions=["balance_sufficient", "payee_resolved", "daily_limit_graded"],
                    expected_effects={"transfer_out": True, "amount_minor": amount},
                    compensation_policy={"reversible": False, "refund_only_by_provider_terms": True},
                    cost_minor=amount,
                    goal="transfer",
                )
            )

    if "analyze_bill" in goals:
        add(
            Candidate(
                node_id="bill_report",
                tool="bill.report",
                typed_args={"period": facts.get("period"), "basis": facts.get("basis", "consumption")},
                read_set=["billing:transactions", "account:*"],
                write_set=[],
                preconditions=["data_cutoff_recorded"],
                expected_effects={"report_generated": True},
                cost_minor=0,
                required_level="GREEN",
                goal="analyze_bill",
            )
        )

    if "risk_assess" in goals:
        add(
            Candidate(
                node_id="risk_assess",
                tool="risk.assess",
                typed_args={"questionnaire_version": "2026.10", "answers": facts.get("answers", {})},
                read_set=["risk:profile"],
                write_set=["risk:profile"],
                expected_effects={"profile_updated": True},
                cost_minor=0,
                goal="risk_assess",
            )
        )

    if "compare_products" in goals:
        add(
            Candidate(
                node_id="wealth_compare",
                tool="wealth.compare",
                typed_args={
                    "budget_minor": int(hard.get("amount_minor") or hard.get("total_budget_minor") or 0),
                    "horizon_days": int(facts.get("horizon_days", 365)),
                    "preferences": goal_spec.get("preferences", []),
                },
                read_set=["wealth:products", "risk:profile"],
                write_set=[],
                expected_effects={"comparison_ready": True},
                cost_minor=0,
                required_level="GREEN",
                goal="compare_products",
            )
        )

    if "quant_risk_report" in goals:
        add(Candidate(node_id="quant_risk", tool="quant.risk_report",
                      typed_args={"asset_ids": facts.get("product_ids", [])},
                      read_set=["wealth:products"], write_set=[],
                      expected_effects={"risk_report_ready": True}, cost_minor=0,
                      required_level="GREEN", goal="quant_risk_report"))

    if "purchase_wealth" in goals:
        amount = int(hard.get("amount_minor") or 0)
        product_id = facts.get("product_id")
        add(
            Candidate(
                node_id="wealth_purchase",
                tool="wealth.purchase",
                typed_args={"product_id": product_id, "source_account_id": account_id, "amount_minor": amount},
                read_set=[f"account:{account_id}", f"product:{product_id}", "risk:profile"],
                write_set=[f"funds:{account_id}", f"holding:{product_id}"],
                preconditions=["assessment_valid", "suitability_passed", "product_terms_version"],
                expected_effects={"purchase_accepted": True, "amount_minor": amount},
                compensation_policy={"cooling_off_by_terms": True},
                cost_minor=amount,
                required_level="RED",
                goal="purchase_wealth",
            )
        )

    if "redeem_wealth" in goals:
        amount = int(hard.get("amount_minor") or 0)
        add(
            Candidate(
                node_id="wealth_redeem",
                tool="wealth.redeem",
                typed_args={"holding_id": facts.get("holding_id"), "source_account_id": account_id, "amount_minor": amount},
                read_set=[f"holding:{facts.get('holding_id')}", "calendar:trading"],
                write_set=[f"holding:{facts.get('holding_id')}", f"funds:{account_id}"],
                preconditions=["shares_available", "trading_day_window"],
                expected_effects={"redemption_accepted": True, "settles_later": True},
                cost_minor=0,
                required_level="RED",
                goal="redeem_wealth",
            )
        )

    if "card_status" in goals:
        add(Candidate(node_id="card_status", tool="card.status", typed_args={},
                      read_set=["card:owned"], write_set=[], expected_effects={"card_status_ready": True},
                      cost_minor=0, required_level="GREEN", goal="card_status"))

    if "card_apply" in goals:
        add(
            Candidate(
                node_id="card_apply",
                tool="card.apply",
                typed_args={"account_id": account_id, "requested_limit_minor": int(hard.get("requested_limit_minor") or 30_000)},
                read_set=[f"account:{account_id}", "card:applications"],
                write_set=["card:application"],
                expected_effects={"application_submitted": True},
                cost_minor=0,
                goal="card_apply",
            )
        )
    if "card_limit_change" in goals:
        add(
            Candidate(
                node_id="card_set_limit",
                tool="card.set_limit",
                typed_args={
                    "card_id": facts.get("card_id"),
                    "requested_limit_minor": int(hard.get("requested_limit_minor") or 0),
                    "direction": facts.get("direction", "increase"),
                },
                read_set=[f"card:{facts.get('card_id')}"],
                write_set=["card:application"],
                expected_effects={"limit_request_submitted": True},
                cost_minor=0,
                goal="card_limit_change",
            )
        )
    if "card_lost_report" in goals:
        add(
            Candidate(
                node_id="card_report_lost",
                tool="card.report_lost",
                typed_args={"card_id": facts.get("card_id"), "action": "lost"},
                read_set=[f"card:{facts.get('card_id')}"],
                write_set=[f"card:{facts.get('card_id')}"],
                expected_effects={"card_lost": True},
                cost_minor=0,
                required_level="RED",
                goal="card_lost_report",
            )
        )
    if "card_control" in goals:
        add(
            Candidate(
                node_id="card_control",
                tool="card.set_control",
                typed_args={
                    "card_id": facts.get("card_id"),
                    "control_type": facts.get("control_type", "user_lock"),
                    "action": facts.get("control_action", "add"),
                },
                read_set=[f"card:{facts.get('card_id')}"],
                write_set=[f"card:{facts.get('card_id')}:controls"],
                expected_effects={"controls_updated": True},
                cost_minor=0,
                goal="card_control",
            )
        )

    if "list_subscriptions" in goals:
        add(
            Candidate(
                node_id="subscription_list",
                tool="subscription.list",
                typed_args={"include_inferred": True},
                read_set=["subscription:contracts", "mandate:status", "billing:transactions"],
                write_set=[],
                expected_effects={"subscriptions_listed": True},
                cost_minor=0,
                required_level="YELLOW",
                goal="list_subscriptions",
            )
        )
    if "cancel_subscription" in goals:
        add(
            Candidate(
                node_id="subscription_cancel",
                tool="subscription.cancel",
                typed_args={"subscription_id": facts.get("subscription_id")},
                read_set=["subscription:contracts"],
                write_set=["subscription:contracts"],
                expected_effects={"merchant_contract_cancelled": True},
                cost_minor=0,
                goal="cancel_subscription",
            )
        )
    if "revoke_mandate" in goals:
        add(
            Candidate(
                node_id="mandate_revoke",
                tool="mandate.revoke",
                typed_args={"mandate_id": facts.get("mandate_id"), "subscription_id": facts.get("subscription_id")},
                read_set=["mandate:status"],
                write_set=["mandate:status"],
                expected_effects={"mandate_revoked": True},
                cost_minor=0,
                required_level="RED",
                goal="revoke_mandate",
            )
        )

    if "plan_event" in goals or any(g in goals for g in ("book_flowers", "book_cake", "book_restaurant")):
        service_map = _service_goal_map(goal_spec)
        book_nodes: list[str] = []
        for category, service_goals in service_map.items():
            offers = sorted(_merchant_offers(session, category), key=lambda o: (o["amount_minor"] + o["fee_minor"]))
            if not offers:
                continue
            offer = offers[0]
            quote_node = f"quote_{category}"
            book_node = f"book_{category}"
            add(
                Candidate(
                    node_id=quote_node,
                    tool="merchant.quote",
                    typed_args={"merchant_id": offer["merchant_id"], "offer_code": offer["offer_code"]},
                    read_set=[f"merchant:{offer['merchant_id']}", "inventory:*"],
                    write_set=[],
                    expected_effects={"quote_observed": True},
                    cost_minor=0,
                    required_level="GREEN",
                    goal=service_goals[0],
                )
            )
            add(
                Candidate(
                    node_id=book_node,
                    tool="merchant.book",
                    typed_args={
                        "merchant_id": offer["merchant_id"],
                        "offer_code": offer["offer_code"],
                        "quantity": 1,
                        "source_account_id": account_id,
                        "amount_minor": offer["amount_minor"],
                        "quoted_amount_minor": offer["amount_minor"],
                        "fee_minor": offer.get("fee_minor", 0),
                        "terms_version": offer.get("terms_version", "2026.10"),
                        "cancel_fee_minor": offer.get("cancel_fee_minor", 0),
                        "refundable": bool(offer.get("refundable", False)),
                    },
                    dependencies=[quote_node],
                    read_set=[f"merchant:{offer['merchant_id']}", f"quote:{offer['offer_code']}", f"account:{account_id}"],
                    write_set=[f"merchant_order:{offer['offer_code']}", f"funds:{account_id}"],
                    preconditions=["quote_not_expired", "inventory_available", "budget_allows_total"],
                    expected_effects={"booked": True, "amount_minor": offer["amount_minor"], "fee_minor": offer.get("fee_minor", 0)},
                    compensation_policy={
                        "cancellable": bool(offer.get("refundable", False)),
                        "cancel_fee_minor": offer.get("cancel_fee_minor", 0),
                        "non_refundable_cost_counts_in_budget": True,
                    },
                    cost_minor=offer["amount_minor"],
                    fee_minor=offer.get("fee_minor", 0),
                    required_level="RED",
                    goal=service_goals[0],
                )
            )
            book_nodes.append(book_node)

        if "create_aa_request" in goals and book_nodes:
            add(
                Candidate(
                    node_id="aa_create",
                    tool="aa.create",
                    typed_args={
                        "title": goal_spec.get("facts", {}).get("event_title", "生日聚会"),
                        "final_cost_minor": 0,
                        "participants": goal_spec.get("facts", {}).get("participants", []),
                        "initiator_included": True,
                        "depends_on_confirmed_costs": True,
                    },
                    dependencies=list(book_nodes),
                    read_set=[f"operation:{node}" for node in book_nodes],
                    write_set=["aa:request"],
                    preconditions=["confirmed_actual_costs"],
                    expected_effects={"aa_created": True},
                    compensation_policy={"refund_adjustment": True},
                    cost_minor=0,
                    goal="create_aa_request",
                )
            )
    return candidates


def _check_constraints(
    session: Session,
    ctx: AuthContext,
    candidates: list[Candidate],
    goal_spec: dict,
    snapshot: dict,
) -> list[str]:
    """硬约束检查：预算含费用、应急金底线、期限、适当性、归属、节点上限。"""
    hard = goal_spec.get("hard_constraints", {})
    reasons: list[str] = []

    nodes = [c.to_node() for c in candidates]
    ok, errors = check_dag(nodes, max_nodes=get_settings().planner_max_nodes)
    if not ok:
        reasons.extend(errors)

    budget = hard.get("total_budget_minor") or hard.get("amount_minor")
    total = sum(c.total_minor() for c in candidates if c.tool in ("merchant.book", "wealth.purchase"))
    if budget is not None and total > budget:
        reasons.append(f"BUDGET_EXCEEDED:{total}>{budget}")

    spendable_map = {account["account_id"]: account["spendable_minor"] for account in snapshot["accounts"]}
    spend_by_account: dict[str, int] = {}
    outflow = 0
    for candidate in candidates:
        if candidate.tool not in ("transfer.submit", "schedule.create", "merchant.book", "wealth.purchase"):
            continue
        outflow += candidate.total_minor()
        account_id = candidate.typed_args.get("source_account_id") or candidate.typed_args.get("account_id")
        if account_id:
            spend_by_account[account_id] = spend_by_account.get(account_id, 0) + candidate.total_minor()

    if spend_by_account:
        for account_id, amount in spend_by_account.items():
            limit = spendable_map.get(account_id)
            if limit is None:
                reasons.append(f"ACCOUNT_NOT_ACCESSIBLE:{account_id}")
            elif amount > limit:
                reasons.append(f"PROTECTED_FLOOR_OR_FUNDS:{account_id}:{amount}>{limit}")
    elif outflow > snapshot["group"]["spendable_minor"]:
        reasons.append(f"PROTECTED_FLOOR_OR_FUNDS:{outflow}>{snapshot['group']['spendable_minor']}")

    deadline = hard.get("delivery_deadline")
    execute_after = hard.get("execute_after")
    if deadline and execute_after and execute_after > deadline:
        reasons.append("DEADLINE_BEFORE_EXECUTION")

    for candidate in candidates:
        if candidate.tool == "wealth.purchase":
            product_id = candidate.typed_args.get("product_id")
            profile = session.execute(
                select(RiskProfile).where(RiskProfile.owner_user_id == ctx.user_id, RiskProfile.is_active.is_(True))
            ).scalars().first()
            product = session.get(WealthProduct, product_id) if product_id else None
            if product is None:
                reasons.append("PRODUCT_NOT_FOUND")
            elif profile is None:
                reasons.append("ASSESSMENT_REQUIRED")
            elif LEVEL_RANK_RISK(product.issuer_risk_level) > LEVEL_RANK_RISK(profile.risk_level):
                reasons.append(f"RISK_MISMATCH:{product.issuer_risk_level}>{profile.risk_level}")
    return reasons


def LEVEL_RANK_RISK(level: str) -> int:
    return {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}.get(level, 5)


def _score(solution: PlanSolution, goal_spec: dict) -> tuple:
    """字典序：用户明确优先级 → 目标完成数 → 总费用 → 必要确认次数 → 计划变更量。"""
    preferences = goal_spec.get("preferences", [])
    priority_penalty = 0
    order = [p.get("higher") for p in preferences if p.get("higher")]
    for goal in order:
        if goal and goal not in solution.completed_goals:
            priority_penalty += 1
    return (
        priority_penalty,
        -len(solution.completed_goals),
        solution.total_minor,
        solution.confirmations,
        0,
    )


def solve(
    session: Session,
    ctx: AuthContext,
    *,
    task: Task,
    goal_spec: dict,
    snapshot: dict | None = None,
) -> tuple[PlanSolution | None, dict]:
    """有限候选上的确定性搜索；返回 (最优可行解或 None, 求解轨迹)。"""
    settings = get_settings()
    started = time.monotonic()
    snapshot = snapshot or get_snapshot(session, ctx)
    candidates = generate_candidates(session, ctx, task, goal_spec, snapshot)
    trace: dict = {
        "planner_version": PLANNER_VERSION,
        "candidate_count": len(candidates),
        "max_candidates": settings.planner_max_candidates,
        "max_nodes": settings.planner_max_nodes,
        "timeout_ms": settings.planner_solve_timeout_ms,
        "excluded": [],
        "feasible": [],
    }

    if len(candidates) > settings.planner_max_candidates:
        raise FinPilotError(
            "PLANNING_LIMIT",
            "候选数量超过工程限额，规划中止（不代表任务无解）。",
            details={"reason_codes": ["candidate_limit"], "candidates": len(candidates)},
        )

    blocked = _check_constraints(session, ctx, candidates, goal_spec, snapshot)
    elapsed_ms = (time.monotonic() - started) * 1000
    trace["elapsed_ms"] = round(elapsed_ms, 2)
    if elapsed_ms > settings.planner_solve_timeout_ms:
        raise FinPilotError("PLANNING_LIMIT", "规划超时（不代表任务无解）。", details={"reason_codes": ["timeout"]})

    if blocked:
        for reason in blocked:
            trace["excluded"].append({"reason": reason, "scope": "whole_plan"})
        return None, trace

    confirmations = 0
    for candidate in candidates:
        decision = evaluate_policy(session, ctx, tool=candidate.tool, typed_args=candidate.typed_args, task_id=task.task_id)
        candidate.required_level = decision.required_level
        if decision.required_level in ("YELLOW", "RED"):
            confirmations += 1

    solution = PlanSolution(
        candidates=candidates,
        completed_goals=sorted({c.goal for c in candidates if c.tool not in ("merchant.quote", "bill.report")}),
        total_minor=sum(c.cost_minor for c in candidates),
        total_fee_minor=sum(c.fee_minor for c in candidates),
        confirmations=confirmations,
        score=(),
        trace=trace,
    )
    solution.score = _score(solution, goal_spec)
    trace["feasible"].append(
        {
            "nodes": len(candidates),
            "total_minor": solution.total_minor,
            "fee_minor": solution.total_fee_minor,
            "confirmations": confirmations,
            "score": list(solution.score),
        }
    )
    return solution, trace


def build_plan(
    session: Session,
    ctx: AuthContext,
    *,
    task: Task,
    goal_spec: dict,
    snapshot: dict | None = None,
) -> tuple[Plan, list[PlanStep]]:
    """生成可执行计划；不可行时保存原因，绝不擅自放宽硬约束。"""
    snapshot = snapshot or get_snapshot(session, ctx)
    solution, trace = solve(session, ctx, task=task, goal_spec=goal_spec, snapshot=snapshot)
    now = utcnow()

    if solution is None:
        plan = Plan(
            plan_id=new_id("plan"),
            task_id=task.task_id,
            revision=task.revision,
            status="infeasible",
            goal_snapshot=goal_spec,
            snapshot_ref={"observed_at": snapshot.get("observed_at"), "policy_version": snapshot.get("policy_version")},
            scope={},
            scope_hash="",
            required_level="YELLOW",
            feasible=False,
            infeasible_reasons=[item["reason"] for item in trace.get("excluded", [])],
            alternatives=_build_alternatives(goal_spec, snapshot),
            total_estimated_minor=0,
            total_fee_minor=0,
            created_at=now,
        )
        return plan, []

    ordered = topological_order([c.to_node() for c in solution.candidates])
    by_id = {c.node_id: c for c in solution.candidates}
    plan_id = new_id("plan")
    steps: list[PlanStep] = []
    for seq, node_id in enumerate(ordered):
        candidate = by_id[node_id]
        steps.append(
            PlanStep(
                step_id=new_id("step"),
                plan_id="",
                seq=seq,
                node_id=candidate.node_id,
                tool=candidate.tool,
                typed_args=candidate.typed_args,
                dependencies=candidate.dependencies,
                read_set=candidate.read_set,
                write_set=candidate.write_set,
                preconditions=candidate.preconditions,
                expected_effects=candidate.expected_effects,
                compensation_policy=candidate.compensation_policy,
                required_level=candidate.required_level,
                status="pending",
            )
        )

    required_level = max(
        (s.required_level for s in steps if s.required_level in LEVEL_RANK),
        key=lambda level: LEVEL_RANK.get(level, 0),
        default="GREEN",
    )
    scope = build_plan_scope(task, goal_spec, steps, snapshot, required_level=required_level, plan_id=plan_id)
    plan = Plan(
        plan_id=plan_id,
        task_id=task.task_id,
        revision=task.revision,
        status="proposed",
        goal_snapshot=goal_spec,
        snapshot_ref={"observed_at": snapshot.get("observed_at"), "policy_version": snapshot.get("policy_version")},
        scope=scope,
        scope_hash=scope["scope_hash"],
        required_level=required_level,
        feasible=True,
        infeasible_reasons=[],
        alternatives=[],
        total_estimated_minor=solution.total_minor,
        total_fee_minor=solution.total_fee_minor,
        created_at=now,
    )
    return plan, steps


def _build_alternatives(goal_spec: dict, snapshot: dict) -> list[dict]:
    """不可行时给出需要用户选择的可验证修改项，而不是放宽约束。"""
    hard = goal_spec.get("hard_constraints", {})
    alternatives: list[dict] = []
    budget = hard.get("total_budget_minor")
    spendable = snapshot["group"]["spendable_minor"]
    if budget and budget > spendable:
        alternatives.append(
            {
                "action": "lower_budget_or_add_funds",
                "message": f"预算 {format_minor(budget)} 元超过当前可支配金额 {format_minor(spendable)} 元（已扣除应急金）。",
            }
        )
    if hard.get("protected_floor_minor"):
        alternatives.append(
            {
                "action": "explicit_floor_change",
                "message": "应急金底线如需调整，必须单独确认，不能为了让计划通过而自动下调。",
            }
        )
    return alternatives


def build_plan_scope(
    task: Task,
    goal_spec: dict,
    steps: list[PlanStep],
    snapshot: dict,
    *,
    required_level: str,
    plan_id: str | None = None,
) -> dict:
    """计划级授权范围：与确认界面展示同源。plan_id 必须参与哈希，避免确认摘要与授权摘要不一致。"""
    write_steps = [s for s in steps if s.required_level in ("YELLOW", "RED")]
    amount = sum(int(s.typed_args.get("amount_minor") or 0) for s in write_steps)
    fee = sum(int(s.typed_args.get("fee_minor") or 0) for s in write_steps)
    deadline_hint = goal_spec.get("hard_constraints", {}).get("delivery_deadline")
    execute_after = goal_spec.get("hard_constraints", {}).get("execute_after")
    now = utcnow()
    deadline = now + timedelta(minutes=15)
    if execute_after:
        try:
            deadline = max(deadline, datetime.fromisoformat(str(execute_after).replace("Z", "+00:00")) + timedelta(minutes=30))
        except ValueError:
            pass
    merchants = sorted(
        {
            s.typed_args.get("merchant_id")
            for s in write_steps
            if s.typed_args.get("merchant_id")
        }
    )
    allowed_tools = sorted({s.tool for s in write_steps}) or ["none"]
    scope = {
        "kind": "scoped",
        "task_id": task.task_id,
        "plan_id": plan_id or task.plan_id,
        "plan_revision": task.revision,
        "allowed_tools": allowed_tools,
        "allowed_account_ids": goal_spec.get("funding_sources") or [a["account_id"] for a in snapshot["accounts"]],
        "max_amount_per_op_minor": max([int(s.typed_args.get("amount_minor") or 0) for s in write_steps] + [0]),
        "max_total_minor": max(amount + fee, 1),
        "max_uses": max(len(write_steps), 1),
        "deadline": deadline.isoformat(),
        "merchants": [{"merchant_id": m} for m in merchants],
        "products": sorted({s.typed_args.get("product_id") for s in write_steps if s.typed_args.get("product_id")}),
        "allowed_compensation": ["cancel_within_terms", "refund_after_delivery"],
        "required_level": required_level,
        "policy_version": POLICY_VERSION,
        "steps": [
            {
                "node_id": s.node_id,
                "tool": s.tool,
                "amount_minor": int(s.typed_args.get("amount_minor") or 0),
                "merchant_id": s.typed_args.get("merchant_id"),
                "payee_id": s.typed_args.get("payee_id"),
                "card_id": s.typed_args.get("card_id"),
                "product_id": s.typed_args.get("product_id"),
                "level": s.required_level,
            }
            for s in write_steps
        ],
        "summary": goal_spec.get("summary"),
        "created_at": format_iso(now),
    }
    scope["scope_hash"] = compute_scope_hash({k: v for k, v in scope.items() if k != "scope_hash"})
    return scope


def describe_plan(plan: Plan, steps: list[PlanStep]) -> dict:
    from .task_repository import plan_view

    return plan_view(None, plan, steps)
