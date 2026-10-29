"""DAG 结构校验与读写集合计算（V5 5.3、5.4、PLAN-03）。

- 拒绝循环、缺依赖、重复节点与超限节点数。
- read_set / write_set 用于增量恢复：变化事实与未完成节点的读集相交，再取其依赖后继闭包。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import FinPilotError

MAX_NODES_DEFAULT = 24


@dataclass
class DagNode:
    node_id: str
    tool: str
    dependencies: list[str] = field(default_factory=list)
    read_set: list[str] = field(default_factory=list)
    write_set: list[str] = field(default_factory=list)


def check_dag(nodes: list[DagNode], *, max_nodes: int = MAX_NODES_DEFAULT) -> tuple[bool, list[str]]:
    """返回 (是否合法, 错误码列表)。"""
    errors: list[str] = []
    if len(nodes) > max_nodes:
        errors.append("TOO_MANY_NODES")
    ids = [n.node_id for n in nodes]
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_NODE")
    known = set(ids)
    for node in nodes:
        for dep in node.dependencies:
            if dep not in known:
                errors.append(f"MISSING_DEPENDENCY:{node.node_id}->{dep}")
        if node.node_id in node.dependencies:
            errors.append(f"SELF_DEPENDENCY:{node.node_id}")
    if not errors and has_cycle(nodes):
        errors.append("CYCLE")
    return (not errors, errors)


def has_cycle(nodes: list[DagNode]) -> bool:
    graph = {n.node_id: list(n.dependencies) for n in nodes}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n.node_id: WHITE for n in nodes}

    def visit(node_id: str) -> bool:
        color[node_id] = GRAY
        for dep in graph.get(node_id, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                return True
            if color[dep] == WHITE and visit(dep):
                return True
        color[node_id] = BLACK
        return False

    return any(color[n.node_id] == WHITE and visit(n.node_id) for n in nodes)


def topological_order(nodes: list[DagNode]) -> list[str]:
    """返回依赖优先的稳定顺序；非法 DAG 抛错。"""
    ok, errors = check_dag(nodes)
    if not ok:
        raise FinPilotError("INFEASIBLE", "计划图不合法。", details={"reason_codes": errors})
    by_id = {n.node_id: n for n in nodes}
    ordered: list[str] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        for dep in by_id[node_id].dependencies:
            visit(dep)
        ordered.append(node_id)

    for node in sorted(nodes, key=lambda n: (len(n.dependencies), n.node_id)):
        visit(node.node_id)
    return ordered


def descendants(nodes: list[DagNode], node_ids: set[str]) -> set[str]:
    by_id = {n.node_id: n for n in nodes}
    children: dict[str, list[str]] = {n.node_id: [] for n in nodes}
    for node in nodes:
        for dep in node.dependencies:
            children.setdefault(dep, []).append(node.node_id)
    result: set[str] = set()
    stack = list(node_ids)
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in result:
                result.add(child)
                stack.append(child)
    return result


def affected_nodes(nodes: list[DagNode], changed_facts: set[str], *, unfinished: set[str] | None = None) -> list[str]:
    """变化事实 → 受影响未完成节点及其依赖后继闭包（不含已完成的不可变节点）。"""
    candidates = unfinished if unfinished is not None else {n.node_id for n in nodes}
    directly = {
        n.node_id
        for n in nodes
        if n.node_id in candidates and changed_facts.intersection(set(n.read_set))
    }
    closure = directly | descendants(nodes, directly)
    return sorted(node_id for node_id in closure if node_id in candidates)


def completable_from(nodes: list[DagNode], completed: set[str]) -> set[str]:
    """已完成集合 + 依赖满足的可执行闭包（用于恢复时判断可继续范围）。"""
    by_id = {n.node_id: n for n in nodes}
    ready: set[str] = set(completed)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.node_id in ready:
                continue
            if all(dep in ready for dep in node.dependencies):
                ready.add(node.node_id)
                changed = True
    return ready