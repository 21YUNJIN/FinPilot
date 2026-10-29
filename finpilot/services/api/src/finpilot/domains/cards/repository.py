"""卡片领域数据访问层（只做查询、行锁与归属定位，不含业务规则）。

设计要点：
- 归属检查集中在此层：``require_owned_card`` 对客户只返回自己的卡，
  对运营角色放行；不存在与无权访问统一抛 ``OBJECT_NOT_ACCESSIBLE``，不泄露他人对象是否存在。
- 查询函数不修改状态，状态迁移与审计字段一律由 ``service.py`` 写。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...kernel.identity import AuthContext
from ...models import Account, Card, CardApplication, CardControl


# ---------------------------------------------------------------------------
# 归属与实体定位
# ---------------------------------------------------------------------------


def get_card(session: Session, card_id: str | None) -> Card | None:
    if not card_id:
        return None
    return session.get(Card, card_id)


def lock_card(session: Session, card_id: str) -> Card:
    card = session.execute(select(Card).where(Card.card_id == card_id).with_for_update()).scalar_one_or_none()
    if card is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该卡片。")
    return card


def can_access_card(ctx: AuthContext, card: Card) -> bool:
    return ctx.is_operator or card.owner_user_id == ctx.user_id


def require_owned_card(session: Session, ctx: AuthContext, card_id: str | None) -> Card:
    """按归属取卡：运营角色例外，客户只能访问自己的卡。"""
    card = get_card(session, card_id)
    if card is None or not can_access_card(ctx, card):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该卡片。")
    return card


def require_owned_account(session: Session, ctx: AuthContext, account_id: str | None) -> Account:
    account = session.get(Account, account_id) if account_id else None
    if account is None or not (ctx.is_operator or account.owner_user_id == ctx.user_id):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该账户。")
    return account


def get_application(session: Session, application_id: str | None) -> CardApplication | None:
    if not application_id:
        return None
    return session.get(CardApplication, application_id)


def require_application(session: Session, ctx: AuthContext, application_id: str) -> CardApplication:
    application = get_application(session, application_id)
    if application is None or not (ctx.is_operator or application.owner_user_id == ctx.user_id):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该申请。")
    return application


def application_by_operation(session: Session, operation_id: str | None) -> CardApplication | None:
    """幂等主键：同一 operation_id 只允许对应一条申请记录。"""
    if not operation_id:
        return None
    return session.execute(
        select(CardApplication)
        .where(CardApplication.operation_id == operation_id)
        .order_by(CardApplication.created_at)
    ).scalars().first()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def cards_for_owner(session: Session, owner_user_id: str) -> list[Card]:
    return list(
        session.execute(
            select(Card).where(Card.owner_user_id == owner_user_id).order_by(Card.created_at, Card.card_id)
        ).scalars()
    )


def cards_for_operator(session: Session) -> list[Card]:
    return list(session.execute(select(Card).order_by(Card.created_at, Card.card_id)).scalars())


def applications_for_owner(
    session: Session,
    owner_user_id: str,
    *,
    status: str | None = None,
    kind: str | None = None,
) -> list[CardApplication]:
    stmt = select(CardApplication).where(CardApplication.owner_user_id == owner_user_id)
    if status:
        stmt = stmt.where(CardApplication.application_status == status)
    if kind:
        stmt = stmt.where(CardApplication.kind == kind)
    return list(session.execute(stmt.order_by(CardApplication.created_at, CardApplication.application_id)).scalars())


def applications_for_operator(
    session: Session,
    *,
    status: str | None = None,
    kind: str | None = None,
) -> list[CardApplication]:
    stmt = select(CardApplication)
    if status:
        stmt = stmt.where(CardApplication.application_status == status)
    if kind:
        stmt = stmt.where(CardApplication.kind == kind)
    return list(session.execute(stmt.order_by(CardApplication.created_at, CardApplication.application_id)).scalars())


def applications_for_card(session: Session, card_id: str, *, kind: str | None = None) -> list[CardApplication]:
    stmt = select(CardApplication).where(CardApplication.card_id == card_id)
    if kind:
        stmt = stmt.where(CardApplication.kind == kind)
    return list(session.execute(stmt.order_by(CardApplication.created_at)).scalars())


def rejected_limit_change(
    session: Session,
    card_id: str,
    requested_limit_minor: int | None = None,
    *,
    exclude_application_id: str | None = None,
) -> CardApplication | None:
    """最近的被拒提额申请；给定参数时只匹配"未变更参数"的重复申请。"""
    stmt = select(CardApplication).where(
        CardApplication.card_id == card_id,
        CardApplication.kind == "limit_change",
        CardApplication.application_status == "rejected",
    )
    if requested_limit_minor is not None:
        stmt = stmt.where(CardApplication.requested_limit_minor == int(requested_limit_minor))
    if exclude_application_id:
        stmt = stmt.where(CardApplication.application_id != exclude_application_id)
    return session.execute(stmt.order_by(CardApplication.created_at.desc())).scalars().first()


def open_replacement(session: Session, card_id: str) -> CardApplication | None:
    """该卡是否已有在途补卡申请（submitted / under_review）。"""
    return session.execute(
        select(CardApplication)
        .where(
            CardApplication.card_id == card_id,
            CardApplication.kind == "replacement",
            CardApplication.application_status.in_(("submitted", "under_review")),
        )
        .order_by(CardApplication.created_at.desc())
    ).scalars().first()


def controls_for_card(session: Session, card_id: str, *, active_only: bool = False) -> list[CardControl]:
    """卡控列表（含已释放记录，供审计与叠加视图使用）。"""
    stmt = select(CardControl).where(CardControl.card_id == card_id)
    if active_only:
        stmt = stmt.where(CardControl.active.is_(True))
    return list(session.execute(stmt.order_by(CardControl.created_at, CardControl.control_id)).scalars())


def get_control(session: Session, control_id: str | None) -> CardControl | None:
    if not control_id:
        return None
    return session.get(CardControl, control_id)


def find_active_control(
    session: Session,
    card_id: str,
    *,
    control_type: str,
    channel: str | None = None,
) -> CardControl | None:
    stmt = select(CardControl).where(
        CardControl.card_id == card_id,
        CardControl.control_type == control_type,
        CardControl.active.is_(True),
    )
    if channel is not None:
        stmt = stmt.where(CardControl.channel == channel)
    return session.execute(stmt.order_by(CardControl.created_at)).scalars().first()