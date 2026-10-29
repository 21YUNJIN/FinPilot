"""有界状态空间迁移模型（PLAN-08、T57）。

定位（先说边界，再说能力）
--------------------------
这是**有界形式化验证**，不是"完整实现已被证明"：

1. 状态空间被显式**有界**：2 个用户、每用户 2 个账户、金额取自有限集合
   （0 / 100 / 200 / 恰好阈值 1000 / 超阈值 1100 分）、每个任务最多 2 个操作、
   动作序列长度 ≤ ``max_actions``；
2. 模型是**迁移级抽象**：它建模"授权签发/消费、提交、捕获、释放、取消、未知回执、
   重复提交"这些动作对账本、预留、授权计数的影响，不建模 HTTP、序列化与真实数据库隔离级别；
3. 因此结论只能读作：**在这组有界交错里没有发现反例**。真实并发（多进程、真实行锁、
   网络分区）不在本模型覆盖范围内——这一点写在 ``check_invariants`` 的未覆盖项里。

模型要素
--------
- ``Money``：整数分，禁止浮点；
- ``Account``：``posted``（已记账余额）、``reserved``（预留占用）、``frozen_other``；
  可支配 = ``max(0, posted − reserved − frozen)``；
- ``Reservation``：``remaining/captured/released/status(long_lived)``；
  不变量 H = Σ remaining；捕获 x 时 ``posted`` 与 ``remaining`` **同减**（禁止双减）；
- ``Operation``：``status ∈ {created, authorized, dispatching, pending, unknown, succeeded, failed, cancelled}``，
  记录是否已记账（``posted``）以保证"同一操作只记一次"；
- ``Ledger``：每笔事务是若干分录，**必须守恒**（Σ signed == 0）；
- ``Grant``：``max_uses / used_count / status``，一次性授权不得二次消费；
- ``Transfer``：转账动作的抽象，带等级（GREEN/YELLOW/RED）与所需授权。

动作（transition）
------------------
``authorize`` / ``submit`` / ``dispatch_unknown`` / ``receipt_success`` / ``receipt_failure`` /
``cancel`` / ``capture`` / ``release_uncommitted`` / ``expire_reservation`` / ``duplicate_submit``。
每个动作都返回**动作后的新状态**（不可变风格：深拷贝），便于枚举里保留历史。
"""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

# ---------------------------------------------------------------------------
# 有限域
# ---------------------------------------------------------------------------

#: 金额取自有限集合（含日额度边界 1000 元 = 100000 分 的抽象缩放）
MONEY_DOMAIN: tuple[int, ...] = (0, 100, 200, 1_000, 1_100)
#: 每个任务最多 2 个操作
MAX_OPERATIONS_PER_TASK = 2
#: 迁移级等级
LEVELS: tuple[str, ...] = ("GREEN", "YELLOW", "RED")
#: 需要强验证的等级
RED = "RED"
#: 抽象日额度阈值（分）——与 constants 里的抽象缩放一致
YELLOW_LIMIT_MINOR = 1_000

USERS: tuple[str, ...] = ("u1", "u2")
ACCOUNTS: tuple[tuple[str, str], ...] = (("u1", "a1"), ("u1", "a2"), ("u2", "a3"), ("u2", "a4"))
PROTECTED_FLOOR_MINOR = 0  # 本有界模型先验证"非负 + 守恒"，底线穿透另有专项（见未覆盖项）

OPERATION_STATUSES: tuple[str, ...] = (
    "created",
    "authorized",
    "dispatching",
    "pending",
    "unknown",
    "succeeded",
    "failed",
    "cancelled",
)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


@dataclass
class AccountState:
    account_id: str
    owner_user_id: str
    posted_minor: int = 0
    reserved_minor: int = 0
    frozen_other_minor: int = 0

    def spendable_minor(self) -> int:
        return max(0, self.posted_minor - self.reserved_minor - self.frozen_other_minor)

    def snapshot(self) -> dict:
        return {
            "account_id": self.account_id,
            "owner_user_id": self.owner_user_id,
            "posted_minor": self.posted_minor,
            "reserved_minor": self.reserved_minor,
            "frozen_other_minor": self.frozen_other_minor,
        }


@dataclass
class ReservationState:
    reservation_id: str
    account_id: str
    owner_user_id: str
    original_minor: int
    remaining_minor: int
    captured_minor: int = 0
    released_minor: int = 0
    status: str = "active"  # active / partially_captured / captured / released
    long_lived: bool = False
    unknown_inflight: bool = False

    def snapshot(self) -> dict:
        return {
            "reservation_id": self.reservation_id,
            "account_id": self.account_id,
            "owner_user_id": self.owner_user_id,
            "original_minor": self.original_minor,
            "remaining_minor": self.remaining_minor,
            "captured_minor": self.captured_minor,
            "released_minor": self.released_minor,
            "status": self.status,
            "unknown_inflight": self.unknown_inflight,
        }


@dataclass
class OperationState:
    operation_id: str
    owner_user_id: str
    account_id: str
    amount_minor: int
    level: str
    status: str = "created"
    required_grant: bool = False
    grant_id: str | None = None
    posted: bool = False
    reservation_id: str | None = None
    duplicate_of: str | None = None

    def snapshot(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "owner_user_id": self.owner_user_id,
            "account_id": self.account_id,
            "amount_minor": self.amount_minor,
            "level": self.level,
            "status": self.status,
            "required_grant": self.required_grant,
            "grant_id": self.grant_id,
            "posted": self.posted,
            "reservation_id": self.reservation_id,
            "duplicate_of": self.duplicate_of,
        }


@dataclass
class GrantState:
    grant_id: str
    owner_user_id: str
    required_level: str
    max_uses: int = 1
    used_count: int = 0
    max_total_minor: int = 0
    used_total_minor: int = 0
    status: str = "active"  # pending / active / consumed / revoked / expired
    verification_level: str = "NONE"

    def snapshot(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "owner_user_id": self.owner_user_id,
            "required_level": self.required_level,
            "max_uses": self.max_uses,
            "used_count": self.used_count,
            "max_total_minor": self.max_total_minor,
            "used_total_minor": self.used_total_minor,
            "status": self.status,
            "verification_level": self.verification_level,
        }


@dataclass
class ModelState:
    """一个具体的有界状态。所有字段都可 JSON 序列化，便于反例导出。"""

    accounts: dict[str, AccountState] = field(default_factory=dict)
    reservations: dict[str, ReservationState] = field(default_factory=dict)
    operations: dict[str, OperationState] = field(default_factory=dict)
    grants: dict[str, GrantState] = field(default_factory=dict)
    ledger_entries: list[dict] = field(default_factory=list)
    ledger_transactions: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    task_operations: dict[str, int] = field(default_factory=dict)

    # --- 基本操作 ---
    def clone(self) -> "ModelState":
        return copy.deepcopy(self)

    def account(self, account_id: str) -> AccountState:
        return self.accounts[account_id]

    def ledger_balance(self, account_id: str) -> int:
        return sum(
            int(entry["signed_amount_minor"])
            for entry in self.ledger_entries
            if entry["account_id"] == account_id
        )

    def hold_total_minor(self) -> int:
        return sum(r.remaining_minor for r in self.reservations.values())

    def snapshot(self) -> dict:
        return {
            "accounts": [a.snapshot() for a in self.accounts.values()],
            "reservations": [r.snapshot() for r in self.reservations.values()],
            "operations": [o.snapshot() for o in self.operations.values()],
            "grants": [g.snapshot() for g in self.grants.values()],
            "ledger_transactions": list(self.ledger_transactions),
            "ledger_entries": list(self.ledger_entries),
            "hold_total_minor": self.hold_total_minor(),
        }

    def key(self) -> str:
        return json.dumps(self.snapshot(), sort_keys=True, ensure_ascii=False)


def initial_state() -> ModelState:
    """标准初态：四账户各 500 分；另有一个清算账户（初始 0），使"清分入账"也可核验。"""
    state = ModelState()
    for owner, account_id in ACCOUNTS:
        state.accounts[account_id] = AccountState(
            account_id=account_id, owner_user_id=owner, posted_minor=500
        )
    state.accounts["clearing"] = AccountState(account_id="clearing", owner_user_id="system", posted_minor=0)
    return state


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    name: str
    args: tuple = ()

    def label(self) -> str:
        return self.name if not self.args else f"{self.name}{self.args}"


def _grant_confers_level(grant: GrantState, level: str) -> bool:
    if grant.status != "active":
        return False
    if level == RED and grant.verification_level != "TOTP":
        return False
    return True


def _post_transfer(state: ModelState, operation: OperationState) -> None:
    """唯一记账入口：同一操作只允许记账一次；分录守恒。"""
    if operation.posted:
        return
    amount = operation.amount_minor
    if amount <= 0:
        operation.posted = True
        return
    account = state.account(operation.account_id)
    if account.posted_minor < amount:
        raise TransitionError("INSUFFICIENT_FUNDS")
    clearing_id = "clearing"
    if clearing_id not in state.accounts:
        state.accounts[clearing_id] = AccountState(
            account_id=clearing_id, owner_user_id="system", posted_minor=0
        )
    clearing = state.accounts[clearing_id]
    tx_id = f"ltx_{operation.operation_id}"
    state.ledger_transactions.append(
        {"ledger_tx_id": tx_id, "operation_id": operation.operation_id, "kind": "transfer", "sum_minor": 0}
    )
    state.ledger_entries.append(
        {
            "ledger_tx_id": tx_id,
            "account_id": account.account_id,
            "signed_amount_minor": -amount,
        }
    )
    state.ledger_entries.append(
        {"ledger_tx_id": tx_id, "account_id": clearing_id, "signed_amount_minor": amount}
    )
    account.posted_minor -= amount
    clearing.posted_minor += amount
    operation.posted = True


class TransitionError(Exception):
    """动作在当前状态下不被允许（用于剪枝，不算反例）。"""


def _require_operation(state: ModelState, operation_id: str) -> OperationState:
    operation = state.operations.get(operation_id)
    if operation is None:
        raise TransitionError("UNKNOWN_OPERATION")
    return operation


def action_open_operation(state: ModelState, *, operation_id: str, owner: str, account_id: str,
                          amount_minor: int, level: str, required_grant: bool,
                          grant_id: str | None = None) -> ModelState:
    if operation_id in state.operations:
        raise TransitionError("DUPLICATE_OPERATION_ID")
    task_key = operation_id.rsplit("_", 1)[0]
    if state.task_operations.get(task_key, 0) >= MAX_OPERATIONS_PER_TASK:
        raise TransitionError("TASK_OPERATION_LIMIT")
    if state.account(account_id).owner_user_id != owner:
        raise TransitionError("CROSS_USER_ACCOUNT")
    state.task_operations[task_key] = state.task_operations.get(task_key, 0) + 1
    state.operations[operation_id] = OperationState(
        operation_id=operation_id,
        owner_user_id=owner,
        account_id=account_id,
        amount_minor=amount_minor,
        level=level,
        status="created",
        required_grant=required_grant,
        grant_id=grant_id,
    )
    return state


def action_authorize(state: ModelState, *, operation_id: str, grant_id: str | None = None) -> ModelState:
    operation = _require_operation(state, operation_id)
    if operation.status != "created":
        raise TransitionError("NOT_CREATED")
    if operation.required_grant:
        if not grant_id:
            raise TransitionError("GRANT_REQUIRED")
        grant = state.grants.get(grant_id)
        if grant is None or grant.owner_user_id != operation.owner_user_id:
            raise TransitionError("GRANT_NOT_FOUND")
        if not _grant_confers_level(grant, operation.level):
            raise TransitionError("GRANT_NOT_ACTIVE_OR_NOT_STRONG")
        operation.grant_id = grant_id
    operation.status = "authorized"
    return state


def action_submit(state: ModelState, *, operation_id: str) -> ModelState:
    """提交：消费授权（一次性）→ 登记额度占用与资金预留 → 进入 dispatching。"""
    operation = _require_operation(state, operation_id)
    if operation.status != "authorized":
        raise TransitionError("NOT_AUTHORIZED")
    if operation.required_grant:
        grant = state.grants.get(str(operation.grant_id))
        if grant is None:
            raise TransitionError("GRANT_NOT_FOUND")
        if grant.used_count >= grant.max_uses:
            raise TransitionError("GRANT_EXHAUSTED")
        if grant.max_total_minor and grant.used_total_minor + operation.amount_minor > grant.max_total_minor:
            raise TransitionError("GRANT_TOTAL_EXCEEDED")
        grant.used_count += 1
        grant.used_total_minor += operation.amount_minor
        if grant.used_count >= grant.max_uses:
            grant.status = "consumed"
    account = state.account(operation.account_id)
    if operation.amount_minor > account.spendable_minor():
        raise TransitionError("INSUFFICIENT_SPENDABLE")
    reservation_id = f"res_{operation.operation_id}"
    state.reservations[reservation_id] = ReservationState(
        reservation_id=reservation_id,
        account_id=account.account_id,
        owner_user_id=operation.owner_user_id,
        original_minor=operation.amount_minor,
        remaining_minor=operation.amount_minor,
    )
    account.reserved_minor += operation.amount_minor
    operation.reservation_id = reservation_id
    operation.status = "dispatching"
    return state


def action_submit_again(state: ModelState, *, operation_id: str) -> ModelState:
    """重复提交同一 operation_id：必须幂等（不产生第二次占用/记账）。"""
    operation = _require_operation(state, operation_id)
    if operation.status in ("created",):
        raise TransitionError("NOT_SUBMITTED_YET")
    operation.duplicate_of = operation_id
    if operation.status in ("dispatching", "pending", "unknown", "succeeded"):
        return state  # 幂等：沿用同一操作，不重复登记
    raise TransitionError("TERMINAL_NOT_RESUBMITTABLE")


def action_dispatch_unknown(state: ModelState, *, operation_id: str) -> ModelState:
    operation = _require_operation(state, operation_id)
    if operation.status != "dispatching":
        raise TransitionError("NOT_DISPATCHING")
    operation.status = "unknown"
    reservation = state.reservations.get(str(operation.reservation_id))
    if reservation is not None:
        reservation.unknown_inflight = True
    return state


def action_receipt_success(state: ModelState, *, operation_id: str) -> ModelState:
    """成功回执：捕获预留或直接记账；**只记账一次**。"""
    operation = _require_operation(state, operation_id)
    if operation.status not in ("dispatching", "pending", "unknown"):
        raise TransitionError("NOT_INFLIGHT")
    if operation.posted:
        # 已经通过捕获记过账：不允许再记一次（这正是"重复扣款"反例的入口）
        operation.status = "succeeded"
        return state
    reservation = state.reservations.get(str(operation.reservation_id))
    if reservation is not None and reservation.remaining_minor >= operation.amount_minor:
        capture_reservation(state, reservation_id=reservation.reservation_id, amount_minor=operation.amount_minor)
    else:
        _post_transfer(state, operation)
    operation.status = "succeeded"
    return state


def action_receipt_failure(state: ModelState, *, operation_id: str) -> ModelState:
    operation = _require_operation(state, operation_id)
    if operation.status not in ("dispatching", "pending", "unknown"):
        raise TransitionError("NOT_INFLIGHT")
    if operation.status == "unknown":
        raise TransitionError("UNKNOWN_MUST_BE_VERIFIED_FIRST")
    reservation = state.reservations.get(str(operation.reservation_id))
    if reservation is not None:
        release_reservation(state, reservation_id=reservation.reservation_id, reason="provider_failed")
    operation.status = "failed"
    return state


def action_cancel(state: ModelState, *, operation_id: str) -> ModelState:
    operation = _require_operation(state, operation_id)
    if operation.status in ("succeeded", "failed", "cancelled"):
        raise TransitionError("ALREADY_TERMINAL")
    if operation.status == "unknown":
        raise TransitionError("UNKNOWN_MUST_BE_VERIFIED_FIRST")
    reservation = state.reservations.get(str(operation.reservation_id))
    if reservation is not None:
        release_reservation(state, reservation_id=reservation.reservation_id, reason="cancelled")
    if operation.grant_id:
        grant = state.grants.get(operation.grant_id)
        if grant is not None and grant.used_count > 0:
            grant.used_count -= 1
            grant.used_total_minor = max(0, grant.used_total_minor - operation.amount_minor)
            if grant.status == "consumed":
                grant.status = "active"
    operation.status = "cancelled"
    return state


def capture_reservation(state: ModelState, *, reservation_id: str, amount_minor: int) -> ModelState:
    """捕获 x：B 与 H **同减 x**（禁止双减）。"""
    reservation = state.reservations.get(reservation_id)
    if reservation is None:
        raise TransitionError("UNKNOWN_RESERVATION")
    if reservation.status in ("released",):
        raise TransitionError("ALREADY_RELEASED")
    if amount_minor > reservation.remaining_minor:
        raise TransitionError("CAPTURE_EXCEEDS_REMAINING")
    account = state.account(reservation.account_id)
    if account.posted_minor < amount_minor:
        raise TransitionError("INSUFFICIENT_FUNDS")
    clearing_id = "clearing"
    if clearing_id not in state.accounts:
        state.accounts[clearing_id] = AccountState(
            account_id=clearing_id, owner_user_id="system", posted_minor=0
        )
    tx_id = f"ltx_cap_{reservation_id}_{len(state.ledger_transactions)}"
    state.ledger_transactions.append(
        {"ledger_tx_id": tx_id, "operation_id": reservation_id, "kind": "capture", "sum_minor": 0}
    )
    state.ledger_entries.append(
        {"ledger_tx_id": tx_id, "account_id": account.account_id, "signed_amount_minor": -amount_minor}
    )
    state.ledger_entries.append(
        {"ledger_tx_id": tx_id, "account_id": clearing_id, "signed_amount_minor": amount_minor}
    )
    account.posted_minor -= amount_minor
    account.reserved_minor -= amount_minor
    # 对方（清分/收款）账户同步入账：分录与余额必须一致
    state.accounts[clearing_id].posted_minor += amount_minor
    reservation.remaining_minor -= amount_minor
    reservation.captured_minor += amount_minor
    reservation.status = "captured" if reservation.remaining_minor == 0 else "partially_captured"
    # 捕获属于"该操作的资金效果"，标记占用避免二次记账
    operation = state.operations.get(str(reservation.reservation_id).removeprefix("res_"))
    if operation is not None:
        operation.posted = True
    return state


def release_reservation(state: ModelState, *, reservation_id: str, reason: str = "") -> ModelState:
    """释放：只能释放"确认未占用"的部分；未知在途不得释放。"""
    reservation = state.reservations.get(reservation_id)
    if reservation is None:
        raise TransitionError("UNKNOWN_RESERVATION")
    if reservation.status == "released":
        return state
    if reservation.unknown_inflight:
        raise TransitionError("UNKNOWN_INFLIGHT_MUST_NOT_RELEASE")
    account = state.account(reservation.account_id)
    released = reservation.remaining_minor
    account.reserved_minor -= released
    reservation.released_minor += released
    reservation.remaining_minor = 0
    reservation.status = "released"
    state.audit.append({"event": "reservation.released", "reservation_id": reservation_id, "reason": reason})
    return state


def expire_reservation(state: ModelState, *, reservation_id: str) -> ModelState:
    """过期释放：只对未被未知在途占用的预留生效。"""
    reservation = state.reservations.get(reservation_id)
    if reservation is None:
        raise TransitionError("UNKNOWN_RESERVATION")
    if reservation.unknown_inflight:
        raise TransitionError("UNKNOWN_INFLIGHT_MUST_NOT_RELEASE")
    if reservation.long_lived:
        raise TransitionError("LONG_LIVED_NOT_EXPIRED")
    return release_reservation(state, reservation_id=reservation_id, reason="expired")


def action_authorize_any(state: ModelState, *, operation_id: str, grant_id: str | None = None) -> ModelState:
    """授权动作的统一入口（对一次性授权可被调用两次，用于覆盖二次消费反例）。"""
    return action_authorize(state, operation_id=operation_id, grant_id=grant_id)


# ---------------------------------------------------------------------------
# 动作生成器：给出有界交错
# ---------------------------------------------------------------------------


def candidate_actions(state: ModelState) -> list[tuple[Action, Callable[[], ModelState]]]:
    """在当前状态下枚举可用动作（含"故意重复"的动作，用来暴露反例）。"""
    actions: list[tuple[Action, Callable[[], ModelState]]] = []

    # 1) 打开新操作（受每任务 2 个操作与金额有限域限制）
    for owner, account_id in ACCOUNTS:
        for amount in MONEY_DOMAIN:
            task_index = state.task_operations.get("t1", 0)
            if task_index >= MAX_OPERATIONS_PER_TASK:
                continue
            operation_id = f"t1_op{task_index + 1}"
            if operation_id in state.operations:
                continue
            level = "YELLOW" if amount <= YELLOW_LIMIT_MINOR else "RED"
            actions.append(
                (
                    Action("open", (owner, account_id, amount, level)),
                    lambda o=owner, a=account_id, m=amount, lv=level, oid=operation_id: action_open_operation(
                        state.clone(),
                        operation_id=oid,
                        owner=o,
                        account_id=a,
                        amount_minor=m,
                        level=lv,
                        required_grant=lv != "GREEN",
                    ),
                )
            )
            # 打开时直接绑定一个不匹配的授权（缺强验证），用于覆盖"绕过强验证"的反例
            if level == RED:
                actions.append(
                    (
                        Action("open_with_weak_grant", (owner, account_id, amount)),
                        lambda o=owner, a=account_id, m=amount, oid=operation_id: action_open_operation(
                            state.clone(),
                            operation_id=oid,
                            owner=o,
                            account_id=a,
                            amount_minor=m,
                            level="RED",
                            required_grant=True,
                            grant_id="g_weak",
                        ),
                    )
                )

    # 2) 签发授权（一次性 / 范围 / 弱验证）
    for owner, _account in ACCOUNTS:
        for grant_id, level, verification, max_uses, max_total in (
            ("g_strong", RED, "TOTP", 1, 0),
            ("g_weak", RED, "NONE", 1, 0),
            ("g_yellow", "YELLOW", "NONE", 2, 1_000),
        ):
            if grant_id in state.grants:
                continue
            actions.append(
                (
                    Action("issue_grant", (grant_id, level)),
                    lambda g=grant_id, lv=level, v=verification, mu=max_uses, mt=max_total, o=owner: action_issue_grant(
                        state.clone(), grant_id=g, owner=o, level=lv, verification_level=v, max_uses=mu, max_total_minor=mt
                    ),
                )
            )

    # 3) 已有操作上的推进动作
    for operation_id, operation in state.operations.items():
        if operation.status == "created":
            for grant_id in [*state.grants.keys(), None]:
                if grant_id is None and operation.required_grant:
                    continue
                actions.append(
                    (
                        Action("authorize", (operation_id, grant_id)),
                        lambda oid=operation_id, g=grant_id: action_authorize(state.clone(), operation_id=oid, grant_id=g),
                    )
                )
        if operation.status == "authorized":
            actions.append(
                (Action("submit", (operation_id,)), lambda oid=operation_id: action_submit(state.clone(), operation_id=oid))
            )
        if operation.status in ("authorized", "dispatching", "pending", "unknown", "succeeded"):
            actions.append(
                (
                    Action("submit_again", (operation_id,)),
                    lambda oid=operation_id: action_submit_again(state.clone(), operation_id=oid),
                )
            )
        if operation.status == "dispatching":
            actions.append(
                (
                    Action("dispatch_unknown", (operation_id,)),
                    lambda oid=operation_id: action_dispatch_unknown(state.clone(), operation_id=oid),
                )
            )
        if operation.status in ("dispatching", "pending", "unknown"):
            actions.append(
                (
                    Action("receipt_success", (operation_id,)),
                    lambda oid=operation_id: action_receipt_success(state.clone(), operation_id=oid),
                )
            )
            actions.append(
                (
                    Action("receipt_failure", (operation_id,)),
                    lambda oid=operation_id: action_receipt_failure(state.clone(), operation_id=oid),
                )
            )
        if operation.status not in ("succeeded", "failed", "cancelled"):
            actions.append(
                (Action("cancel", (operation_id,)), lambda oid=operation_id: action_cancel(state.clone(), operation_id=oid))
            )

    # 4) 预留上的动作（捕获 / 释放 / 过期）
    for reservation_id, reservation in state.reservations.items():
        if reservation.status in ("captured", "released"):
            continue
        for amount in (0, reservation.remaining_minor):
            if amount <= 0:
                continue
            actions.append(
                (
                    Action("capture", (reservation_id, amount)),
                    lambda rid=reservation_id, m=amount: capture_reservation(
                        state.clone(), reservation_id=rid, amount_minor=m
                    ),
                )
            )
        actions.append(
            (
                Action("release", (reservation_id,)),
                lambda rid=reservation_id: release_reservation(state.clone(), reservation_id=rid, reason="user_cancelled"),
            )
        )
        actions.append(
            (
                Action("expire", (reservation_id,)),
                lambda rid=reservation_id: expire_reservation(state.clone(), reservation_id=rid),
            )
        )

    return actions


def action_issue_grant(
    state: ModelState,
    *,
    grant_id: str,
    owner: str,
    level: str,
    verification_level: str,
    max_uses: int,
    max_total_minor: int,
) -> ModelState:
    if grant_id in state.grants:
        raise TransitionError("GRANT_EXISTS")
    state.grants[grant_id] = GrantState(
        grant_id=grant_id,
        owner_user_id=owner,
        required_level=level,
        max_uses=max_uses,
        max_total_minor=max_total_minor,
        status="active",
        verification_level=verification_level,
    )
    return state


def enumerate_states(max_actions: int = 4) -> Iterator[tuple[ModelState, tuple[str, ...]]]:
    """广度优先枚举长度 ≤ ``max_actions`` 的动作序列（去重后产出不同状态）。"""
    start = initial_state()
    frontier: list[tuple[ModelState, tuple[str, ...]]] = [(start, ())]
    seen: set[str] = {start.key()}
    yield start, ()
    for depth in range(max_actions):
        next_frontier: list[tuple[ModelState, tuple[str, ...]]] = []
        for state, path in frontier:
            for action, thunk in candidate_actions(state):
                try:
                    new_state = thunk()
                except TransitionError:
                    continue
                except Exception as exc:  # 未预期的异常必须暴露，不能吞
                    raise RuntimeError(f"动作 {action.label()} 抛出未预期异常: {exc!r}") from exc
                key = new_state.key()
                if key in seen:
                    continue
                seen.add(key)
                next_frontier.append((new_state, path + (action.label(),)))
                yield new_state, path + (action.label(),)
        frontier = next_frontier
        if not frontier:
            break


def state_space_report(max_actions: int = 4) -> dict:
    """统计有界状态空间规模（用于报告"枚举了多少"）。"""
    states = 0
    max_depth = 0
    transitions = 0
    for _state, path in enumerate_states(max_actions=max_actions):
        states += 1
        max_depth = max(max_depth, len(path))
    # 单独统计边数（与状态数分开报告，避免把二者混为一谈）
    frontier: list[tuple[ModelState, tuple[str, ...]]] = [(initial_state(), ())]
    seen: set[str] = {frontier[0][0].key()}
    for _depth in range(max_actions):
        next_frontier = []
        for state, path in frontier:
            for _action, thunk in candidate_actions(state):
                try:
                    new_state = thunk()
                except TransitionError:
                    continue
                transitions += 1
                key = new_state.key()
                if key not in seen:
                    seen.add(key)
                    next_frontier.append((new_state, path + ("x",)))
        frontier = next_frontier
    return {
        "max_actions": max_actions,
        "states": states,
        "transitions_explored": transitions,
        "max_depth_reached": max_depth,
        "money_domain": list(MONEY_DOMAIN),
        "max_operations_per_task": MAX_OPERATIONS_PER_TASK,
        "accounts": len(ACCOUNTS),
        "users": len(USERS),
        "note": "有界：只枚举长度 ≤ max_actions 的动作序列；不覆盖真实并发与网络分区。",
    }


def product_states(*states: ModelState) -> list[ModelState]:
    """笛卡尔积辅助（保留给扩展使用；当前枚举不需要）。"""
    return [copy.deepcopy(item) for item in itertools.chain(states)]


__all__ = [
    "MONEY_DOMAIN",
    "MAX_OPERATIONS_PER_TASK",
    "YELLOW_LIMIT_MINOR",
    "USERS",
    "ACCOUNTS",
    "OPERATION_STATUSES",
    "AccountState",
    "ReservationState",
    "OperationState",
    "GrantState",
    "ModelState",
    "Action",
    "TransitionError",
    "initial_state",
    "candidate_actions",
    "enumerate_states",
    "state_space_report",
    "capture_reservation",
    "release_reservation",
    "expire_reservation",
]