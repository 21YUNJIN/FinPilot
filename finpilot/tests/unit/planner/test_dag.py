"""DAG 校验与增量恢复的影响集合（V5 5.3/5.4、PLAN-03/PLAN-05、T24/T28/T42/T44）。"""

from __future__ import annotations

import pytest

from finpilot.errors import FinPilotError
from finpilot.orchestration.dag import (
    DagNode,
    affected_nodes,
    check_dag,
    completable_from,
    descendants,
    has_cycle,
    topological_order,
)
from finpilot.orchestration.recovery import remaining_budget_minor

pytestmark = [pytest.mark.planning]


def _chain() -> list[DagNode]:
    return [
        DagNode(node_id="quote_flowers", tool="merchant.quote", read_set=["merchant:flowers"]),
        DagNode(node_id="book_flowers", tool="merchant.book", dependencies=["quote_flowers"], read_set=["merchant:flowers", "account:account_salary"], write_set=["merchant_order:flowers"]),
        DagNode(node_id="quote_cake", tool="merchant.quote", read_set=["merchant:cake"]),
        DagNode(node_id="book_cake", tool="merchant.book", dependencies=["quote_cake"], read_set=["merchant:cake", "account:account_salary"], write_set=["merchant_order:cake"]),
        DagNode(node_id="aa_create", tool="aa.create", dependencies=["book_flowers", "book_cake"], read_set=["operation:op_flowers", "operation:op_cake"], write_set=["aa:request"]),
    ]


def test_valid_dag_passes_and_orders_dependencies_first():
    nodes = _chain()
    ok, errors = check_dag(nodes)
    assert ok and errors == []
    order = topological_order(nodes)
    assert order.index("quote_flowers") < order.index("book_flowers")
    assert order.index("book_flowers") < order.index("aa_create")
    assert order.index("book_cake") < order.index("aa_create")


def test_cycle_is_rejected():
    nodes = [
        DagNode(node_id="a", tool="x", dependencies=["c"]),
        DagNode(node_id="b", tool="x", dependencies=["a"]),
        DagNode(node_id="c", tool="x", dependencies=["b"]),
    ]
    ok, errors = check_dag(nodes)
    assert ok is False
    assert "CYCLE" in errors
    assert has_cycle(nodes)
    with pytest.raises(FinPilotError) as excinfo:
        topological_order(nodes)
    assert excinfo.value.code == "INFEASIBLE"


def test_missing_dependency_and_self_dependency_reported():
    nodes = [
        DagNode(node_id="a", tool="x", dependencies=["ghost"]),
        DagNode(node_id="b", tool="x", dependencies=["b"]),
    ]
    ok, errors = check_dag(nodes)
    assert ok is False
    assert any("MISSING_DEPENDENCY" in e for e in errors)
    assert any("SELF_DEPENDENCY" in e for e in errors)


def test_node_limit_is_enforced():
    nodes = [DagNode(node_id=f"n{i}", tool="x") for i in range(25)]
    ok, errors = check_dag(nodes, max_nodes=24)
    assert ok is False and "TOO_MANY_NODES" in errors


def test_duplicate_node_rejected():
    ok, errors = check_dag([DagNode(node_id="a", tool="x"), DagNode(node_id="a", tool="y")])
    assert ok is False and "DUPLICATE_NODE" in errors


def test_descendants_returns_dependency_closure():
    nodes = _chain()
    assert descendants(nodes, {"quote_flowers"}) == {"book_flowers", "aa_create"}
    assert descendants(nodes, {"book_flowers", "book_cake"}) == {"aa_create"}


def test_affected_nodes_selects_read_dependents_and_successors():
    """价格变化影响读取该报价的未完成节点及其后继；已完成节点不在集合内。"""
    nodes = _chain()
    unfinished = {n.node_id for n in nodes}
    affected = affected_nodes(nodes, {"merchant:cake"}, unfinished=unfinished)
    assert set(affected) == {"quote_cake", "book_cake", "aa_create"}

    # 已完成节点不可变：即使读集命中也不进入受影响集合
    unfinished_without_quote = {n.node_id for n in nodes if n.node_id != "quote_cake"}
    affected2 = affected_nodes(nodes, {"merchant:cake"}, unfinished=unfinished_without_quote)
    assert "quote_cake" not in affected2
    assert "book_cake" in affected2


def test_affected_nodes_unrelated_fact_changes_nothing():
    nodes = _chain()
    assert affected_nodes(nodes, {"product:far_away"}, unfinished={n.node_id for n in nodes}) == []


def test_completable_from_respects_completed_set():
    """已完成为起点计算"最终可执行闭包"；依赖不在图内的节点不可达。"""
    nodes = _chain()
    ready = completable_from(nodes, {"quote_flowers"})
    assert ready == {n.node_id for n in nodes}, "依赖链完整时，从任一节点出发最终都可达"

    # 依赖缺失（不在图内）的节点不可达
    broken = [
        DagNode(node_id="a", tool="x"),
        DagNode(node_id="b", tool="x", dependencies=["ghost"]),
        DagNode(node_id="c", tool="x", dependencies=["b"]),
    ]
    reachable = completable_from(broken, {"a"})
    assert reachable == {"a"}, "依赖不在图内的节点及其后继不得判定为可执行"


def test_remaining_budget_subtracts_confirmed_and_inflight():
    goal = {"hard_constraints": {"total_budget_minor": 80_000}}
    assert remaining_budget_minor(goal, 31_000, 0) == 49_000
    # 在途最大承诺金额也占用预算，避免把未确认支出当可花余量
    assert remaining_budget_minor(goal, 31_000, 45_000) == 4_000
    assert remaining_budget_minor({"hard_constraints": {"amount_minor": 10_000}}, 4_000, 0) == 6_000