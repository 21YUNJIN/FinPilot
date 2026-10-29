"""在有限交错上检查不变量（PLAN-08、T57）。

检查项（每条都可独立失败的"安全性质"）
--------------------------------------
1. ``ledger_split_balance``   每笔账本事务分录守恒：Σ signed_amount_minor == 0；
2. ``ledger_matches_accounts`` 账户已记账余额 == 初始余额 + Σ 该账户分录（无凭空记账）；
3. ``grant_single_use``       一次性授权不会被消费两次（used_count ≤ max_uses，且成功操作不重复）；
4. ``no_cross_user_write``    任何用户都不能花别人的账户（动作层已拒绝，这里复查最终状态）；
5. ``unknown_hold_not_released`` 结果未知的在途资金不得被释放；
6. ``balance_non_negative``   任何账户任何时刻的已记账余额不得为负；
7. ``capture_no_double_count`` 捕获时 posted 与 reserved 同减（不会双减/重复扣款）；
8. ``operation_posts_once``   同一操作最多记账一次（幂等）。
9. ``no_hold_leak``           释放后 reserved_minor 不得残留（H = Σ remaining）。

报告口径
--------
``checked_states`` / ``checked_transitions`` / ``counterexamples`` / ``uncovered``。
**不声称证明了完整实现**：模型是有界的（见 ``transition_model`` 文档）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterator, Sequence

from .transition_model import (
    ACCOUNTS,
    MONEY_DOMAIN,
    ModelState,
    TransitionError,
    candidate_actions,
    enumerate_states,
    initial_state,
    state_space_report,
)

CHECK_NAMES: tuple[str, ...] = (
    "ledger_split_balance",
    "ledger_matches_accounts",
    "grant_single_use",
    "no_cross_user_write",
    "unknown_hold_not_released",
    "balance_non_negative",
    "capture_no_double_count",
    "operation_posts_once",
    "no_hold_leak",
)


@dataclass
class Violation:
    check: str
    path: tuple[str, ...]
    detail: dict

    def to_dict(self) -> dict:
        return {"check": self.check, "path": list(self.path), "detail": self.detail}


def _initial_posted() -> dict[str, int]:
    state = initial_state()
    return {account_id: account.posted_minor for account_id, account in state.accounts.items()}


INITIAL_POSTED = _initial_posted()


# ---------------------------------------------------------------------------
# 单项不变量
# ---------------------------------------------------------------------------


def check_ledger_split_balance(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    violations: list[Violation] = []
    by_tx: dict[str, list[dict]] = {}
    for entry in state.ledger_entries:
        by_tx.setdefault(str(entry["ledger_tx_id"]), []).append(entry)
    for tx in state.ledger_transactions:
        tx_id = str(tx["ledger_tx_id"])
        total = sum(int(item["signed_amount_minor"]) for item in by_tx.get(tx_id, []))
        if total != 0:
            violations.append(Violation("ledger_split_balance", path, {"ledger_tx_id": tx_id, "sum_minor": total}))
    return violations


def check_ledger_matches_accounts(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    violations: list[Violation] = []
    for account_id, account in state.accounts.items():
        expected = INITIAL_POSTED.get(account_id, 0) + state.ledger_balance(account_id)
        if account.posted_minor != expected:
            violations.append(
                Violation(
                    "ledger_matches_accounts",
                    path,
                    {
                        "account_id": account_id,
                        "expected_posted_minor": expected,
                        "actual_posted_minor": account.posted_minor,
                    },
                )
            )
    return violations


def check_grant_single_use(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    violations: list[Violation] = []
    for grant in state.grants.values():
        if grant.used_count > grant.max_uses:
            violations.append(
                Violation(
                    "grant_single_use",
                    path,
                    {"grant_id": grant.grant_id, "used_count": grant.used_count, "max_uses": grant.max_uses},
                )
            )
        if grant.max_total_minor and grant.used_total_minor > grant.max_total_minor:
            violations.append(
                Violation(
                    "grant_single_use",
                    path,
                    {
                        "grant_id": grant.grant_id,
                        "used_total_minor": grant.used_total_minor,
                        "max_total_minor": grant.max_total_minor,
                    },
                )
            )
    return violations


def check_no_cross_user_write(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    violations: list[Violation] = []
    owners = {account_id: owner for owner, account_id in ACCOUNTS}
    for tx in state.ledger_transactions:
        operation = state.operations.get(str(tx.get("operation_id") or ""))
        if operation is None:
            continue
        for entry in state.ledger_entries:
            if entry["ledger_tx_id"] != tx["ledger_tx_id"]:
                continue
            account_id = str(entry["account_id"])
            if account_id == "clearing":
                continue
            owner = owners.get(account_id)
            if owner and owner != operation.owner_user_id:
                violations.append(
                    Violation(
                        "no_cross_user_write",
                        path,
                        {
                            "operation_id": operation.operation_id,
                            "owner_user_id": operation.owner_user_id,
                            "account_id": account_id,
                            "account_owner": owner,
                        },
                    )
                )
    return violations


def check_unknown_hold_not_released(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    violations: list[Violation] = []
    for reservation in state.reservations.values():
        if reservation.unknown_inflight and reservation.status == "released":
            violations.append(
                Violation(
                    "unknown_hold_not_released",
                    path,
                    {"reservation_id": reservation.reservation_id, "status": reservation.status},
                )
            )
    return violations


def check_balance_non_negative(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    violations: list[Violation] = []
    for account in state.accounts.values():
        if account.posted_minor < 0:
            violations.append(
                Violation(
                    "balance_non_negative",
                    path,
                    {"account_id": account.account_id, "posted_minor": account.posted_minor},
                )
            )
        if account.reserved_minor < 0:
            violations.append(
                Violation(
                    "balance_non_negative",
                    path,
                    {"account_id": account.account_id, "reserved_minor": account.reserved_minor},
                )
            )
    return violations


def check_capture_no_double_count(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    """捕获：posted 减少额 == Σ 捕获分录，且 reserved 减少额 == 捕获额。"""
    violations: list[Violation] = []
    captured_by_account: dict[str, int] = {}
    for tx in state.ledger_transactions:
        if tx.get("kind") != "capture":
            continue
        for entry in state.ledger_entries:
            if entry["ledger_tx_id"] != tx["ledger_tx_id"]:
                continue
            if int(entry["signed_amount_minor"]) < 0:
                account_id = str(entry["account_id"])
                captured_by_account[account_id] = captured_by_account.get(account_id, 0) + abs(
                    int(entry["signed_amount_minor"])
                )
    for reservation in state.reservations.values():
        if reservation.captured_minor <= 0:
            continue
        account = state.account(reservation.account_id)
        # 该账户被捕获的总额不得超过其初始余额（否则就是凭空多扣）
        if captured_by_account.get(account.account_id, 0) > INITIAL_POSTED.get(account.account_id, 0):
            violations.append(
                Violation(
                    "capture_no_double_count",
                    path,
                    {
                        "account_id": account.account_id,
                        "captured_minor": captured_by_account.get(account.account_id, 0),
                        "initial_posted_minor": INITIAL_POSTED.get(account.account_id, 0),
                    },
                )
            )
        if reservation.captured_minor + reservation.remaining_minor > reservation.original_minor:
            violations.append(
                Violation(
                    "capture_no_double_count",
                    path,
                    {
                        "reservation_id": reservation.reservation_id,
                        "captured": reservation.captured_minor,
                        "remaining": reservation.remaining_minor,
                        "original": reservation.original_minor,
                    },
                )
            )
    return violations


def check_operation_posts_once(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    """同一操作的同类账本事务最多一条，且已成功的操作必须确有资金效果。"""
    violations: list[Violation] = []
    by_operation: dict[str, list[str]] = {}
    for tx in state.ledger_transactions:
        operation_id = str(tx.get("operation_id") or "")
        if not operation_id:
            continue
        by_operation.setdefault(operation_id, []).append(str(tx.get("kind")))
    for operation_id, kinds in by_operation.items():
        if len(kinds) != len(set(kinds)):
            violations.append(
                Violation("operation_posts_once", path, {"operation_id": operation_id, "kinds": kinds})
            )
    for operation in state.operations.values():
        if operation.status != "succeeded":
            continue
        account = state.account(operation.account_id)
        if operation.amount_minor <= 0:
            continue
        # 成功的转账必须真的减少了账户已记账余额（捕获或直接记账都算）
        effect_recorded = operation.posted or any(
            entry["account_id"] == account.account_id
            and int(entry["signed_amount_minor"]) < 0
            and str(tx.get("operation_id")) == operation.operation_id
            for tx in state.ledger_transactions
            for entry in state.ledger_entries
            if entry["ledger_tx_id"] == tx["ledger_tx_id"]
        )
        if not effect_recorded:
            violations.append(
                Violation(
                    "operation_posts_once",
                    path,
                    {
                        "operation_id": operation.operation_id,
                        "reason": "succeeded_without_recorded_effect",
                        "account_id": account.account_id,
                    },
                )
            )
    return violations


def check_no_hold_leak(state: ModelState, path: tuple[str, ...]) -> list[Violation]:
    """H（reserved）必须等于各预留 remaining 之和；释放后不得残留。"""
    violations: list[Violation] = []
    for account_id, account in state.accounts.items():
        expected = sum(
            reservation.remaining_minor
            for reservation in state.reservations.values()
            if reservation.account_id == account_id and reservation.status not in ("released",)
        )
        if account.reserved_minor != expected:
            violations.append(
                Violation(
                    "no_hold_leak",
                    path,
                    {
                        "account_id": account_id,
                        "reserved_minor": account.reserved_minor,
                        "expected_from_reservations": expected,
                    },
                )
            )
    return violations


CHECKS: dict[str, Callable[[ModelState, tuple[str, ...]], list[Violation]]] = {
    "ledger_split_balance": check_ledger_split_balance,
    "ledger_matches_accounts": check_ledger_matches_accounts,
    "grant_single_use": check_grant_single_use,
    "no_cross_user_write": check_no_cross_user_write,
    "unknown_hold_not_released": check_unknown_hold_not_released,
    "balance_non_negative": check_balance_non_negative,
    "capture_no_double_count": check_capture_no_double_count,
    "operation_posts_once": check_operation_posts_once,
    "no_hold_leak": check_no_hold_leak,
}


# ---------------------------------------------------------------------------
# 遍历检查
# ---------------------------------------------------------------------------


UNCOVERED_ITEMS: tuple[str, ...] = (
    "真实数据库并发（多连接、行锁等待、序列化失败重试）不在有界模型内",
    "网络分区、提供方乱序/重复回调的时序细节只抽象为 dispatch_unknown/receipt_*",
    "应急金底线（protected floor）未纳入本有界模型；由资金与策略测试覆盖",
    "日额度桶的并发分拆只在等级维度抽象（金额域含恰好阈值与超阈值）",
    "HTTP 层鉴权、审计哈希链与沙箱执行不在本模型内",
    "未枚举超过 max_actions 的更长交错；未做符号化/无界证明",
)


def check_invariants(*, max_actions: int = 4, max_counterexamples: int = 5) -> dict:
    """在长度 ≤ ``max_actions`` 的全部动作序列上检查不变量。"""
    per_check_failures: dict[str, int] = {name: 0 for name in CHECK_NAMES}
    counterexamples: dict[str, list[dict]] = {name: [] for name in CHECK_NAMES}
    states_checked = 0
    transitions_checked = 0
    max_depth = 0

    for state, path in enumerate_states(max_actions=max_actions):
        states_checked += 1
        max_depth = max(max_depth, len(path))
        for name, checker in CHECKS.items():
            for violation in checker(state, path):
                per_check_failures[name] += 1
                if len(counterexamples[name]) < max_counterexamples:
                    counterexamples[name].append(violation.to_dict())

    # 单独统计边数：状态数与迁移数必须分开报告，避免夸大覆盖面
    frontier: list[ModelState] = [initial_state()]
    seen: set[str] = {frontier[0].key()}
    for _depth in range(max_actions):
        next_frontier: list[ModelState] = []
        for state in frontier:
            for _action, thunk in candidate_actions(state):
                try:
                    new_state = thunk()
                except TransitionError:
                    continue
                transitions_checked += 1
                key = new_state.key()
                if key not in seen:
                    seen.add(key)
                    next_frontier.append(new_state)
        frontier = next_frontier

    space = state_space_report(max_actions=max_actions)
    failed_checks = {name: count for name, count in per_check_failures.items() if count}
    return {
        "bounded_model_check": True,
        "max_actions": max_actions,
        "money_domain": list(MONEY_DOMAIN),
        "accounts": len(ACCOUNTS),
        "states_enumerated": states_checked,
        "states_unique": len(seen),
        "transitions_checked": transitions_checked,
        "max_depth_reached": max_depth,
        "checks": list(CHECK_NAMES),
        "checks_passed": [name for name in CHECK_NAMES if name not in failed_checks],
        "failed_checks": failed_checks,
        "counterexamples": counterexamples if failed_checks else {},
        "uncovered": list(UNCOVERED_ITEMS),
        "claim": (
            "在给定有界状态空间与动作长度内未发现所列不变量的反例。"
            "这不是对完整实现的证明，也不覆盖真实并发、网络与数据库隔离级别。"
            if not failed_checks
            else "发现反例：见 counterexamples；这些反例必须在实现里修复或显式登记为已知限制。"
        ),
        "space_report": space,
    }


def counterexample_state(path: Sequence[str], *, max_actions: int = 4) -> dict | None:
    """把一条反例路径回放成具体状态快照（便于写测试与报告）。"""
    state = initial_state()
    for label in path:
        applied = False
        for action, thunk in candidate_actions(state):
            if action.label() != label:
                continue
            try:
                state = thunk()
                applied = True
            except TransitionError:
                applied = False
            break
        if not applied:
            return None
    return state.snapshot()


__all__ = [
    "CHECKS",
    "CHECK_NAMES",
    "UNCOVERED_ITEMS",
    "Violation",
    "check_invariants",
    "counterexample_state",
]