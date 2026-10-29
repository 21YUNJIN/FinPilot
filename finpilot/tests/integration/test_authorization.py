"""分级授权、强验证、幂等与并发（V5 6、8、AUTH-01..05、MONEY-02/03、T16—T20、T26、T35）。

并发用例使用真实多连接与线程，跑在真实 PostgreSQL 上；不使用 SQLite 替代并发保证。
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from finpilot.errors import FinPilotError
from finpilot.kernel import identity, limits, submission, verifier
from finpilot.kernel.grants import (
    create_exact_grant,
    create_scoped_grant,
    grant_covers,
    revoke_grant,
)
from finpilot.kernel.identity import AuthContext
from finpilot.kernel.submission import OperationDraft, prepare_operation, submit_operation
from finpilot.models import Account, Grant, GrantUse, LedgerTransaction, Operation
from finpilot.security.scope import build_scope
from finpilot.timeutil import utcnow
from finpilot.workers.outbox import drain

pytestmark = [pytest.mark.integration]


def _ctx(user_id: str = "user_demo_01") -> AuthContext:
    return AuthContext(user_id=user_id, roles=("customer",), channel="app", session_id="sess")


def _draft(amount: int, *, operation_id: str, source: str = "account_salary", payee: str = "payee_lisi", task: str = "task_x") -> OperationDraft:
    return OperationDraft(
        tool="transfer.submit",
        typed_args={
            "payee_id": payee,
            "source_account_id": source,
            "amount_minor": amount,
            "fee_minor": 0,
        },
        operation_id=operation_id,
        task_id=task,
        plan_id="plan_x",
        plan_revision=1,
        source_account_id=source,
        amount_minor=amount,
    )


def _exact_grant_for(session, ctx, operation, level: str):
    scope = build_scope(
        operation.tool,
        operation.typed_args,
        task_id=operation.task_id,
        plan_id=operation.plan_id,
        plan_revision=operation.plan_revision,
        parameters_hash=operation.parameters_hash,
        operation_id=operation.operation_id,
        required_level=level,
    )
    grant = create_exact_grant(
        session, ctx, plan_id=operation.plan_id, plan_revision=operation.plan_revision, scope=scope, required_level=level
    )
    session.flush()
    return grant


def test_daily_limit_boundary_is_inclusive(session, seed, ctx):
    """恰好 100000 分为 YELLOW，超过为 RED（1000 元是等级边界，不是限额）。"""
    assert limits.grade_transfer(session, ctx, principal_minor=100_000) == "YELLOW"
    assert limits.grade_transfer(session, ctx, principal_minor=100_001) == "RED"


def test_daily_limit_counts_settled_and_held(session, seed, ctx):
    """日累计 = 已成功 + 待执行占用；手续费不计入本金累计。"""
    limits.hold_daily_quota(session, ctx, operation_id="op_hold_1", principal_minor=90_000)
    session.commit()
    assert limits.grade_transfer(session, ctx, principal_minor=10_000) == "YELLOW"
    assert limits.grade_transfer(session, ctx, principal_minor=10_001) == "RED"
    usage = limits.daily_usage(session, ctx)
    assert usage["held_pending_minor"] == 90_000
    assert usage["settled_transfer_minor"] == 0


def test_fee_not_counted_in_principal_cumulative(session, seed, ctx):
    limits.hold_daily_quota(session, ctx, operation_id="op_hold_fee", principal_minor=100_000)
    session.commit()
    # 手续费 10 元不改变本金累计口径
    assert limits.grade_transfer(session, ctx, principal_minor=1) == "RED"


def test_yellow_flow_settles_and_releases_quota(session, seed, ctx):
    operation = prepare_operation(session, ctx, _draft(10_000, operation_id="op_yellow_1"))
    grant = _exact_grant_for(session, ctx, operation, "YELLOW")
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()
    drain(session)
    session.commit()

    stored = session.get(Operation, "op_yellow_1")
    assert stored.status == "succeeded"
    assert session.get(Account, "account_salary").posted_balance_minor == 890_000
    usage = limits.daily_usage(session, ctx)
    assert usage["settled_transfer_minor"] == 10_000
    assert usage["held_pending_minor"] == 0
    assert session.query(LedgerTransaction).count() == 1


def test_red_requires_strong_verification(session, seed, ctx, clock):
    """RED 操作仅凭 YELLOW 授权不能提交。"""
    operation = prepare_operation(session, ctx, _draft(200_000, operation_id="op_red_1", source="account_savings"))
    assert operation.required_level == "RED"
    yellow_grant = _exact_grant_for(session, ctx, operation, "YELLOW")
    session.commit()
    with pytest.raises(FinPilotError) as excinfo:
        submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=yellow_grant.grant_id)
    assert excinfo.value.code in ("GRANT_REQUIRED", "GRANT_SCOPE_VIOLATION")
    session.rollback()
    assert session.get(Account, "account_savings").posted_balance_minor == 1_500_000


def test_totp_challenge_flow_issues_scoped_grant(session, seed, ctx, clock):
    """正确 TOTP → 签发带 TOTP 验证等级的授权；挑战只能消费一次。"""
    secret = seed["totp_secret"]
    scope = {
        "kind": "exact",
        "tool": "transfer.submit",
        "task_id": "task_red",
        "plan_id": "plan_red",
        "plan_revision": 1,
        "amount_minor": 200_000,
        "required_level": "RED",
    }
    challenge = verifier.issue_challenge(session, ctx, scope=scope, plan_id="plan_red", plan_revision=1)
    session.commit()
    code, _counter = identity.totp_at(secret, utcnow())
    result = verifier.verify_challenge(session, ctx, challenge_id=challenge.challenge_id, factor_response=code)
    session.commit()
    assert result["grant"]["verification_level"] == "TOTP"
    assert result["grant"]["status"] == "active"

    with pytest.raises(FinPilotError) as excinfo:
        verifier.verify_challenge(session, ctx, challenge_id=challenge.challenge_id, factor_response=code)
    assert excinfo.value.code == "GRANT_INVALID"
    session.rollback()


def test_totp_replay_across_challenges_is_rejected(session, seed, ctx, clock):
    """已成功消费的时间计数器不能在另一个挑战重复使用。"""
    secret = seed["totp_secret"]
    scope = {"kind": "exact", "tool": "transfer.submit", "task_id": "t", "plan_revision": 1, "amount_minor": 1000, "required_level": "RED"}
    first = verifier.issue_challenge(session, ctx, scope=scope, plan_id="p", plan_revision=1)
    session.commit()
    code, _ = identity.totp_at(secret, utcnow())
    verifier.verify_challenge(session, ctx, challenge_id=first.challenge_id, factor_response=code)
    session.commit()

    second = verifier.issue_challenge(session, ctx, scope=scope, plan_id="p", plan_revision=1)
    session.commit()
    with pytest.raises(FinPilotError) as excinfo:
        verifier.verify_challenge(session, ctx, challenge_id=second.challenge_id, factor_response=code)
    assert excinfo.value.code == "CHALLENGE_FAILED"
    session.rollback()


def test_challenge_locks_after_max_attempts(session, seed, ctx, clock):
    scope = {"kind": "exact", "tool": "transfer.submit", "task_id": "t", "plan_revision": 1, "amount_minor": 1000, "required_level": "RED"}
    challenge = verifier.issue_challenge(session, ctx, scope=scope, plan_id="p", plan_revision=1)
    session.commit()
    for _ in range(4):
        with pytest.raises(FinPilotError) as excinfo:
            verifier.verify_challenge(session, ctx, challenge_id=challenge.challenge_id, factor_response="000000")
        assert excinfo.value.code == "CHALLENGE_FAILED"
    with pytest.raises(FinPilotError) as excinfo:
        verifier.verify_challenge(session, ctx, challenge_id=challenge.challenge_id, factor_response="000000")
    assert excinfo.value.code == "CHALLENGE_LOCKED"
    session.rollback()


def test_challenge_expires(session, seed, ctx, clock):
    scope = {"kind": "exact", "tool": "transfer.submit", "task_id": "t", "plan_revision": 1, "amount_minor": 1000, "required_level": "RED"}
    challenge = verifier.issue_challenge(session, ctx, scope=scope, plan_id="p", plan_revision=1)
    session.commit()
    clock.advance(121)
    code, _ = identity.totp_at(seed["totp_secret"], utcnow())
    with pytest.raises(FinPilotError) as excinfo:
        verifier.verify_challenge(session, ctx, challenge_id=challenge.challenge_id, factor_response=code)
    assert excinfo.value.code == "CHALLENGE_EXPIRED"
    session.rollback()


def test_exact_grant_bound_to_parameters_and_revision(session, seed, ctx):
    operation = prepare_operation(session, ctx, _draft(10_000, operation_id="op_bind_1"))
    grant = _exact_grant_for(session, ctx, operation, "YELLOW")
    session.commit()

    # 篡改金额后参数哈希不匹配
    operation.typed_args = {**operation.typed_args, "amount_minor": 20_000}
    session.flush()
    covered, reason = grant_covers(
        session.get(Grant, grant.grant_id),
        plan_revision=1,
        candidate_scope={"tool": "transfer.submit", "operation_id": operation.operation_id},
        amount_minor=20_000,
        parameter_hash="deadbeef",
    )
    assert covered is False and reason == "parameter_hash_mismatch"

    # 另一修订不能复用精确授权
    covered2, reason2 = grant_covers(
        session.get(Grant, grant.grant_id),
        plan_revision=2,
        candidate_scope={"tool": "transfer.submit", "operation_id": operation.operation_id},
        amount_minor=10_000,
        parameter_hash=operation.parameters_hash,
    )
    assert covered2 is False and reason2 == "exact_grant_revision_mismatch"
    session.rollback()


def test_new_operation_id_cannot_reset_scoped_quota(session, seed, ctx):
    """范围授权共享额度与次数：换 operation_id 不等于重置。"""
    scope = {
        "kind": "scoped",
        "tool": "transfer.submit",
        "task_id": "task_scoped",
        "allowed_tools": ["transfer.submit"],
        "allowed_account_ids": ["account_savings"],
        "max_amount_per_op_minor": 10_000,
        "max_total_minor": 15_000,
        "max_uses": 2,
        "required_level": "YELLOW",
    }
    grant = create_scoped_grant(
        session,
        ctx,
        plan_id="plan_scoped",
        plan_revision=1,
        scope=scope,
        required_level="YELLOW",
        max_uses=2,
        max_total_minor=15_000,
        expires_at=utcnow() + timedelta(minutes=10),
    )
    session.commit()

    first = prepare_operation(session, ctx, _draft(10_000, operation_id="op_s1", source="account_savings"))
    submit_operation(session, ctx, operation_id=first.operation_id, grant_id=grant.grant_id)
    session.commit()

    second = prepare_operation(session, ctx, _draft(10_000, operation_id="op_s2", source="account_savings"))
    with pytest.raises(FinPilotError) as excinfo:
        submit_operation(session, ctx, operation_id=second.operation_id, grant_id=grant.grant_id)
    assert excinfo.value.code == "GRANT_SCOPE_VIOLATION"
    assert "grant_total_cap_exceeded" in str(excinfo.value.details)
    session.rollback()


def test_scope_subset_rejects_out_of_scope_merchant(session, seed, ctx):
    scope = {
        "kind": "scoped",
        "tool": "merchant.book",
        "allowed_tools": ["merchant.book"],
        "allowed_account_ids": ["account_savings"],
        "max_amount_per_op_minor": 50_000,
        "max_total_minor": 80_000,
        "max_uses": 3,
        "merchants": [{"merchant_id": "merchant_flowers"}],
        "required_level": "RED",
    }
    grant = create_scoped_grant(
        session,
        ctx,
        plan_id="p",
        plan_revision=1,
        scope=scope,
        required_level="RED",
        max_uses=3,
        max_total_minor=80_000,
        expires_at=utcnow() + timedelta(minutes=10),
        verification_level="TOTP",
    )
    session.flush()
    from finpilot.kernel.grants import activate_grant

    activate_grant(session, ctx, grant_id=grant.grant_id, verification_level="TOTP")
    session.commit()
    covered, reason = grant_covers(
        grant,
        plan_revision=1,
        candidate_scope={"tool": "merchant.book", "merchant_id": "merchant_cake"},
        amount_minor=40_000,
    )
    assert covered is False and reason == "merchant_out_of_scope"


def test_consumed_grant_cannot_be_reused(session, seed, ctx):
    operation = prepare_operation(session, ctx, _draft(10_000, operation_id="op_once"))
    grant = _exact_grant_for(session, ctx, operation, "YELLOW")
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    other = prepare_operation(session, ctx, _draft(10_000, operation_id="op_once_2"))
    with pytest.raises(FinPilotError) as excinfo:
        submit_operation(session, ctx, operation_id=other.operation_id, grant_id=grant.grant_id)
    assert excinfo.value.code in ("GRANT_INVALID", "GRANT_SCOPE_VIOLATION")
    session.rollback()
    assert session.query(GrantUse).count() == 1


def test_revoke_stops_pending_but_keeps_inflight(session, seed, ctx):
    """多步范围授权：消费一次后撤销，未提交步骤停止，已提交操作继续核验。"""
    scope = {
        "kind": "scoped",
        "tool": "transfer.submit",
        "task_id": "task_x",
        "plan_id": "plan_x",
        "allowed_tools": ["transfer.submit"],
        "allowed_account_ids": ["account_salary"],
        "max_amount_per_op_minor": 10_000,
        "max_total_minor": 30_000,
        "max_uses": 3,
        "required_level": "YELLOW",
    }
    grant = create_scoped_grant(
        session,
        ctx,
        plan_id="plan_x",
        plan_revision=1,
        scope=scope,
        required_level="YELLOW",
        max_uses=3,
        max_total_minor=30_000,
        expires_at=utcnow() + timedelta(minutes=10),
    )
    session.flush()
    operation = prepare_operation(session, ctx, _draft(10_000, operation_id="op_revoke"))
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    result = revoke_grant(session, ctx, grant_id=grant.grant_id)
    session.commit()
    assert result["grant"]["status"] == "revoked"
    # 已提交的操作继续核验，不因撤销而消失
    assert "op_revoke" in result["inflight_operations"]
    stored = session.get(Grant, grant.grant_id)
    assert stored.revoked_at is not None


def test_same_idempotency_key_with_different_parameters_conflicts(session, seed, ctx):
    first = prepare_operation(session, ctx, OperationDraft(tool="transfer.submit", typed_args={"payee_id": "payee_lisi", "source_account_id": "account_salary", "amount_minor": 10_000, "fee_minor": 0}, operation_id="op_k1", idempotency_key="key-1", source_account_id="account_salary", amount_minor=10_000))
    session.commit()
    with pytest.raises(FinPilotError) as excinfo:
        prepare_operation(
            session,
            ctx,
            OperationDraft(
                tool="transfer.submit",
                typed_args={"payee_id": "payee_lisi", "source_account_id": "account_salary", "amount_minor": 20_000, "fee_minor": 0},
                operation_id="op_k2",
                idempotency_key="key-1",
                source_account_id="account_salary",
                amount_minor=20_000,
            ),
        )
    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"
    session.rollback()


def test_concurrent_duplicate_submission_creates_single_effect(session, session_factory, seed, clock):
    """50 个并发相同操作，只允许一次金融效果（T19）。"""
    ctx = _ctx()
    operation = prepare_operation(session, ctx, _draft(10_000, operation_id="op_concurrent_same"))
    grant = _exact_grant_for(session, ctx, operation, "YELLOW")
    session.commit()
    operation_id = operation.operation_id
    grant_id = grant.grant_id

    errors: list[str] = []
    barrier = threading.Barrier(10)

    def worker() -> None:
        worker_session = session_factory()
        try:
            barrier.wait(timeout=10)
            submit_operation(worker_session, ctx, operation_id=operation_id, grant_id=grant_id)
            worker_session.commit()
        except FinPilotError as exc:
            errors.append(exc.code)
            worker_session.rollback()
        except Exception as exc:  # pragma: no cover
            errors.append(type(exc).__name__)
            worker_session.rollback()
        finally:
            worker_session.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "并发线程未在限期内结束（可能存在锁等待）"

    drain(session)
    session.commit()
    assert session.get(Account, "account_salary").posted_balance_minor == 890_000, "并发重试只产生一次扣款"
    assert session.query(LedgerTransaction).count() == 1
    assert session.get(Operation, operation_id).status == "succeeded"


def test_concurrent_split_cannot_bypass_level_upgrade(session, session_factory, seed, clock):
    """900 元累计后并发 80＋80：不能两次都以 YELLOW 通过（T16）。"""
    ctx = _ctx()
    baseline = prepare_operation(session, ctx, _draft(90_000, operation_id="op_base_90k", source="account_savings"))
    baseline_grant = _exact_grant_for(session, ctx, baseline, "YELLOW")
    submit_operation(session, ctx, operation_id=baseline.operation_id, grant_id=baseline_grant.grant_id)
    session.commit()
    drain(session)
    session.commit()
    assert limits.daily_usage(session, ctx)["settled_transfer_minor"] == 90_000

    first = prepare_operation(session, ctx, _draft(8_000, operation_id="op_split_1", source="account_savings"))
    second = prepare_operation(session, ctx, _draft(8_000, operation_id="op_split_2", source="account_savings"))
    first_grant = _exact_grant_for(session, ctx, first, "YELLOW")
    second_grant = _exact_grant_for(session, ctx, second, "YELLOW")
    session.commit()

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker(operation_id: str, grant_id: str) -> None:
        worker_session = session_factory()
        try:
            barrier.wait(timeout=10)
            submit_operation(worker_session, ctx, operation_id=operation_id, grant_id=grant_id)
            worker_session.commit()
            outcomes.append("submitted")
        except FinPilotError as exc:
            outcomes.append(exc.code)
            worker_session.rollback()
        except Exception as exc:  # pragma: no cover
            outcomes.append(type(exc).__name__)
            worker_session.rollback()
        finally:
            worker_session.close()

    threads = [
        threading.Thread(target=worker, args=("op_split_1", first_grant.grant_id)),
        threading.Thread(target=worker, args=("op_split_2", second_grant.grant_id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "并发线程未在限期内结束（可能存在锁等待）"

    drain(session)
    session.commit()
    assert outcomes.count("submitted") == 1, f"必须恰好一个通过，实际 {outcomes}"
    assert any(code in ("GRANT_REQUIRED", "GRANT_SCOPE_VIOLATION") for code in outcomes), "另一个必须因等级升级被拦"
    usage = limits.daily_usage(session, ctx)
    assert usage["settled_transfer_minor"] + usage["held_pending_minor"] <= 100_000


def test_customer_cannot_use_operator_policy_level(session, seed, ctx):
    from finpilot.security.policy import evaluate_policy

    decision = evaluate_policy(
        session,
        ctx,
        tool="operator.release_risk_freeze",
        typed_args={},
    )
    assert decision.allow is False
    assert "operator_role_required" in decision.reason_codes


def test_forbidden_argument_fields_rejected(session, seed, ctx):
    with pytest.raises(FinPilotError) as excinfo:
        prepare_operation(
            session,
            ctx,
            OperationDraft(
                tool="transfer.submit",
                typed_args={
                    "payee_id": "payee_lisi",
                    "source_account_id": "account_salary",
                    "amount_minor": 10_000,
                    "user_id": "user_demo_02",
                    "grant_id": "grant_forged",
                },
                operation_id="op_forged",
                source_account_id="account_salary",
                amount_minor=10_000,
            ),
        )
    assert excinfo.value.code == "EXTRA_DANGEROUS_FIELD"
    session.rollback()