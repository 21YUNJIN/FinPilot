"""领域只读接口与客户端展示数据（A 的界面数据全部来自这里）。

原则：展示数据必须是结构化、可追溯、带版本与观测时间的；自由文本一律标为 untrusted。
领域模块尚未装配时，明确返回能力错误，不伪造结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..envelope import ok
from ..errors import FinPilotError
from ..models import AAShare, AARequest, Card, CardApplication, CardControl, Holding, Mandate, Plan, ProductNav, ScheduledJob, Subscription, Task, WealthOrder, WealthProduct
from ..money import format_minor
from ..timeutil import business_day, format_iso, utcnow
from ..kernel.identity import AuthContext
from ..kernel.snapshots import get_snapshot
from .deps import current_auth, get_db

router = APIRouter(tags=["domain"])


def _missing_domain(name: str) -> FinPilotError:
    return FinPilotError(
        "DEPENDENCY_FAILED",
        f"领域模块 {name} 尚未装配，未返回任何伪造结果。",
        details={"reason_codes": ["domain_not_ready"], "domain": name},
    )


@router.get("/v1/accounts")
def list_accounts(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    snapshot = get_snapshot(session, ctx)
    from ..kernel.limits import daily_usage

    return ok(
        {
            "accounts": [
                {
                    **account,
                    "posted_balance": format_minor(account["posted_balance_minor"]),
                    "available": format_minor(account["available_minor"]),
                    "spendable": format_minor(account["spendable_minor"]),
                }
                for account in snapshot["accounts"]
            ],
            "group": snapshot["group"],
            "protected_floor_minor": snapshot["protected_floor_minor"],
            "pending_receivables_minor": snapshot["pending_receivables_minor"],
            "unconfirmed_refunds_minor": snapshot["unconfirmed_refunds_minor"],
            "daily_usage": daily_usage(session, ctx),
            "observed_at": snapshot["observed_at"],
        }
    )


@router.get("/v1/payees")
def list_payees(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..models import Payee

    rows = session.execute(select(Payee).where(Payee.owner_user_id == ctx.user_id)).scalars().all()
    return ok(
        {
            "items": [
                {
                    "payee_id": row.payee_id,
                    "display_name": row.display_name,
                    "masked_account": row.destination_account_ref,
                    "phone_tail": row.phone_ref[-4:] if row.phone_ref else "",
                    "bank_name": row.bank_name,
                    "aliases": row.aliases,
                }
                for row in rows
            ]
        }
    )


@router.get("/v1/bills/report")
def bill_report(
    kind: str = Query(default="month", pattern="^(month|year|custom)$"),
    anchor: str | None = Query(default=None),
    start: str | None = None,
    end: str | None = None,
    basis: str = Query(default="both", pattern="^(consumption|cashflow|both)$"),
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    try:
        from ..domains.billing.service import bill_report as _report
    except Exception as exc:  # pragma: no cover
        raise _missing_domain("billing") from exc
    period: dict = {"kind": kind}
    if kind == "custom":
        if not start or not end:
            raise FinPilotError("PARAM_INVALID", "自选账期必须提供 start 与 end。")
        period.update({"start": start, "end": end})
    else:
        period["anchor"] = anchor or utcnow().astimezone().strftime("%Y-%m") if kind == "month" else (anchor or str(business_day().year))
    return ok(_report(session, ctx, period=period, basis=basis))


class CorrectionBody(BaseModel):
    bt_id: str
    to_category: str = Field(pattern="^(餐饮|交通|购物|居住|娱乐|医疗|教育|金融|其他)$")
    reason: str = "用户纠正"


@router.post("/v1/bills/corrections")
def correct_category(body: CorrectionBody, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    try:
        from ..domains.billing.service import correct_category as _correct
    except Exception as exc:  # pragma: no cover
        raise _missing_domain("billing") from exc
    return ok(_correct(session, ctx, bt_id=body.bt_id, to_category=body.to_category, reason=body.reason))


@router.get("/v1/cards")
def list_cards(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    cards = session.execute(select(Card).where(Card.owner_user_id == ctx.user_id)).scalars().all()
    controls = session.execute(
        select(CardControl).where(CardControl.card_id.in_([c.card_id for c in cards]), CardControl.active.is_(True))
    ).scalars().all() if cards else []
    applications = session.execute(
        select(CardApplication).where(CardApplication.owner_user_id == ctx.user_id).order_by(CardApplication.created_at.desc())
    ).scalars().all()
    from ..security.policy import card_control_block_reason

    return ok(
        {
            "items": [
                {
                    "card_id": card.card_id,
                    "masked_pan": card.masked_pan,
                    "lifecycle_status": card.lifecycle_status,
                    "credit_limit_minor": card.credit_limit_minor,
                    "credit_limit": format_minor(card.credit_limit_minor),
                    "account_id": card.account_id,
                    "replaces_card_id": card.replaces_card_id,
                    "replaced_by_card_id": card.replaced_by_card_id,
                    "version": card.version,
                    "controls": [
                        {
                            "control_id": control.control_id,
                            "control_type": control.control_type,
                            "channel": control.channel,
                            "limit_minor": control.limit_minor,
                            "active": control.active,
                            "created_by_role": control.created_by_role,
                        }
                        for control in controls
                        if control.card_id == card.card_id
                    ],
                    "blocked_reasons": card_control_block_reason(session, card),
                }
                for card in cards
            ],
            "applications": [
                {
                    "application_id": app.application_id,
                    "kind": app.kind,
                    "application_status": app.application_status,
                    "requested_limit_minor": app.requested_limit_minor,
                    "approved_limit_minor": app.approved_limit_minor,
                    "decision_reason": app.decision_reason,
                    "created_at": format_iso(app.created_at),
                }
                for app in applications
            ],
        }
    )


@router.get("/v1/subscriptions")
def list_subscriptions(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    return ok({"items": [], "requires_confirmation": True,
               "policy_note": "订阅查询为黄色操作。确认后才查询订阅合同、金额、续费日期和代扣详情。"})


class SubscriptionQueryConsent(BaseModel):
    confirmed: Literal[True]
    operation_id: str = Field(min_length=8, max_length=128)


@router.post("/v1/subscriptions/query", openapi_extra={"x-agent-exposed": False})
def confirmed_subscription_query(body: SubscriptionQueryConsent, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..kernel.submission import OperationDraft, prepare_operation, submit_operation, transition
    from ..kernel.grants import create_exact_grant
    from ..models import Operation
    from ..security.scope import build_scope
    from ..domains.subscriptions.service import list_subscriptions as query

    existing = session.get(Operation, body.operation_id)
    if existing is not None and existing.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该操作。")
    operation = prepare_operation(session, ctx, OperationDraft(tool="subscription.list",
        typed_args={"include_inferred": True}, operation_id=body.operation_id))
    if operation.status == "succeeded":
        return ok(operation.domain_result, operation_id=operation.operation_id)
    scope = build_scope(operation.tool, operation.typed_args, task_id=operation.task_id,
        plan_id=operation.plan_id, plan_revision=operation.plan_revision,
        parameters_hash=operation.parameters_hash, operation_id=operation.operation_id,
        required_level=operation.required_level)
    grant = create_exact_grant(session, ctx, plan_id=operation.plan_id,
        plan_revision=operation.plan_revision, scope=scope, required_level=operation.required_level)
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    data = {"items": query(session, ctx), "requires_confirmation": False,
            "policy_level": operation.required_level, "operation_id": operation.operation_id}
    operation.domain_result = data
    transition(session, operation, "succeeded", reason="confirmed_subscription_query")
    return ok(data, operation_id=operation.operation_id)


@router.get("/v1/wealth/products")
def list_products(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    rows = session.execute(select(WealthProduct).where(WealthProduct.is_active.is_(True))).scalars().all()
    latest_nav: dict[str, float] = {}
    for row in session.execute(select(ProductNav).order_by(ProductNav.as_of)).scalars():
        latest_nav[row.product_id] = row.nav_e6
    return ok(
        {
            "items": [
                {
                    "product_id": p.product_id,
                    "name": p.name,
                    "issuer": p.issuer,
                    "product_type": p.product_type,
                    "issuer_risk_level": p.issuer_risk_level,
                    "min_minor": p.min_minor,
                    "fee_bps": p.fee_bps,
                    "fee_fixed_minor": p.fee_fixed_minor,
                    "term_days": p.term_days,
                    "liquidity_terms": p.liquidity_terms,
                    "cancel_terms": p.cancel_terms,
                    "cooling_off_days": p.cooling_off_days,
                    "settlement_days": p.settlement_days,
                    "terms_version": p.terms_version,
                    "latest_nav": latest_nav.get(p.product_id),
                }
                for p in rows
            ],
            "data_version": "2026.10",
        }
    )


@router.get("/v1/wealth/comparison")
def wealth_comparison(
    budget_minor: int = Query(default=100_000, ge=0),
    horizon_days: int = Query(default=365, ge=1),
    preference: str = Query(default="risk", pattern="^(risk|yield|fee|liquidity)$"),
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    try:
        from ..domains.wealth_rules.compare import compare_products
    except Exception as exc:  # pragma: no cover
        raise _missing_domain("wealth_rules") from exc
    return ok({"items": compare_products(session, ctx, budget_minor=budget_minor, horizon_days=horizon_days, preferences=[preference])})


@router.get("/v1/wealth/assessment")
def wealth_assessment(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..domains.wealth_rules.assessment import current_profile
    profile = current_profile(session, ctx)
    if not profile["usable"]:
        return ok(None)
    return ok({"risk_profile_id": profile["profile_id"], "level": profile["risk_level"],
               "valid_until": profile["expires_at"], "assessed_at": profile["assessed_at"],
               "questionnaire_version": profile["questionnaire_version"]})


@router.get("/v1/wealth/orders")
def list_wealth_orders(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    rows = session.execute(
        select(WealthOrder).where(WealthOrder.owner_user_id == ctx.user_id).order_by(WealthOrder.created_at.desc())
    ).scalars().all()
    holdings = session.execute(select(Holding).where(Holding.owner_user_id == ctx.user_id)).scalars().all()
    return ok(
        {
            "orders": [
                {
                    "order_id": row.order_id,
                    "operation_id": row.operation_id,
                    "product_id": row.product_id,
                    "side": row.side,
                    "amount_minor": row.amount_minor,
                    "status": row.status,
                    "estimated_shares_e8": row.estimated_shares_e8,
                    "confirmed_shares_e8": row.confirmed_shares_e8,
                    "expected_settlement_date": row.expected_settlement_date.isoformat() if row.expected_settlement_date else None,
                }
                for row in rows
            ],
            "holdings": [
                {
                    "holding_id": row.holding_id,
                    "product_id": row.product_id,
                    "confirmed_shares_e8": row.confirmed_shares_e8,
                    "frozen_shares_e8": row.frozen_shares_e8,
                    "available_shares_e8": row.confirmed_shares_e8 - row.frozen_shares_e8,
                }
                for row in holdings
            ],
            "note": "受理、资金冻结、确认份额与到账是不同状态；未知净值不编造份额",
        }
    )


@router.get("/v1/aa")
def list_aa(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    rows = session.execute(select(AARequest).where(AARequest.owner_user_id == ctx.user_id)).scalars().all()
    shares = session.execute(
        select(AAShare).where(AAShare.aa_id.in_([r.aa_id for r in rows]))
    ).scalars().all() if rows else []
    return ok(
        {
            "items": [
                {
                    "aa_id": row.aa_id,
                    "title": row.title,
                    "final_cost_minor": row.final_cost_minor,
                    "status": row.status,
                    "shares": [
                        {
                            "share_id": share.share_id,
                            "participant_ref": share.participant_ref,
                            "display_name": share.display_name,
                            "share_minor": share.share_minor,
                            "adjusted_minor": share.adjusted_minor,
                            "status": share.status,
                            "is_initiator": share.is_initiator,
                        }
                        for share in shares
                        if share.aa_id == row.aa_id
                    ],
                    "adjustments": row.adjustments,
                }
                for row in rows
            ],
            "note": "AA 只创建收款请求；参与人付款需各自授权，未收款不计入可支配资金",
        }
    )


@router.get("/v1/schedules")
def list_schedules(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..kernel.scheduled_jobs import list_jobs

    return ok({"items": list_jobs(session, ctx)})


@router.get("/v1/catalog/merchants")
def list_merchant_quotes(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..models import MerchantQuote

    rows = session.execute(select(MerchantQuote).where(MerchantQuote.inventory > 0)).scalars().all()
    return ok(
        {
            "items": [
                {
                    "quote_id": row.quote_id,
                    "merchant_id": row.merchant_id,
                    "offer_code": row.offer_code,
                    "title": row.title,
                    "amount_minor": row.amount_minor,
                    "fee_minor": row.fee_minor,
                    "cancel_fee_minor": row.cancel_fee_minor,
                    "refundable": row.refundable,
                    "inventory": row.inventory,
                    "terms_version": row.terms_version,
                    "observed_at": format_iso(row.observed_at),
                    "untrusted_text": row.untrusted_text,
                }
                for row in rows
            ],
            "note": "商户自由文本为不可信数据，不参与金额与授权判断",
        }
    )
