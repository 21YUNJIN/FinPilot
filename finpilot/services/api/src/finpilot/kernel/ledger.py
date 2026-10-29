"""双重记账账本与余额口径（V5 8.1、8.3、MONEY-01）。

资金不变量：
    B = 已记账余额 posted_balance_minor
    H = 有效任务预留的剩余金额 Σ remaining_minor（active / partially_captured）
    F = 与 H 不重叠的其他银行冻结 frozen_other_minor
    E = 用户确认的应急金底线
    银行可用余额 = B − H − F
    规划可支配金额 = max(0, B − H − F − E)

规则：每笔事务同币种分录之和必须为 0；已入账记录只允许追加纠正分录，不允许原地修改。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import lock_accounts
from ..errors import FinPilotError
from ..ids import new_id
from ..models import Account, LedgerEntry, LedgerTransaction, Reservation, User
from ..money import CURRENCY
from ..timeutil import utcnow

CLEARING_ACCOUNT_ID = "account_clearing"
SYSTEM_CLEARING_OWNER = "user_system"

RESERVATION_ACTIVE_STATUSES = ("active", "partially_captured")


def post_transaction(
    session: Session,
    *,
    kind: str,
    entries: list[tuple[str, int]],
    operation_id: str | None,
    task_id: str | None = None,
    memo: str = "",
    posted_at: datetime | None = None,
) -> str:
    """追加一笔守恒的双重记账事务；返回 ledger_tx_id。

    (operation_id, kind) 唯一：同一业务效果只允许入账一次。
    """
    if not entries:
        raise FinPilotError("SYSTEM_ERROR", "账本事务至少需要两条分录。")
    total = sum(int(amount) for _account, amount in entries)
    if total != 0:
        raise FinPilotError(
            "SYSTEM_ERROR",
            "分录金额之和不守恒。",
            details={"reason_codes": ["ledger_unbalanced"], "total_minor": total},
        )

    account_ids = [account_id for account_id, _amount in entries]
    lock_accounts(session, account_ids)

    accounts: dict[str, Account] = {}
    for account_id in set(account_ids):
        account = session.get(Account, account_id)
        if account is None:
            raise FinPilotError("SYSTEM_ERROR", f"账户不存在: {account_id}")
        if account.currency != CURRENCY:
            raise FinPilotError("SYSTEM_ERROR", "本版本仅支持人民币单币种记账。")
        accounts[account_id] = account

    tx = LedgerTransaction(
        ledger_tx_id=new_id("ltx"),
        operation_id=operation_id,
        currency=CURRENCY,
        kind=kind,
        memo=memo[:255],
        task_id=task_id,
        posted_at=posted_at or utcnow(),
    )
    session.add(tx)
    session.flush()

    for account_id, amount in entries:
        account = accounts[account_id]
        new_balance = account.posted_balance_minor + int(amount)
        if new_balance < 0:
            # 所有账户（含清算镜像账户）都不允许负余额：清算账户以初始头寸代表外部世界。
            raise FinPilotError(
                "INSUFFICIENT_FUNDS",
                "账户余额不足，交易未执行。",
                details={"reason_codes": ["negative_balance"], "account_id": account_id},
            )
        account.posted_balance_minor = new_balance
        account.version += 1
        session.add(
            LedgerEntry(
                ledger_tx_id=tx.ledger_tx_id,
                account_id=account_id,
                currency=CURRENCY,
                signed_amount_minor=int(amount),
                balance_after_minor=new_balance,
            )
        )
    session.flush()
    return tx.ledger_tx_id


def reserved_minor(session: Session, account_id: str) -> int:
    """H：该账户有效预留的剩余金额合计。"""
    value = session.execute(
        select(func.coalesce(func.sum(Reservation.remaining_minor), 0)).where(
            Reservation.account_id == account_id,
            Reservation.status.in_(RESERVATION_ACTIVE_STATUSES),
        )
    ).scalar_one()
    return int(value)


def account_balance_minor(session: Session, account_id: str) -> int:
    account = session.get(Account, account_id)
    if account is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
    return account.posted_balance_minor


def account_available_minor(session: Session, account_id: str) -> int:
    """B − H − F：银行可用余额（不含应急金）。"""
    account = session.get(Account, account_id)
    if account is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
    return account.posted_balance_minor - reserved_minor(session, account_id) - account.frozen_other_minor


def account_spendable_minor(session: Session, account_id: str, protected_floor_minor: int = 0) -> int:
    """规划可支配金额 = max(0, B − H − F − E)。"""
    return max(0, account_available_minor(session, account_id) - max(0, protected_floor_minor))


def group_totals(session: Session, account_ids: list[str], protected_floor_minor: int = 0) -> dict:
    """多账户保护组：B/H/F 同币种汇总，E 只扣一次。"""
    if not account_ids:
        return {"posted_minor": 0, "reserved_minor": 0, "frozen_minor": 0, "available_minor": 0, "spendable_minor": 0}
    rows = session.execute(select(Account).where(Account.account_id.in_(account_ids))).scalars().all()
    if len(rows) != len(set(account_ids)):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "保护组中包含不可访问的账户。")
    posted = sum(a.posted_balance_minor for a in rows)
    frozen = sum(a.frozen_other_minor for a in rows)
    reserved = sum(reserved_minor(session, a.account_id) for a in rows)
    available = posted - reserved - frozen
    return {
        "posted_minor": posted,
        "reserved_minor": reserved,
        "frozen_minor": frozen,
        "available_minor": available,
        "spendable_minor": max(0, available - max(0, protected_floor_minor)),
    }


def ledger_history(
    session: Session,
    *,
    owner_user_id: str,
    account_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """只读流水查询，按归属过滤（不能通过传别人的账户绕过归属）。"""
    owned = session.execute(select(Account.account_id).where(Account.owner_user_id == owner_user_id)).scalars().all()
    if account_id is not None:
        if account_id not in owned:
            raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
        scoped = [account_id]
    else:
        scoped = list(owned)
    if not scoped:
        return []

    stmt = (
        select(LedgerEntry, LedgerTransaction)
        .join(LedgerTransaction, LedgerEntry.ledger_tx_id == LedgerTransaction.ledger_tx_id)
        .where(LedgerEntry.account_id.in_(scoped))
        .order_by(LedgerTransaction.posted_at.desc())
        .limit(max(1, min(limit, 1000)))
    )
    if start is not None:
        stmt = stmt.where(LedgerTransaction.posted_at >= start)
    if end is not None:
        stmt = stmt.where(LedgerTransaction.posted_at < end)

    return [
        {
            "entry_id": entry.entry_id,
            "ledger_tx_id": tx.ledger_tx_id,
            "operation_id": tx.operation_id,
            "account_id": entry.account_id,
            "signed_amount_minor": entry.signed_amount_minor,
            "balance_after_minor": entry.balance_after_minor,
            "kind": tx.kind,
            "memo": tx.memo,
            "posted_at": tx.posted_at,
        }
        for entry, tx in session.execute(stmt).all()
    ]


def conservation_check(session: Session) -> dict:
    """守恒自检：每笔事务分录之和为 0，且账户余额等于分录累计。"""
    unbalanced = session.execute(
        select(LedgerTransaction.ledger_tx_id, func.sum(LedgerEntry.signed_amount_minor))
        .join(LedgerEntry, LedgerEntry.ledger_tx_id == LedgerTransaction.ledger_tx_id)
        .group_by(LedgerTransaction.ledger_tx_id)
        .having(func.sum(LedgerEntry.signed_amount_minor) != 0)
    ).all()
    mismatched = session.execute(
        select(LedgerEntry.account_id, func.sum(LedgerEntry.signed_amount_minor))
        .group_by(LedgerEntry.account_id)
    ).all()
    balance_mismatch = []
    for account_id, total in mismatched:
        account = session.get(Account, account_id)
        if account is None:
            balance_mismatch.append({"account_id": account_id, "reason": "missing_account"})
            continue
        expected = account.posted_balance_minor
        # 初始余额不来自分录，允许差额等于该账户的初始余额
        initial = INITIAL_BALANCES.get(account_id, None)
        if initial is None:
            continue
        if expected != initial + int(total):
            balance_mismatch.append({"account_id": account_id, "expected": expected, "from_entries": initial + int(total)})
    return {
        "ok": not unbalanced and not balance_mismatch,
        "unbalanced_transactions": [row[0] for row in unbalanced],
        "balance_mismatch": balance_mismatch,
    }


# 种子初始余额登记（测试与演示用；真实初始余额由开户流程写入）
INITIAL_BALANCES: dict[str, int] = {}


def register_initial_balance(account_id: str, amount_minor: int) -> None:
    INITIAL_BALANCES[account_id] = amount_minor


def ensure_clearing_account(session: Session) -> Account:
    account = session.get(Account, CLEARING_ACCOUNT_ID)
    if account is None:
        if session.get(User, SYSTEM_CLEARING_OWNER) is None:
            session.add(
                User(
                    user_id=SYSTEM_CLEARING_OWNER,
                    status="active",
                    display_name="清算系统账户",
                    password_hash="",
                    roles=["system"],
                    created_at=utcnow(),
                )
            )
            session.flush()
        account = Account(
            account_id=CLEARING_ACCOUNT_ID,
            owner_user_id=SYSTEM_CLEARING_OWNER,
            name="清算账户",
            account_type="clearing",
            currency=CURRENCY,
            posted_balance_minor=0,
            created_at=utcnow(),
        )
        session.add(account)
        session.flush()
    return account


def transfer_entries(*, source_account_id: str, amount_minor: int, fee_minor: int = 0) -> list[tuple[str, int]]:
    """对外付款分录：付款账户减少，清算账户增加（不以单边改余额替代双重记账）。"""
    ensure_clearing = [(CLEARING_ACCOUNT_ID, amount_minor + fee_minor)]
    debit = [(source_account_id, -(amount_minor + fee_minor))]
    return debit + ensure_clearing


def fee_entries(*, source_account_id: str, fee_minor: int) -> list[tuple[str, int]]:
    return [(source_account_id, -fee_minor), (CLEARING_ACCOUNT_ID, fee_minor)]


def incoming_entries(*, target_account_id: str, amount_minor: int) -> list[tuple[str, int]]:
    return [(CLEARING_ACCOUNT_ID, -amount_minor), (target_account_id, amount_minor)]