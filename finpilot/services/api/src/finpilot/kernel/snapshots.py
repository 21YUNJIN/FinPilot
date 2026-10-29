"""资金与保护组快照（V5 4.2、8.1、PLAN-04）。

快照携带版本与观测时间，供 C/D 检查；未到账退款、待收 AA 与待赎回款
一律单列，绝不作为可用资金。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..models import (
    AARequest,
    AAShare,
    Account,
    ProtectionPolicy,
    Refund,
)
from ..money import CURRENCY
from ..timeutil import format_iso, utcnow
from .identity import AuthContext
from .ledger import account_available_minor, reserved_minor


def _owned_account_ids(session: Session, ctx: AuthContext, account_ids: list[str] | None) -> list[str]:
    owned = (
        session.execute(select(Account.account_id).where(Account.owner_user_id == ctx.user_id)).scalars().all()
    )
    if account_ids:
        if not set(account_ids).issubset(set(owned)):
            # 不泄露他人的对象是否存在
            raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
        return sorted(set(account_ids))
    return sorted(owned)


def protection_policy(session: Session, user_id: str) -> ProtectionPolicy | None:
    return session.execute(
        select(ProtectionPolicy)
        .where(ProtectionPolicy.owner_user_id == user_id)
        .order_by(ProtectionPolicy.version.desc())
    ).scalars().first()


def get_snapshot(
    session: Session,
    ctx: AuthContext,
    account_ids: list[str] | None = None,
    task_id: str | None = None,
) -> dict:
    """账户／保护组、余额、预留、冻结、政策版本与观测时间。"""
    ctx.require_customer()
    scoped = _owned_account_ids(session, ctx, account_ids)
    policy = protection_policy(session, ctx.user_id)
    protected_floor = policy.protected_floor_minor if policy else 0
    scope = list(policy.account_scope) if policy else []
    policy_version = f"protection-v{policy.version}" if policy else "protection-none"

    accounts: list[dict] = []
    for account_id in scoped:
        account = session.get(Account, account_id)
        if account is None:
            continue
        reserved = reserved_minor(session, account_id)
        in_group = (not scope) or (account_id in scope)
        floor_share = protected_floor if (in_group and scope and len(scope) == 1) else 0
        if in_group and scope and len(scope) > 1:
            floor_share = None  # 多账户组按组汇总扣一次 E
        accounts.append(
            {
                "account_id": account.account_id,
                "name": account.name,
                "owner_user_id": account.owner_user_id,
                "currency": account.currency,
                "posted_balance_minor": account.posted_balance_minor,
                "reserved_minor": reserved,
                "frozen_other_minor": account.frozen_other_minor,
                "available_minor": account.posted_balance_minor - reserved - account.frozen_other_minor,
                "spendable_minor": max(
                    0,
                    account.posted_balance_minor - reserved - account.frozen_other_minor - (floor_share or 0),
                ),
                "protected_floor_minor": floor_share,
                "in_protection_group": in_group,
                "status": account.status,
                "version": account.version,
            }
        )

    group_accounts = [a["account_id"] for a in accounts if a["in_protection_group"]]
    group_posted = sum(a["posted_balance_minor"] for a in accounts if a["account_id"] in group_accounts)
    group_reserved = sum(a["reserved_minor"] for a in accounts if a["account_id"] in group_accounts)
    group_frozen = sum(a["frozen_other_minor"] for a in accounts if a["account_id"] in group_accounts)
    group_available = group_posted - group_reserved - group_frozen

    pending_receivables = session.execute(
        select(func.coalesce(func.sum(AAShare.share_minor), 0))
        .join(AARequest, AARequest.aa_id == AAShare.aa_id)
        .where(AARequest.owner_user_id == ctx.user_id, AAShare.status == "pending", AAShare.is_initiator.is_(False))
    ).scalar_one()

    unconfirmed_refunds = session.execute(
        select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
            Refund.owner_user_id == ctx.user_id, Refund.status.in_(("pending", "processing"))
        )
    ).scalar_one()

    return {
        "task_id": task_id,
        "accounts": accounts,
        "protected_floor_minor": protected_floor,
        "protection_scope": scope,
        "policy_version": policy_version,
        "currency": CURRENCY,
        "group": {
            "account_ids": group_accounts,
            "posted_minor": group_posted,
            "reserved_minor": group_reserved,
            "frozen_minor": group_frozen,
            "available_minor": group_available,
            "spendable_minor": max(0, group_available - protected_floor),
        },
        # 明确不作为可用资金
        "pending_receivables_minor": int(pending_receivables),
        "unconfirmed_refunds_minor": int(unconfirmed_refunds),
        "unconfirmed_receivables_are_spendable": False,
        "observed_at": format_iso(utcnow()),
    }


def get_task_snapshot(session: Session, ctx: AuthContext, task_id: str) -> dict:
    snapshot = get_snapshot(session, ctx, None, task_id=task_id)
    return snapshot


def account_view(session: Session, ctx: AuthContext, account_id: str) -> dict:
    snapshot = get_snapshot(session, ctx, [account_id])
    if not snapshot["accounts"]:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
    return snapshot["accounts"][0]


def available_for_account(session: Session, account_id: str) -> int:
    return account_available_minor(session, account_id)


def snapshot_for_amount_checks(session: Session, ctx: AuthContext) -> tuple[dict, int]:
    """返回 (快照, 保护组可支配金额)。"""
    snapshot = get_snapshot(session, ctx)
    return snapshot, snapshot["group"]["spendable_minor"]


def observed_at_now() -> datetime:
    return utcnow()