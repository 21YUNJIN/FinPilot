"""状态机：分离任务、操作、授权、调度与补偿状态，强制校验合法转移（V5 7.1）。

任何模块修改状态都必须经此模块，禁止直接赋值绕过校验。
"""

from __future__ import annotations

from ..errors import FinPilotError

# 任务状态转移（V5 7.1 表）
TASK_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"clarifying", "planning", "cancelled", "failed", "blocked"},
    "clarifying": {"planning", "cancelled", "failed", "draft", "blocked"},
    "planning": {"awaiting_authorization", "ready", "blocked", "clarifying", "cancelled", "failed"},
    "awaiting_authorization": {"ready", "blocked", "planning", "cancelled", "failed"},
    "ready": {"running", "awaiting_authorization", "cancelled", "failed", "blocked"},
    "running": {
        "reconciling",
        "replanning",
        "awaiting_authorization",
        "awaiting_human",
        "succeeded",
        "partially_succeeded",
        "failed",
        "cancelled",
        "ready",
    },
    "reconciling": {"running", "replanning", "succeeded", "partially_succeeded", "failed", "awaiting_human", "cancelled"},
    "replanning": {"planning", "awaiting_authorization", "ready", "awaiting_human", "failed", "cancelled"},
    "awaiting_human": {"running", "replanning", "succeeded", "partially_succeeded", "failed", "cancelled", "blocked"},
    "blocked": {"planning", "ready", "cancelled", "failed"},
    "succeeded": set(),
    "partially_succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}

# 操作状态转移
OPERATION_TRANSITIONS: dict[str, set[str]] = {
    "created": {"awaiting_authorization", "authorized", "cancelled", "failed"},
    "awaiting_authorization": {"authorized", "cancelled", "failed"},
    "authorized": {"dispatching", "cancelled", "failed"},
    "dispatching": {"pending", "unknown", "succeeded", "failed", "authorized"},
    "pending": {"succeeded", "failed", "unknown", "cancelled"},
    "unknown": {"succeeded", "failed", "pending"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}

GRANT_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"active", "revoked", "expired"},
    "active": {"consumed", "revoked", "expired", "active"},
    "consumed": set(),
    "revoked": set(),
    "expired": set(),
}

JOB_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"leased", "missed", "failed", "awaiting_authorization", "completed", "cancelled"},
    "leased": {"running", "scheduled", "missed", "failed", "cancelled"},
    "running": {"completed", "failed", "awaiting_authorization", "scheduled"},
    "awaiting_authorization": {"scheduled", "failed", "completed", "cancelled"},
    "completed": set(),
    "missed": {"scheduled"},
    "failed": {"scheduled"},
    "cancelled": set(),
}

RESERVATION_TRANSITIONS: dict[str, set[str]] = {
    "active": {"partially_captured", "captured", "released", "expired"},
    "partially_captured": {"partially_captured", "captured", "released", "expired"},
    "captured": set(),
    "released": set(),
    "expired": set(),
}

_TRANSITIONS = {
    "task": TASK_TRANSITIONS,
    "operation": OPERATION_TRANSITIONS,
    "grant": GRANT_TRANSITIONS,
    "job": JOB_TRANSITIONS,
    "reservation": RESERVATION_TRANSITIONS,
}


def can_transition(kind: str, from_status: str, to_status: str) -> bool:
    table = _TRANSITIONS.get(kind)
    if table is None:
        raise FinPilotError("SYSTEM_ERROR", f"未知状态机类型: {kind}")
    if from_status == to_status:
        return True
    return to_status in table.get(from_status, set())


def assert_transition(kind: str, from_status: str, to_status: str) -> None:
    if not can_transition(kind, from_status, to_status):
        raise FinPilotError(
            "POLICY_DENIED",
            f"非法状态转移：{kind} {from_status} → {to_status}",
            details={"reason_codes": ["illegal_transition"], "kind": kind, "from": from_status, "to": to_status},
        )


def is_terminal(kind: str, status: str) -> bool:
    table = _TRANSITIONS[kind]
    return not table.get(status)


def task_status_for_outcome(*, completed_goals: int, total_goals: int, any_effect: bool, blocked: bool) -> str:
    """按已核验效果推导任务终态；等待与未知都不是成功。"""
    if blocked:
        return "blocked"
    if total_goals and completed_goals >= total_goals:
        return "succeeded"
    if completed_goals > 0 or any_effect:
        return "partially_succeeded"
    return "failed"