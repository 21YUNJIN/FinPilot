"""有界形式化验证包（PLAN-08、T57）。

- :mod:`eval.formal.transition_model` —— 有界状态空间与迁移；
- :mod:`eval.formal.check_invariants` —— 交错枚举上的不变量检查与反例导出。

**边界声明**：这是有界模型检查，不是完整实现的证明。详见两个模块的文档与
``check_invariants`` 返回里的 ``claim`` / ``uncovered`` 字段。
"""

from __future__ import annotations

from .check_invariants import (
    CHECKS,
    CHECK_NAMES,
    UNCOVERED_ITEMS,
    check_invariants,
    counterexample_state,
)
from .transition_model import (
    ACCOUNTS,
    MAX_OPERATIONS_PER_TASK,
    MONEY_DOMAIN,
    ModelState,
    TransitionError,
    candidate_actions,
    enumerate_states,
    initial_state,
    state_space_report,
)

__all__ = [
    "ACCOUNTS",
    "MAX_OPERATIONS_PER_TASK",
    "MONEY_DOMAIN",
    "ModelState",
    "TransitionError",
    "candidate_actions",
    "enumerate_states",
    "initial_state",
    "state_space_report",
    "CHECKS",
    "CHECK_NAMES",
    "UNCOVERED_ITEMS",
    "check_invariants",
    "counterexample_state",
]