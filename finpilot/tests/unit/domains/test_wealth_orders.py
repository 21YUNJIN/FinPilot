"""理财申购/赎回：受理、资金效果与份额确认必须分开（T10、T11、S3-03、S3-04）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from finpilot.errors import FinPilotError
from finpilot.kernel.identity import AuthContext
from finpilot.kernel.submission import OperationDraft, prepare_operation, submit_operation
from finpilot.models import Account, Holding, LedgerTransaction, WealthOrder, WealthProduct
from finpilot.timeutil import SHANGHAI, Clock, set_clock

pytestmark = [pytest.mark.integration]

REFERENCE = datetime(2026, 10, 22, 10, 0, tzinfo=SHANGHAI)


@pytest.fixture()
def wealth(session, seed, clock):
    from finpilot.models import ProductNav

    products = [
        WealthProduct(product_id="prod_stable", name="稳健 90 天", issuer_risk_level="R2", min_minor=10_000,
                      fee_bps=0, fee_fixed_minor=0, term_days=90, settlement_days=1, terms_version="2026.10"),
        WealthProduct(product_id="prod_new", name="新发无净值", issuer_risk_level="R2", min_minor=10_000,
                      fee_bps=0, fee_fixed_minor=0, term_days=30, settlement_days=1, terms_version="2026.10"),
    ]
    session.add_all(products)
    session.flush()  # 外键顺序：先产品，后净值
    session.add(ProductNav(product_id="prod_stable", as_of=REFERENCE.date(), nav_e6=1_000_000, daily_return_bps=1.2))
    session.add(
        Holding(
            owner_user_id="user_demo_01",
            account_id="account_savings",
            product_id="prod_stable",
            confirmed_shares_e8=100 * 10**8,
        )
    )
    # 有效风险测评：申购前必须已完成（无测评时由领域层阻断）
    from finpilot.models import RiskProfile

    session.add(
        RiskProfile(
            owner_user_id="user_demo_01",
            questionnaire_version="2026.10",
            answers={"experience": 3, "loss_tolerance": 3, "attitude": 3, "horizon": 3, "liquidity": 3, "purpose": 3},
            score=18,
            risk_level="R4",
            assessed_at=REFERENCE.astimezone(timezone.utc),
            expires_at=(REFERENCE + timedelta(days=365)).astimezone(timezone.utc),
            is_active=True,
        )
    )
    session.commit()
    return {"stable": "prod_stable", "no_nav": "prod_new"}


def _ctx() -> AuthContext:
    return AuthContext(user_id="user_demo_01", roles=("customer",), channel="app", session_id="s")


def _grant_for(session, ctx, operation, level="RED"):
    from finpilot.kernel.grants import create_exact_grant, activate_grant
    from finpilot.security.scope import build_scope

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
    if level == "RED":
        activate_grant(session, ctx, grant_id=grant.grant_id, verification_level="TOTP")
    return grant


def test_purchase_is_accepted_then_confirms_shares(session, seed, wealth, ctx):
    draft = OperationDraft(
        tool="wealth.purchase",
        typed_args={"product_id": wealth["stable"], "source_account_id": "account_savings", "amount_minor": 100_000, "fee_minor": 0},
        operation_id="op_wealth_buy",
        source_account_id="account_savings",
        amount_minor=100_000,
    )
    operation = prepare_operation(session, ctx, draft)
    assert operation.required_level == "RED", "申购按 RED 处理"
    grant = _grant_for(session, ctx, operation)
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    from finpilot.orchestration.executor import tool_handlers

    result = tool_handlers()["wealth.purchase"](session, ctx, operation)
    session.commit()

    order = session.query(WealthOrder).filter_by(operation_id="op_wealth_buy").one()
    assert order.status in ("accepted", "awaiting_confirmation", "confirmed")
    assert order.accepted_at is not None
    # 资金已扣划，份额已按净值估算并确认
    assert session.get(Account, "account_savings").posted_balance_minor == 1_500_000 - 100_000
    assert order.confirmed_shares_e8 == 1000 * 10**8, "净值 1.0000 时 1000 元 = 1000 份（1e-8 定点）"
    assert result["data"]["confirmed_shares"] is not None
    assert result["data"]["settled_at"] is None, "申购不是到账，settled_at 应为空"
    tx = session.query(LedgerTransaction).filter_by(operation_id="op_wealth_buy").one()
    assert tx.kind == "wealth_purchase"


def test_purchase_without_nav_does_not_invent_shares(session, seed, wealth, ctx):
    draft = OperationDraft(
        tool="wealth.purchase",
        typed_args={"product_id": wealth["no_nav"], "source_account_id": "account_savings", "amount_minor": 50_000, "fee_minor": 0},
        operation_id="op_wealth_nonav",
        source_account_id="account_savings",
        amount_minor=50_000,
    )
    operation = prepare_operation(session, ctx, draft)
    grant = _grant_for(session, ctx, operation)
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    from finpilot.orchestration.executor import tool_handlers

    result = tool_handlers()["wealth.purchase"](session, ctx, operation)
    session.commit()
    order = session.query(WealthOrder).filter_by(operation_id="op_wealth_nonav").one()
    assert order.status == "awaiting_confirmation", "净值未知只能等待确认，不得编造份额"
    assert order.confirmed_shares_e8 == 0
    assert result["warnings"], "必须给出净值未知的警告"


def test_redeem_checks_available_shares_and_credits_on_settlement(session, seed, wealth, ctx):
    draft = OperationDraft(
        tool="wealth.redeem",
        typed_args={"product_id": wealth["stable"], "source_account_id": "account_savings", "shares_e8": 40 * 10**8},
        operation_id="op_wealth_redeem",
        source_account_id="account_savings",
        amount_minor=40_000,
    )
    operation = prepare_operation(session, ctx, draft)
    grant = _grant_for(session, ctx, operation)
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    from finpilot.orchestration.executor import tool_handlers

    tool_handlers()["wealth.redeem"](session, ctx, operation)
    session.commit()

    order = session.query(WealthOrder).filter_by(operation_id="op_wealth_redeem").one()
    assert order.status == "settled"
    assert order.accepted_at and order.settled_at, "受理与到账时间必须分别记录"
    holding = session.query(Holding).filter_by(product_id=wealth["stable"]).one()
    assert holding.confirmed_shares_e8 == 60 * 10**8, "份额扣减 40 份"
    assert holding.frozen_shares_e8 == 0
    assert session.get(Account, "account_savings").posted_balance_minor == 1_500_000 + 40_000


def test_redeem_more_than_available_is_rejected(session, seed, wealth, ctx):
    draft = OperationDraft(
        tool="wealth.redeem",
        typed_args={"product_id": wealth["stable"], "source_account_id": "account_savings", "shares_e8": 500 * 10**8},
        operation_id="op_wealth_over",
        source_account_id="account_savings",
        amount_minor=500_000,
    )
    operation = prepare_operation(session, ctx, draft)
    grant = _grant_for(session, ctx, operation)
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    from finpilot.orchestration.executor import tool_handlers

    with pytest.raises(FinPilotError) as excinfo:
        tool_handlers()["wealth.redeem"](session, ctx, operation)
    assert excinfo.value.code == "PARAM_INVALID"
    assert "exceeds_available_shares" in str(excinfo.value.details)
    session.rollback()
    holding = session.query(Holding).filter_by(product_id=wealth["stable"]).one()
    assert holding.frozen_shares_e8 == 0, "被拒绝时不得留下冻结份额"


def test_purchase_is_idempotent_by_operation(session, seed, wealth, ctx):
    draft = OperationDraft(
        tool="wealth.purchase",
        typed_args={"product_id": wealth["stable"], "source_account_id": "account_savings", "amount_minor": 20_000, "fee_minor": 0},
        operation_id="op_wealth_idem",
        source_account_id="account_savings",
        amount_minor=20_000,
    )
    operation = prepare_operation(session, ctx, draft)
    grant = _grant_for(session, ctx, operation)
    session.commit()
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    session.commit()

    from finpilot.orchestration.executor import tool_handlers

    handler = tool_handlers()["wealth.purchase"]
    handler(session, ctx, operation)
    session.commit()
    handler(session, ctx, operation)
    session.commit()
    assert session.query(WealthOrder).filter_by(operation_id="op_wealth_idem").count() == 1, "同一操作只建一个订单"