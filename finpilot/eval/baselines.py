"""基线与消融的**显式配置开关**（EVAL-02）。

原则
----
1. 所有差异都写成可读的配置字段（见 :data:`CONFIGURATIONS`），不在运行器里散落 if；
2. 消融**只削弱一个维度**，其余维度保持与 FULL 相同——不做"故意做弱的基线"：
   - A2（故障后全量重规划）仍保留幂等、已完成效果、授权与资金检查，只是恢复策略从
     "受影响子图增量重规划"换成"整图重新生成"；
   - A3（仅精确逐操作授权）仍走完整的强验证与范围校验，只是不再签发范围授权；
   - A1（去显式约束求解）只移除**计划期的硬约束汇总求解**，执行期仍然逐步重检资金、
     应急金底线与日额度——否则就不是消融，而是把系统做坏了；
   - A4（去 StockQuant 指标）只在工具可见性上体现，不影响资金与授权路径。
3. 离线模式下这些配置产出的是**工程回归对比**，不是模型能力结论（见 ``eval/protocol.md``）。

配置如何生效
------------
:func:`apply_switches` 以**上下文管理器**的方式临时替换（或包裹）实现模块里的少数函数，
退出时精确还原。被替换的目标全部是"决策点"，不是数据路径：

============================  ==========================================
配置字段                       影响的决策点
============================  ==========================================
``use_constraint_solver``      ``orchestration.planner._check_constraints``
``replan_mode``                ``orchestration.recovery`` 的调用方式（由运行器选择）
``grant_scope_kind``           ``orchestration.executor.ensure_plan_grant``
``quant.enabled``              运行器下发给模型的工具可见列表
``driver``                     运行器如何驱动计划（表单／单工具／DAG／完整）
============================  ==========================================
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator

#: 工具目录里属于 StockQuant 只读指标的条目（A4 消融对象）。
QUANT_TOOLS = frozenset({"quant.risk_report"})

#: 与 V5 一致的默认预算（离线本地提供方，无网络调用）。
DEFAULT_BUDGET = {"max_steps": 24, "max_confirmations": 8, "wall_clock_seconds": 300}


@dataclass(frozen=True)
class BaselineConfig:
    """一个可比较的评测配置。"""

    name: str
    kind: str  # baseline / full / ablation
    driver: str  # form / single_tool / dag / full
    description: str
    use_constraint_solver: bool = True
    use_dag_preferences: bool = True
    replan_mode: str = "incremental"  # incremental / global / none
    grant_scope_kind: str = "scoped"  # scoped / exact
    confirm_mode: str = "per_plan"  # per_plan / per_step
    quant_enabled: bool = True
    step_order: str = "topological"  # topological / first_viable / as_returned
    budget: dict = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    ablation_of: str = "FULL"
    ablation_dimension: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "driver": self.driver,
            "description": self.description,
            "use_constraint_solver": self.use_constraint_solver,
            "use_dag_preferences": self.use_dag_preferences,
            "replan_mode": self.replan_mode,
            "grant_scope_kind": self.grant_scope_kind,
            "confirm_mode": self.confirm_mode,
            "quant_enabled": self.quant_enabled,
            "step_order": self.step_order,
            "budget": self.budget,
            "ablation_of": self.ablation_of,
            "ablation_dimension": self.ablation_dimension,
        }


CONFIGURATIONS: dict[str, BaselineConfig] = {
    # --- 基线 ---------------------------------------------------------------
    "B0": BaselineConfig(
        name="B0",
        kind="baseline",
        driver="form",
        description=(
            "结构化表单 + 确定性工作流：由固定字段表驱动，不使用 DAG 规划、不使用偏好排序，"
            "每个写操作单独确认；不产生替代方案，也不做增量恢复。"
        ),
        use_constraint_solver=False,
        use_dag_preferences=False,
        replan_mode="none",
        confirm_mode="per_step",
        step_order="as_returned",
        quant_enabled=False,
    ),
    "B1": BaselineConfig(
        name="B1",
        kind="baseline",
        driver="single_tool",
        description=(
            "单 Agent 工具调用：把每个目标映射到单个工具直接调用，不做依赖规划；"
            "写入仍经完整授权与资金检查。"
        ),
        use_constraint_solver=False,
        use_dag_preferences=False,
        replan_mode="none",
        confirm_mode="per_step",
        step_order="first_viable",
    ),
    "B2": BaselineConfig(
        name="B2",
        kind="baseline",
        driver="dag",
        description=(
            "B1 + 普通 DAG 规划与故障后重新生成剩余计划：有依赖与拓扑序，"
            "但恢复时整图重生成（不区分受影响子图），也不做偏好字典序排序。"
        ),
        use_constraint_solver=False,
        use_dag_preferences=False,
        replan_mode="global",
        quant_enabled=False,
    ),
    # --- 完整实现 -----------------------------------------------------------
    "FULL": BaselineConfig(
        name="FULL",
        kind="full",
        driver="full",
        description="完整实现：约束求解 + DAG + 范围授权 + 增量恢复 + 全工具目录。",
    ),
    # --- 消融 ---------------------------------------------------------------
    "A1": BaselineConfig(
        name="A1",
        kind="ablation",
        driver="full",
        description=(
            "消融：去显式约束求解。计划期不再汇总检查预算/底线/期限/适当性，"
            "执行期仍逐步重检资金、应急金底线与日额度。"
        ),
        use_constraint_solver=False,
        ablation_dimension="constraint_solver",
    ),
    "A2": BaselineConfig(
        name="A2",
        kind="ablation",
        driver="dag",
        description=(
            "消融：故障后全量重规划。保留幂等、已完成效果、授权与资金检查，"
            "只把恢复策略从受影响子图增量修复换成整图重新生成。"
        ),
        replan_mode="global",
        ablation_dimension="recovery_strategy",
    ),
    "A3": BaselineConfig(
        name="A3",
        kind="ablation",
        driver="full",
        description=(
            "消融：仅精确逐操作授权（不签发范围授权）。"
            "强验证、范围校验、一次性使用与资金检查全部保留。"
        ),
        grant_scope_kind="exact",
        confirm_mode="per_step",
        ablation_dimension="grant_scope",
    ),
    "A4": BaselineConfig(
        name="A4",
        kind="ablation",
        driver="full",
        description="消融：去 StockQuant 指标（``quant.risk_report`` 不进入可见工具目录）。",
        quant_enabled=False,
        ablation_dimension="quant_metrics",
    ),
}


def get_config(name: str) -> BaselineConfig:
    key = str(name).upper()
    if key not in CONFIGURATIONS:
        raise KeyError(f"未知评测配置: {name}（可选：{', '.join(sorted(CONFIGURATIONS))}）")
    return CONFIGURATIONS[key]


def config_names() -> list[str]:
    return sorted(CONFIGURATIONS)


def with_overrides(name: str, **overrides: Any) -> BaselineConfig:
    """在固定配置上做显式覆盖（只允许覆盖已声明的字段）。"""
    base = get_config(name)
    unknown = set(overrides) - set(base.to_dict())
    if unknown:
        raise KeyError(f"不允许的配置覆盖字段: {sorted(unknown)}")
    return replace(base, **overrides)


# ---------------------------------------------------------------------------
# 开关生效点
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def apply_switches(config: BaselineConfig) -> Iterator[dict]:
    """按配置临时替换实现里的决策点；退出时精确还原。

    返回一个 dict，记录本次实际生效的开关与"是否真的改变了行为"，
    供逐任务记录写入 ``config_switches`` 字段（可复核）。
    """
    applied: dict[str, Any] = {
        "config": config.name,
        "driver": config.driver,
        "constraint_solver": "on",
        "grant_scope_kind": config.grant_scope_kind,
        "replan_mode": config.replan_mode,
        "quant_enabled": config.quant_enabled,
        "step_order": config.step_order,
        "patched": [],
    }
    restore: list[Callable[[], None]] = []

    if not config.use_constraint_solver:
        from finpilot.orchestration import planner as planner_mod

        original = planner_mod._check_constraints

        def _relaxed_constraints(session, ctx, candidates, goal_spec, snapshot):
            """A1/B0/B1/B2：跳过计划期汇总约束，只保留"账户可访问"这一条安全底线。

            保留这条是因为它属于归属检查（越权），不属于可权衡的硬约束求解；
            去掉它会让消融变成越权漏洞，而不是"没有约束求解"。
            """
            spendable_map = {
                account["account_id"]: account["spendable_minor"] for account in snapshot["accounts"]
            }
            reasons: list[str] = []
            account_id = (goal_spec.get("funding_sources") or [None])[0]
            if account_id and account_id not in spendable_map:
                reasons.append(f"ACCOUNT_NOT_ACCESSIBLE:{account_id}")
            return reasons

        planner_mod._check_constraints = _relaxed_constraints  # type: ignore[assignment]
        restore.append(lambda: setattr(planner_mod, "_check_constraints", original))
        applied["constraint_solver"] = "off"
        applied["patched"].append("orchestration.planner._check_constraints")

    if config.grant_scope_kind == "exact":
        from finpilot.kernel import grants as grants_mod
        from finpilot.orchestration import executor as executor_mod

        original_scoped = grants_mod.create_scoped_grant

        def _no_scoped_grant(*args, **kwargs):
            raise AssertionError("A3 配置不签发范围授权：请使用精确授权路径")

        def _exact_plan_grant(session, ctx, task, plan, steps):
            """A3：每个计划只签发精确授权（绑定参数哈希、单次使用）。"""
            scope = dict(plan.scope)
            scope["kind"] = "exact"
            scope["task_id"] = task.task_id
            scope["plan_id"] = plan.plan_id
            scope["max_uses"] = 1
            scope["max_total_minor"] = int(scope.get("max_total_minor") or 1)
            grant = grants_mod.create_exact_grant(
                session,
                ctx,
                plan_id=plan.plan_id,
                plan_revision=plan.revision,
                scope=scope,
                required_level=plan.required_level,
                verification_level="TOTP" if plan.required_level == "RED" else "NONE",
            )
            if plan.required_level == "RED":
                grant.status = "active"
            session.flush()
            return {"grant_id": grant.grant_id, "level": plan.required_level, "scope_kind": "exact"}

        executor_mod.create_scoped_grant = _no_scoped_grant  # type: ignore[assignment]
        original_ensure = executor_mod.ensure_plan_grant
        executor_mod.ensure_plan_grant = _exact_plan_grant  # type: ignore[assignment]
        restore.append(lambda: setattr(executor_mod, "create_scoped_grant", original_scoped))
        restore.append(lambda: setattr(executor_mod, "ensure_plan_grant", original_ensure))
        applied["patched"].extend(
            [
                "orchestration.executor.ensure_plan_grant",
                "orchestration.executor.create_scoped_grant",
            ]
        )

    try:
        yield applied
    finally:
        for undo in reversed(restore):
            undo()


# ---------------------------------------------------------------------------
# 驱动方式
# ---------------------------------------------------------------------------


def visible_tools(config: BaselineConfig, catalog: list[str]) -> list[str]:
    """按配置给出"模型可见"的工具集合（A4 去掉 StockQuant 指标）。"""
    tools = [t for t in catalog if t not in QUANT_TOOLS] if not config.quant_enabled else list(catalog)
    if config.driver == "single_tool":
        # 单 Agent 基线只暴露直接动作工具，不做只读探索
        read_only = {"account.balance", "bill.report", "wealth.compare", "card.status", "subscription.list"}
        return [t for t in tools if t not in read_only]
    return tools


def plan_step_order(config: BaselineConfig, steps: list[dict]) -> list[dict]:
    """按配置给出计划步骤的执行顺序。

    - ``topological``：依赖优先（FULL / A1 / A3 / A4 的默认顺序，由 executor 保证）；
    - ``first_viable``：只按 seq，遇到依赖未满足即跳过（B1 的单工具直连语义）；
    - ``as_returned``：完全按生成顺序，不做任何重排（B0 的确定性工作流）。
    """
    if config.step_order == "as_returned":
        return list(steps)
    if config.step_order == "first_viable":
        return sorted(steps, key=lambda s: (len(s.get("dependencies") or []), s.get("seq") or 0))
    return sorted(steps, key=lambda s: (s.get("seq") or 0))


def recovery_plan_call(config: BaselineConfig, *, global_replan: Callable[[], Any], incremental: Callable[[], Any]) -> Any:
    """按 ``replan_mode`` 选择恢复策略；A2/B2 走整图重生成，但调用同一套约束与幂等路径。"""
    if config.replan_mode == "global":
        return global_replan()
    if config.replan_mode == "none":
        return None
    return incremental()


def describe_all() -> list[dict]:
    return [CONFIGURATIONS[name].to_dict() for name in config_names()]


__all__ = [
    "BaselineConfig",
    "CONFIGURATIONS",
    "QUANT_TOOLS",
    "DEFAULT_BUDGET",
    "get_config",
    "config_names",
    "with_overrides",
    "apply_switches",
    "visible_tools",
    "plan_step_order",
    "recovery_plan_call",
    "describe_all",
]