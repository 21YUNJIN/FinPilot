"""有界不变量检查的测试（PLAN-08、T57）。

三类用例，缺一不可：
1. **正例**：模型在给定界内满足全部不变量集群；
2. **变异（mutation）**：故意改坏一个迁移，检查必须报错——否则"全绿"没有意义；
3. **边界与诚实性**：报告必须给出状态空间规模与**未覆盖项**，并明确"这不是证明"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.formal import check_invariants, state_space_report  # noqa: E402
from eval.formal import transition_model as tm  # noqa: E402
from eval.formal.check_invariants import (  # noqa: E402
    CHECK_NAMES,
    UNCOVERED_ITEMS,
    check_balance_non_negative,
    check_capture_no_double_count,
    check_grant_single_use,
    check_ledger_matches_accounts,
    check_ledger_split_balance,
    check_no_cross_user_write,
    check_no_hold_leak,
    check_operation_posts_once,
    check_unknown_hold_not_released,
)


def _state_with_grant() -> tm.ModelState:
    state = tm.initial_state()
    return tm.action_issue_grant(
        state,
        grant_id="g1",
        owner="u1",
        level="RED",
        verification_level="TOTP",
        max_uses=1,
        max_total_minor=0,
    )


# ---------------------------------------------------------------------------
# 1. 正例：界内无违反
# ---------------------------------------------------------------------------


def test_bounded_state_space_is_finite_and_reported():
    report = state_space_report(max_actions=3)
    assert report["max_actions"] == 3
    assert report["states"] > 100
    assert report["transitions_explored"] > report["states"]
    assert report["money_domain"] == [0, 100, 200, 1000, 1100]
    assert report["max_operations_per_task"] == 2
    assert report["users"] == 2
    assert report["accounts"] == 4


def test_all_invariants_hold_within_bound():
    report = check_invariants(max_actions=4)
    assert report["failed_checks"] == {}, report["counterexamples"]
    assert set(report["checks_passed"]) == set(CHECK_NAMES)
    assert report["states_enumerated"] > 1000
    assert report["transitions_checked"] > report["states_enumerated"]
    assert report["max_depth_reached"] == 4


def test_report_states_scope_and_uncovered_items_honestly():
    report = check_invariants(max_actions=3)
    assert report["bounded_model_check"] is True
    assert "不是对完整实现的证明" in report["claim"]
    assert report["uncovered"] == list(UNCOVERED_ITEMS)
    # 必须显式声明不覆盖真实并发
    assert any("并发" in item for item in report["uncovered"])
    assert any("应急金" in item for item in report["uncovered"])


def test_report_is_json_serializable():
    import json

    report = check_invariants(max_actions=2)
    text = json.dumps(report, ensure_ascii=False)
    assert "bounded_model_check" in text


# ---------------------------------------------------------------------------
# 2. 变异测试：改坏迁移后检查必须报错
# ---------------------------------------------------------------------------


def test_ledger_split_balance_detects_unbalanced_transaction():
    state = tm.initial_state()
    state.ledger_transactions.append(
        {"ledger_tx_id": "ltx_bad", "operation_id": "op_bad", "kind": "transfer", "sum_minor": 0}
    )
    state.ledger_entries.append(
        {"ledger_tx_id": "ltx_bad", "account_id": "a1", "signed_amount_minor": -100}
    )
    # 少了对方分录：Σ = -100 ≠ 0
    violations = check_ledger_split_balance(state, ("mutate",))
    assert violations and violations[0].check == "ledger_split_balance"
    assert violations[0].detail["sum_minor"] == -100


def test_ledger_matches_accounts_detects_untracked_deduction():
    state = tm.initial_state()
    state.ledger_transactions.append(
        {"ledger_tx_id": "ltx_bad", "operation_id": "op_bad", "kind": "transfer", "sum_minor": 0}
    )
    state.ledger_entries.extend(
        [
            {"ledger_tx_id": "ltx_bad", "account_id": "a1", "signed_amount_minor": -100},
            {"ledger_tx_id": "ltx_bad", "account_id": "clearing", "signed_amount_minor": 100},
        ]
    )
    # 账户余额没跟着扣：余额与分录不一致
    violations = check_ledger_matches_accounts(state, ("mutate",))
    assert violations
    assert violations[0].detail["expected_posted_minor"] == 400
    assert violations[0].detail["actual_posted_minor"] == 500


def test_grant_single_use_detects_double_consumption():
    state = _state_with_grant()
    state.grants["g1"].used_count = 2  # 突破 max_uses=1
    violations = check_grant_single_use(state, ("mutate",))
    assert violations and violations[0].detail["used_count"] == 2


def test_balance_non_negative_detects_negative_balance():
    state = tm.initial_state()
    state.accounts["a1"].posted_minor = -1
    violations = check_balance_non_negative(state, ("mutate",))
    assert violations and violations[0].detail["posted_minor"] == -1


def test_unknown_hold_not_released_detects_illegal_release():
    state = tm.initial_state()
    state.reservations["res_x"] = tm.ReservationState(
        reservation_id="res_x",
        account_id="a1",
        owner_user_id="u1",
        original_minor=100,
        remaining_minor=0,
        status="released",
        unknown_inflight=True,
    )
    violations = check_unknown_hold_not_released(state, ("mutate",))
    assert violations and violations[0].check == "unknown_hold_not_released"


def test_no_hold_leak_detects_reserved_mismatch():
    state = tm.initial_state()
    state.accounts["a1"].reserved_minor = 50  # 没有任何预留支撑
    violations = check_no_hold_leak(state, ("mutate",))
    assert violations and violations[0].detail["reserved_minor"] == 50


def test_operation_posts_once_detects_double_posting():
    state = tm.initial_state()
    for suffix in ("a", "b"):
        tx_id = f"ltx_{suffix}"
        state.ledger_transactions.append(
            {"ledger_tx_id": tx_id, "operation_id": "op_dup", "kind": "transfer", "sum_minor": 0}
        )
        state.ledger_entries.extend(
            [
                {"ledger_tx_id": tx_id, "account_id": "a1", "signed_amount_minor": -100},
                {"ledger_tx_id": tx_id, "account_id": "clearing", "signed_amount_minor": 100},
            ]
        )
    violations = check_operation_posts_once(state, ("mutate",))
    assert violations and violations[0].detail["kinds"] == ["transfer", "transfer"]


def test_no_cross_user_write_detects_foreign_account_charge():
    state = tm.initial_state()
    state.operations["op_x"] = tm.OperationState(
        operation_id="op_x",
        owner_user_id="u1",
        account_id="a3",  # 属于 u2
        amount_minor=100,
        level="YELLOW",
    )
    state.ledger_transactions.append(
        {"ledger_tx_id": "ltx_x", "operation_id": "op_x", "kind": "transfer", "sum_minor": 0}
    )
    state.ledger_entries.extend(
        [
            {"ledger_tx_id": "ltx_x", "account_id": "a3", "signed_amount_minor": -100},
            {"ledger_tx_id": "ltx_x", "account_id": "clearing", "signed_amount_minor": 100},
        ]
    )
    violations = check_no_cross_user_write(state, ("mutate",))
    assert violations and violations[0].detail["account_owner"] == "u2"


def test_capture_no_double_count_detects_over_capture():
    state = tm.initial_state()
    state.reservations["res_x"] = tm.ReservationState(
        reservation_id="res_x",
        account_id="a1",
        owner_user_id="u1",
        original_minor=100,
        remaining_minor=100,
        captured_minor=150,  # 捕获 + 剩余 > 原始
        status="partially_captured",
    )
    violations = check_capture_no_double_count(state, ("mutate",))
    assert violations


# ---------------------------------------------------------------------------
# 3. 关键路径：正常捕获与释放不产生违反
# ---------------------------------------------------------------------------


def test_happy_path_capture_keeps_invariants():
    state = tm.initial_state()
    state = tm.action_issue_grant(
        state, grant_id="g1", owner="u1", level="RED", verification_level="TOTP", max_uses=1, max_total_minor=0
    )
    state = tm.action_open_operation(
        state,
        operation_id="t1_op1",
        owner="u1",
        account_id="a1",
        amount_minor=200,
        level="RED",
        required_grant=True,
        grant_id="g1",
    )
    state = tm.action_authorize(state, operation_id="t1_op1", grant_id="g1")
    state = tm.action_submit(state, operation_id="t1_op1")
    # 提交阶段：只剩资金占用，未记账
    assert state.account("a1").posted_minor == 500
    assert state.account("a1").reserved_minor == 200
    state = tm.action_dispatch_unknown(state, operation_id="t1_op1")
    state = tm.action_receipt_success(state, operation_id="t1_op1")
    # 成功结算：posted 与 reserved 同减
    assert state.account("a1").posted_minor == 300
    assert state.account("a1").reserved_minor == 0
    assert state.operations["t1_op1"].posted is True
    for checker in (
        check_ledger_split_balance,
        check_ledger_matches_accounts,
        check_grant_single_use,
        check_no_cross_user_write,
        check_unknown_hold_not_released,
        check_balance_non_negative,
        check_capture_no_double_count,
        check_operation_posts_once,
        check_no_hold_leak,
    ):
        assert checker(state, ("happy",)) == [], checker.__name__


def test_unknown_inflight_reservation_cannot_be_released():
    state = tm.initial_state()
    state = tm.action_issue_grant(
        state, grant_id="g1", owner="u1", level="RED", verification_level="TOTP", max_uses=1, max_total_minor=0
    )
    state = tm.action_open_operation(
        state,
        operation_id="t1_op1",
        owner="u1",
        account_id="a1",
        amount_minor=100,
        level="RED",
        required_grant=True,
        grant_id="g1",
    )
    state = tm.action_authorize(state, operation_id="t1_op1", grant_id="g1")
    state = tm.action_submit(state, operation_id="t1_op1")
    state = tm.action_dispatch_unknown(state, operation_id="t1_op1")
    with pytest.raises(tm.TransitionError):
        tm.release_reservation(state, reservation_id="res_t1_op1", reason="user_cancelled")
    with pytest.raises(tm.TransitionError):
        tm.expire_reservation(state, reservation_id="res_t1_op1")
    # 未知在途也禁止直接判失败（必须先按原键查询）
    with pytest.raises(tm.TransitionError):
        tm.action_receipt_failure(state, operation_id="t1_op1")


def test_weak_grant_cannot_authorize_red_operation():
    state = tm.action_issue_grant(
        tm.initial_state(),
        grant_id="g_weak",
        owner="u1",
        level="RED",
        verification_level="NONE",
        max_uses=1,
        max_total_minor=0,
    )
    state = tm.action_open_operation(
        state,
        operation_id="t1_op1",
        owner="u1",
        account_id="a1",
        amount_minor=1100,
        level="RED",
        required_grant=True,
        grant_id="g_weak",
    )
    with pytest.raises(tm.TransitionError):
        tm.action_authorize(state, operation_id="t1_op1", grant_id="g_weak")


def test_cross_user_account_cannot_be_opened():
    with pytest.raises(tm.TransitionError):
        tm.action_open_operation(
            tm.initial_state(),
            operation_id="t1_op1",
            owner="u1",
            account_id="a3",  # u2 的账户
            amount_minor=100,
            level="YELLOW",
            required_grant=True,
        )


def test_second_operation_per_task_is_allowed_but_third_is_not():
    state = tm.initial_state()
    state = tm.action_open_operation(
        state, operation_id="t1_op1", owner="u1", account_id="a1", amount_minor=100, level="YELLOW", required_grant=True
    )
    state = tm.action_open_operation(
        state, operation_id="t1_op2", owner="u1", account_id="a1", amount_minor=100, level="YELLOW", required_grant=True
    )
    with pytest.raises(tm.TransitionError):
        tm.action_open_operation(
            state, operation_id="t1_op3", owner="u1", account_id="a1", amount_minor=100, level="YELLOW", required_grant=True
        )