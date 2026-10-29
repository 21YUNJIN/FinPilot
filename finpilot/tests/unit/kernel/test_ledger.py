"""账本与资金预留不变量（V5 8.1、8.3、MONEY-01、MONEY-05、T23、T50）。

所有断言给出**手算期望**，不依赖实现自证。
"""

from __future__ import annotations

import pytest

from finpilot.errors import FinPilotError
from finpilot.kernel import ledger, reservations, snapshots
from finpilot.models import Account, LedgerEntry, LedgerTransaction, Reservation

pytestmark = [pytest.mark.integration]


def test_snapshot_formula_matches_hand_calculation(session, seed, ctx):
    """B=900000、H=0、F=0、E=800000 → 可用 900000，可支配 100000。"""
    snap = snapshots.get_snapshot(session, ctx)
    account = snap["accounts"][0]
    assert account["posted_balance_minor"] == 900_000
    assert account["reserved_minor"] == 0
    assert account["available_minor"] == 900_000
    assert account["spendable_minor"] == 100_000
    assert snap["protected_floor_minor"] == 800_000


def test_reservation_creation_does_not_change_balance(session, seed, ctx):
    """预留创建不改变 B，只减少可支配金额。"""
    reservations.create_reservation(session, ctx, account_id="account_salary", amount_minor=80_000, task_id="t1")
    session.commit()
    account = session.get(Account, "account_salary")
    snap = snapshots.get_snapshot(session, ctx)
    assert account.posted_balance_minor == 900_000, "预留不得改变已记账余额"
    assert snap["accounts"][0]["available_minor"] == 820_000
    assert snap["accounts"][0]["spendable_minor"] == 20_000


def test_capture_reduces_balance_and_reservation_once(session, seed, ctx):
    """B=1000 元、H=800 元；捕获 300 元 → B=700、H=500、可用仍 200；不得双减。"""
    reservation = reservations.create_reservation(
        session, ctx, account_id="account_salary", amount_minor=80_000, task_id="t1"
    )
    session.commit()
    reservations.capture_reservation(
        session, ctx, reservation_id=reservation.reservation_id, amount_minor=30_000, operation_id="op_capture_1"
    )
    session.commit()

    account = session.get(Account, "account_salary")
    stored = session.get(Reservation, reservation.reservation_id)
    assert account.posted_balance_minor == 870_000, "捕获只减少一次余额"
    assert stored.remaining_minor == 50_000
    assert stored.captured_minor == 30_000
    assert ledger.account_available_minor(session, "account_salary") == 820_000


def test_release_restores_spendable_amount(session, seed, ctx):
    reservation = reservations.create_reservation(
        session, ctx, account_id="account_salary", amount_minor=80_000, task_id="t1"
    )
    session.commit()
    reservations.release_reservation(session, ctx, reservation_id=reservation.reservation_id, amount_minor=50_000)
    session.commit()
    stored = session.get(Reservation, reservation.reservation_id)
    assert stored.remaining_minor == 30_000
    assert stored.released_minor == 50_000
    assert ledger.account_available_minor(session, "account_salary") == 870_000


def test_reservation_cannot_breach_protected_floor(session, seed, ctx):
    """可支配仅 100000 分，预留 100001 分必须被拒且不留下任何记录。"""
    with pytest.raises(FinPilotError) as excinfo:
        reservations.create_reservation(session, ctx, account_id="account_salary", amount_minor=100_001, task_id="t1")
    assert excinfo.value.code == "PROTECTED_FLOOR_VIOLATION"
    session.rollback()
    assert session.query(Reservation).count() == 0


def test_ledger_transaction_must_balance(session, seed):
    with pytest.raises(FinPilotError) as excinfo:
        ledger.post_transaction(
            session,
            kind="broken",
            entries=[("account_salary", -100), ("account_clearing", 90)],
            operation_id="op_broken",
        )
    assert excinfo.value.code == "SYSTEM_ERROR"
    assert "不守恒" in (excinfo.value.message or "")


def test_ledger_is_double_entry_and_conserved(session, seed):
    initial_clearing = session.get(Account, "account_clearing").posted_balance_minor
    ledger.post_transaction(
        session,
        kind="transfer",
        entries=[("account_salary", -10_000), ("account_clearing", 10_000)],
        operation_id="op_double_entry",
    )
    session.commit()
    tx = session.query(LedgerTransaction).filter_by(operation_id="op_double_entry").one()
    entries = session.query(LedgerEntry).filter_by(ledger_tx_id=tx.ledger_tx_id).all()
    assert len(entries) == 2, "外部付款用清算账户补齐分录，不允许单边改余额"
    assert sum(entry.signed_amount_minor for entry in entries) == 0
    assert session.get(Account, "account_salary").posted_balance_minor == 890_000
    assert session.get(Account, "account_clearing").posted_balance_minor == initial_clearing + 10_000


def test_same_operation_and_kind_cannot_post_twice(session, seed):
    """(operation_id, kind) 唯一：同一业务效果只入账一次。"""
    from sqlalchemy.exc import IntegrityError

    ledger.post_transaction(
        session, kind="transfer", entries=[("account_salary", -5_000), ("account_clearing", 5_000)], operation_id="op_dup"
    )
    session.commit()
    with pytest.raises(IntegrityError):
        ledger.post_transaction(
            session, kind="transfer", entries=[("account_salary", -5_000), ("account_clearing", 5_000)], operation_id="op_dup"
        )
    session.rollback()
    assert session.get(Account, "account_salary").posted_balance_minor == 895_000


def test_insufficient_funds_rejected_without_side_effect(session, seed):
    """可用余额之外的支出被拒，且不产生任何分录。"""
    with pytest.raises(FinPilotError) as excinfo:
        ledger.post_transaction(
            session,
            kind="transfer",
            entries=[("account_demo2", -600_000), ("account_clearing", 600_000)],
            operation_id="op_insufficient",
        )
    assert excinfo.value.code == "INSUFFICIENT_FUNDS"
    session.rollback()
    assert session.query(LedgerTransaction).count() == 0


def test_multi_account_protection_group_subtracts_floor_once(session, seed, ctx):
    """多账户保护组：B/H/F 汇总，E 只扣一次。"""
    from finpilot.models import ProtectionPolicy

    policy = session.query(ProtectionPolicy).one()
    policy.account_scope = ["account_salary", "account_savings"]
    session.commit()
    snap = snapshots.get_snapshot(session, ctx)
    group = snap["group"]
    assert group["posted_minor"] == 2_400_000, "9000 + 15000 元"
    assert group["spendable_minor"] == 1_600_000, "应急金 8000 元只扣一次"


def test_expire_idle_reservations_skips_inflight(session, seed, ctx):
    """在途未知金额不得因 TTL 到期被释放。"""
    from finpilot.kernel.reservations import mark_inflight

    reservation = reservations.create_reservation(
        session, ctx, account_id="account_salary", amount_minor=50_000, task_id="t1"
    )
    session.commit()
    mark_inflight(session, reservation_id=reservation.reservation_id, amount_minor=10_000)
    session.commit()
    expired = reservations.expire_idle_reservations(session, now=seed["reference_time"] + __import__("datetime").timedelta(hours=2))
    assert reservation.reservation_id not in expired
    assert session.get(Reservation, reservation.reservation_id).status == "active"


def test_long_lived_reservation_not_expired(session, seed, ctx):
    from datetime import timedelta

    reservation = reservations.create_reservation(
        session,
        ctx,
        account_id="account_salary",
        amount_minor=50_000,
        task_id="t1",
        long_lived=True,
        expires_at=seed["reference_time"] + timedelta(days=3),
    )
    session.commit()
    expired = reservations.expire_idle_reservations(session, now=seed["reference_time"] + timedelta(days=4))
    assert reservation.reservation_id not in expired


def test_unconfirmed_inflows_are_not_spendable(session, seed, ctx):
    """待收 AA 与未到账退款单列，不进入可用资金。"""
    from finpilot.models import AARequest, AAShare, Refund

    aa = AARequest(
        aa_id="aa_test",
        owner_user_id="user_demo_01",
        title="测试",
        final_cost_minor=10_000,
        status="open",
        created_at=seed["reference_time"],
        updated_at=seed["reference_time"],
    )
    session.add(aa)
    session.flush()
    session.add(
        AAShare(
            aa_id="aa_test",
            participant_ref="p1",
            display_name="他人",
            share_minor=3_334,
            status="pending",
            is_initiator=False,
            created_at=seed["reference_time"],
        )
    )
    session.add(
        Refund(
            owner_user_id="user_demo_01",
            operation_id="op_paid",
            original_operation_id="op_paid",
            amount_minor=5_000,
            status="pending",
            created_at=seed["reference_time"],
        )
    )
    session.commit()
    snap = snapshots.get_snapshot(session, ctx)
    assert snap["pending_receivables_minor"] == 3_334
    assert snap["unconfirmed_refunds_minor"] == 5_000
    assert snap["accounts"][0]["available_minor"] == 900_000, "待收与待退不增加可用资金"
    assert snap["unconfirmed_receivables_are_spendable"] is False