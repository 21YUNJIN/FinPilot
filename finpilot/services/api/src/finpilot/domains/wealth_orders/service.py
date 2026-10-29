"""理财申购、赎回与持仓（V5 11.2、S3-03、S3-04、T10/T11）。

状态分层（必须可区分）：
    受理 accepted → 资金效果（冻结/扣划）→ 确认份额 confirmed；赎回为申请 → 份额扣减 → 资金到账。
- 受理不等于确认份额：估算份额与正式份额分开保存。
- 净值未知时只给估算区间，不填造精确持仓（由 wealth_rules.terms 负责）。
- 赎回校验可用份额（confirmed − frozen），到账前不得增加余额。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...kernel.identity import AuthContext
from ...kernel.ledger import CLEARING_ACCOUNT_ID, ensure_clearing_account, post_transaction
from ...models import Holding, Operation, ProductNav, WealthOrder, WealthProduct
from ...money import format_minor, format_shares_e8
from ...timeutil import business_day, utcnow

TERMS_VERSION_FALLBACK = "2026.10"


def _product(session: Session, product_id: str | None) -> WealthProduct:
    if not product_id:
        raise FinPilotError("PARAM_INVALID", "缺少产品编号。")
    product = session.get(WealthProduct, product_id)
    if product is None or not product.is_active:
        raise FinPilotError("PARAM_INVALID", "该理财产品不存在或已下架。")
    return product


def _latest_nav(session: Session, product_id: str) -> ProductNav | None:
    return session.execute(
        select(ProductNav).where(ProductNav.product_id == product_id).order_by(ProductNav.as_of.desc())
    ).scalars().first()


def _holding(session: Session, ctx: AuthContext, account_id: str, product_id: str, *, create: bool) -> Holding | None:
    holding = session.execute(
        select(Holding).where(
            Holding.owner_user_id == ctx.user_id,
            Holding.account_id == account_id,
            Holding.product_id == product_id,
        )
    ).scalars().first()
    if holding is None and create:
        holding = Holding(
            owner_user_id=ctx.user_id,
            account_id=account_id,
            product_id=product_id,
            confirmed_shares_e8=0,
            frozen_shares_e8=0,
        )
        session.add(holding)
        session.flush()
    return holding


def _existing_order(session: Session, operation_id: str) -> WealthOrder | None:
    return session.execute(select(WealthOrder).where(WealthOrder.operation_id == operation_id)).scalars().first()


def purchase(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """申购：受理 → 资金扣划 → 确认份额（三段状态分别记录）。"""
    existing = _existing_order(session, operation.operation_id)
    if existing is not None:
        return _order_result(session, existing, note="幂等重放")

    args = operation.typed_args or {}
    product = _product(session, args.get("product_id"))
    account_id = args.get("source_account_id") or operation.source_account_id
    amount = int(args.get("amount_minor") or operation.amount_minor or 0)
    fee = int(args.get("fee_minor") or 0)
    if amount <= 0:
        raise FinPilotError("PARAM_INVALID", "申购金额必须大于 0。")
    if amount < product.min_minor:
        raise FinPilotError(
            "PARAM_INVALID",
            "低于该产品起购金额。",
            details={"reason_codes": ["below_min_amount"], "min_minor": product.min_minor},
        )

    # 适当性：无有效测评或风险不匹配必须阻断（纯确定性规则，不签发权限）
    try:
        from ..wealth_rules.suitability import validate_domain

        validation = validate_domain(session, ctx, operation, snapshot=None)
        if isinstance(validation, dict) and validation.get("valid") is False:
            blocking = set(validation.get("blocking_reason_codes") or validation.get("reason_codes") or [])
            # 净值未知不作为受理阻断：可以受理订单，但不得编造正式份额（V5 11.2）
            blocking -= {"NAV_UNAVAILABLE", "INSUFFICIENT_HISTORY"}
            if blocking:
                code = "RISK_MISMATCH" if "RISK_MISMATCH" in blocking else (
                    "ASSESSMENT_REQUIRED" if "ASSESSMENT_REQUIRED" in blocking else "POLICY_DENIED"
                )
                raise FinPilotError(
                    code,
                    "该产品与当前风险测评或资金约束不匹配。",
                    details={"reason_codes": sorted(blocking)},
                )
    except ImportError:  # pragma: no cover - 规则模块尚未装配
        pass

    now = utcnow()
    nav = _latest_nav(session, product.product_id)
    estimated_shares_e8 = 0
    estimated_range: dict | None = None
    if nav is not None and nav.nav_e6 > 0:
        net = max(0, amount - fee)
        # 份额（1e-8 定点）= 金额(分)/100 ÷ 净值(nav_e6/1e6) × 1e8 = net_minor × 1e12 ÷ nav_e6
        estimated_shares_e8 = (net * 10**12) // nav.nav_e6
        estimated_range = {
            "low_shares_e8": int(estimated_shares_e8 * 0.97),
            "high_shares_e8": int(estimated_shares_e8 * 1.03),
            "basis_nav_e6": nav.nav_e6,
            "as_of": nav.as_of.isoformat(),
        }

    # 资金效果：从付款账户划出（有任务预留时由执行器的捕获路径处理）
    ensure_clearing_account(session)
    post_transaction(
        session,
        kind="wealth_purchase",
        entries=[(account_id, -(amount + fee)), (CLEARING_ACCOUNT_ID, amount + fee)],
        operation_id=operation.operation_id,
        task_id=operation.task_id,
        memo=f"申购 {product.name}",
    )

    order = WealthOrder(
        operation_id=operation.operation_id,
        owner_user_id=ctx.user_id,
        account_id=account_id,
        product_id=product.product_id,
        side="purchase",
        amount_minor=amount,
        fee_minor=fee,
        estimated_shares_e8=estimated_shares_e8,
        confirmed_shares_e8=0,
        status="accepted",
        product_terms_version=product.terms_version or TERMS_VERSION_FALLBACK,
        accepted_at=now,
        expected_settlement_date=business_day(now) + timedelta(days=max(0, product.settlement_days)),
        created_at=now,
    )
    session.add(order)
    session.flush()

    # 受理之后才确认份额；净值未知时保持 awaiting_confirmation 并明确 NA
    order.status = "awaiting_confirmation"
    if estimated_shares_e8 > 0:
        holding = _holding(session, ctx, account_id, product.product_id, create=True)
        assert holding is not None
        holding.confirmed_shares_e8 += estimated_shares_e8
        holding.valuation_at = now
        holding.version += 1
        order.confirmed_shares_e8 = estimated_shares_e8
        order.status = "confirmed"
        order.confirmed_at = now
    session.flush()

    return _order_result(
        session,
        order,
        note="申购已受理；正式份额以产品确认为准",
        extra={"estimated_range": estimated_range, "nav_unavailable": estimated_shares_e8 == 0},
    )


def redeem(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """赎回：校验可用份额 → 冻结申请份额 → 到账时才增加余额。"""
    existing = _existing_order(session, operation.operation_id)
    if existing is not None:
        return _order_result(session, existing, note="幂等重放")

    args = operation.typed_args or {}
    product = _product(session, args.get("product_id"))
    account_id = args.get("source_account_id") or operation.source_account_id
    holding = _holding(session, ctx, account_id, product.product_id, create=False)
    if holding is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "没有该产品的持仓。")

    available = holding.confirmed_shares_e8 - holding.frozen_shares_e8
    requested_shares = int(args.get("shares_e8") or 0)
    amount = int(args.get("amount_minor") or operation.amount_minor or 0)
    nav = _latest_nav(session, product.product_id)
    if requested_shares <= 0:
        if amount <= 0 or nav is None or nav.nav_e6 <= 0:
            raise FinPilotError("PARAM_INVALID", "请给出赎回份额，或在净值已知时给出金额。")
        requested_shares = (amount * 10**12) // nav.nav_e6
    if requested_shares > available:
        raise FinPilotError(
            "PARAM_INVALID",
            "赎回份额超过可用份额。",
            details={"reason_codes": ["exceeds_available_shares"], "available_shares_e8": available},
        )

    now = utcnow()
    holding.frozen_shares_e8 += requested_shares
    holding.version += 1
    order = WealthOrder(
        operation_id=operation.operation_id,
        owner_user_id=ctx.user_id,
        account_id=account_id,
        product_id=product.product_id,
        side="redeem",
        amount_minor=amount or (requested_shares * (nav.nav_e6 if nav else 0)) // 10**14,
        fee_minor=int(args.get("fee_minor") or 0),
        estimated_shares_e8=requested_shares,
        confirmed_shares_e8=0,
        status="redeeming",
        product_terms_version=product.terms_version or TERMS_VERSION_FALLBACK,
        accepted_at=now,
        expected_settlement_date=business_day(now) + timedelta(days=max(0, product.settlement_days)),
        created_at=now,
    )
    session.add(order)
    session.flush()

    # 份额扣减与资金到账：按条款在结算日完成；离线/演示模式在同一次受理后立即结算，
    # 但仍保留“申请受理 / 份额扣减 / 资金到账”三个时间与状态，绝不混为一谈。
    holding.confirmed_shares_e8 -= requested_shares
    holding.frozen_shares_e8 -= requested_shares
    holding.version += 1
    payout = max(0, order.amount_minor - order.fee_minor)
    if payout > 0:
        ensure_clearing_account(session)
        post_transaction(
            session,
            kind="wealth_redeem_settlement",
            entries=[(CLEARING_ACCOUNT_ID, -payout), (account_id, payout)],
            operation_id=f"{operation.operation_id}:settle",
            task_id=operation.task_id,
            memo=f"赎回 {product.name} 到账",
        )
    order.status = "settled"
    order.confirmed_shares_e8 = requested_shares
    order.confirmed_at = now
    order.settled_at = now
    session.flush()

    return _order_result(
        session,
        order,
        note="申请已受理，份额已扣减，资金按条款到账（受理≠到账）",
        extra={"payout_minor": payout},
    )


def status_view(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    args = operation.typed_args or {}
    order = None
    if args.get("order_id"):
        order = session.get(WealthOrder, str(args["order_id"]))
    elif args.get("operation_id"):
        order = _existing_order(session, str(args["operation_id"]))
    if order is None or order.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该订单。")
    return _order_result(session, order, note="只读查询")


def _order_result(session: Session, order: WealthOrder, *, note: str, extra: dict | None = None) -> dict:
    product = session.get(WealthProduct, order.product_id)
    holding = session.execute(
        select(Holding).where(
            Holding.owner_user_id == order.owner_user_id,
            Holding.account_id == order.account_id,
            Holding.product_id == order.product_id,
        )
    ).scalars().first()
    data = {
        "order_id": order.order_id,
        "operation_id": order.operation_id,
        "product_id": order.product_id,
        "product_name": product.name if product else "",
        "side": order.side,
        "status": order.status,
        "amount_minor": order.amount_minor,
        "amount": format_minor(order.amount_minor),
        "fee_minor": order.fee_minor,
        "estimated_shares": format_shares_e8(order.estimated_shares_e8),
        "confirmed_shares": format_shares_e8(order.confirmed_shares_e8) if order.confirmed_shares_e8 else None,
        "product_terms_version": order.product_terms_version,
        "accepted_at": order.accepted_at.isoformat() if order.accepted_at else None,
        "confirmed_at": order.confirmed_at.isoformat() if order.confirmed_at else None,
        "settled_at": order.settled_at.isoformat() if order.settled_at else None,
        "expected_settlement_date": order.expected_settlement_date.isoformat() if order.expected_settlement_date else None,
        "holding_available_shares": (
            format_shares_e8(holding.confirmed_shares_e8 - holding.frozen_shares_e8) if holding else "0"
        ),
    }
    if extra:
        data.update(extra)
    warnings = []
    if order.status == "awaiting_confirmation":
        warnings.append("净值未知：只给估算区间，正式份额待产品确认，不得当作已持有")
    return {
        "type": "wealth_order",
        "domain": "wealth_orders",
        "result_type": f"wealth_{order.side}",
        "data": data,
        "source": "local_ledger",
        "version": order.product_terms_version,
        "warnings": warnings,
        "note": note,
        "observed_at": utcnow().isoformat(),
    }


READ_HANDLERS = {"wealth.status": status_view}
TOOL_HANDLERS = {"wealth.purchase": purchase, "wealth.redeem": redeem}