"""理财规则领域测试：测评、适当性、条款、估算份额与产品比较。

覆盖 S3-01／S3-02／S3-05 与 T09／T10／T11／T46。金额：整数分；份额：1e-8 定点。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from finpilot import models
from finpilot.domains.wealth_rules import (
    assessment,
    compare,
    current_profile,
    estimate_shares,
    expected_settlement_date,
    product_terms,
    questionnaire,
    score_answers,
    settlement_explanation,
    submit_assessment,
    terms,
    validate_domain,
)
from finpilot.domains.wealth_rules.compare import compare_products, ensure_no_guarantee_wording
from finpilot.errors import FinPilotError
from finpilot.kernel.identity import AuthContext
from finpilot.money import format_shares_e8
from finpilot.timeutil import SHANGHAI, format_iso, utcnow

USER = "user_demo_01"
ACCOUNT = "account_salary"


def ctx_for(user_id: str = USER) -> AuthContext:
    return AuthContext(user_id=user_id, roles=("customer",), channel="app", session_id="sess_wealth_test")


def sh(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


ALL_MAX_ANSWERS = {q["question_id"]: q["options"][-1]["option_id"] for q in questionnaire()["questions"]}
ALL_MIN_ANSWERS = {q["question_id"]: q["options"][0]["option_id"] for q in questionnaire()["questions"]}


def answers(**overrides) -> dict:
    """默认给一套 R3 档答案（各维度 2 分），可逐题覆盖。"""
    base = {q["question_id"]: q["options"][2]["option_id"] for q in questionnaire()["questions"]}
    base.update(overrides)
    return base


def add_product(session, product_id: str, **overrides) -> models.WealthProduct:
    payload = {
        "name": f"产品 {product_id}",
        "issuer": "模拟银行理财",
        "product_type": "fixed_income",
        "terms_version": "2026.10",
        "issuer_risk_level": "R2",
        "min_minor": 10_000,
        "fee_bps": 0,
        "fee_fixed_minor": 0,
        "term_days": 0,
        "open_days": [0, 1, 2, 3, 4],
        "cutoff_time": "15:00",
        "liquidity_terms": "开放式，工作日可赎回",
        "cancel_terms": "确认前可撤单",
        "cooling_off_days": 0,
        "settlement_days": 1,
        "history_ref": "",
        "meta": {},
        "is_active": True,
    }
    payload.update(overrides)
    product = models.WealthProduct(product_id=product_id, **payload)
    session.add(product)
    # ProductNav / WealthOrder 通过外键指向产品，模型没有 relationship，
    # 因此这里显式 flush，保证后续插入满足外键顺序。
    session.flush()
    return product


def add_navs(
    session,
    product_id: str,
    count: int,
    *,
    start: date = date(2025, 1, 1),
    nav_start_e6: int = 1_000_000,
    daily_return_bps: float = 5.0,
) -> None:
    nav = float(nav_start_e6)
    for index in range(count):
        day = start + timedelta(days=index)
        session.add(
            models.ProductNav(
                nav_id=f"nav_{product_id}_{index:04d}",
                product_id=product_id,
                as_of=day,
                nav_e6=int(round(nav)),
                daily_return_bps=daily_return_bps,
                terms_version="2026.10",
            )
        )
        nav *= 1.0 + daily_return_bps / 10_000.0


def add_profile(
    session,
    *,
    owner_user_id: str = USER,
    risk_level: str = "R3",
    is_active: bool = True,
    assessed_days_ago: int = 1,
    valid_for_days: int = 300,
) -> models.RiskProfile:
    now = utcnow()
    profile = models.RiskProfile(
        owner_user_id=owner_user_id,
        questionnaire_version=assessment.QUESTIONNAIRE_VERSION,
        answers=answers(),
        score=12,
        risk_level=risk_level,
        max_loss_tolerance="medium",
        assessed_at=now - timedelta(days=assessed_days_ago),
        expires_at=now + timedelta(days=valid_for_days),
        version=1,
        is_active=is_active,
    )
    session.add(profile)
    return profile


def snapshot(spendable_minor: int = 1_000_000, observed_at: str | None = None) -> dict:
    return {
        "accounts": [
            {
                "account_id": ACCOUNT,
                "spendable_minor": spendable_minor,
                "available_minor": spendable_minor,
            }
        ],
        "group": {"account_ids": [ACCOUNT], "spendable_minor": spendable_minor},
        "observed_at": observed_at or format_iso(utcnow()),
    }


def purchase(product_id: str, amount_minor: int = 100_000, **extra) -> dict:
    payload = {"tool": "wealth.purchase", "product_id": product_id, "amount_minor": amount_minor}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# 问卷与测评
# ---------------------------------------------------------------------------


def test_questionnaire_is_versioned_and_covers_six_dimensions():
    data = questionnaire()
    assert data["questionnaire_version"] == assessment.QUESTIONNAIRE_VERSION == "2026.10"
    assert data["validity_days"] == 365
    assert [d["dimension"] for d in data["dimensions"]] == [
        "investment_experience",
        "loss_tolerance",
        "risk_attitude",
        "investment_horizon",
        "liquidity_need",
        "capital_purpose",
    ]
    assert len(data["questions"]) == 6
    for question in data["questions"]:
        assert len(question["options"]) == 5, "每题 5 个单选选项（0—4 分）"
        assert [option["score"] for option in question["options"]] == [0, 1, 2, 3, 4]
    assert "模拟规则" in data["disclaimer"]


def test_score_answers_maps_to_risk_level_with_explainable_cap():
    all_max = score_answers(ALL_MAX_ANSWERS)
    assert all_max["score"] == 24
    assert all_max["risk_level"] == "R5"
    assert all_max["max_loss_tolerance"] == "high"
    assert all_max["caps_applied"] == []
    assert set(all_max["dimension_scores"]) == {
        "investment_experience", "loss_tolerance", "risk_attitude",
        "investment_horizon", "liquidity_need", "capital_purpose",
    }

    all_min = score_answers(ALL_MIN_ANSWERS)
    assert all_min["score"] == 0
    assert all_min["risk_level"] == "R1"
    assert all_min["max_loss_tolerance"] == "low"

    # 态度与期限都很激进，但承受损失能力只有 1 分 → 封顶 R2
    capped = score_answers(
        answers(
            q_investment_experience="exp_multi",
            q_loss_tolerance="loss_5",
            q_risk_attitude="att_very_high",
            q_investment_horizon="hz_3yplus",
            q_liquidity_need="lq_none",
            q_capital_purpose="cp_growth",
        )
    )
    assert capped["score"] == 21
    assert capped["raw_risk_level"] == "R5"
    assert capped["risk_level"] == "R2"
    assert capped["caps_applied"] == ["loss_tolerance_cap:R5->R2"]
    assert any("承受损失能力上限" in reason for reason in capped["reasons"])
    assert any("总分 21/24" in reason for reason in capped["reasons"])


@pytest.mark.parametrize(
    "bad_answers,reason",
    [
        ({"q_investment_experience": "exp_multi"}, "missing_answers"),
        ({**ALL_MAX_ANSWERS, "q_unknown": "x"}, "unknown_question"),
        ({**ALL_MAX_ANSWERS, "q_loss_tolerance": "loss_999"}, "unknown_option"),
    ],
)
def test_score_answers_rejects_incomplete_or_unknown(bad_answers, reason):
    with pytest.raises(FinPilotError) as excinfo:
        score_answers(bad_answers)
    assert excinfo.value.code == "PARAM_INVALID"
    assert reason in excinfo.value.details["reason_codes"]


def test_submit_assessment_writes_versioned_profile_with_one_year_validity(session, seed):
    profile = submit_assessment(session, ctx_for(), ALL_MAX_ANSWERS)
    session.commit()
    assert profile.questionnaire_version == "2026.10"
    assert profile.score == 24
    assert profile.risk_level == "R5"
    assert profile.max_loss_tolerance == "high"
    assert (profile.expires_at - profile.assessed_at).days == 365
    assert profile.assessed_at == utcnow()

    state = current_profile(session, ctx_for())
    assert state["status"] == "valid"
    assert state["usable"] is True
    assert state["risk_level"] == "R5"
    assert state["reason_codes"] == ["profile_valid"]
    assert state["days_remaining"] == pytest.approx(365, abs=1)

    # 重新测评：旧记录置为非活动，新记录版本递增
    second = submit_assessment(session, ctx_for(), ALL_MIN_ANSWERS)
    session.commit()
    assert second.version == 2
    session.refresh(profile)
    assert profile.is_active is False
    assert current_profile(session, ctx_for())["risk_level"] == "R1"


def test_current_profile_reports_none_and_expired(session, seed):
    none_state = current_profile(session, ctx_for())
    assert none_state["status"] == "none"
    assert none_state["usable"] is False
    assert none_state["risk_level"] is None
    assert none_state["reason_codes"] == ["profile_not_found"]

    add_profile(session, risk_level="R4", assessed_days_ago=400, valid_for_days=-35)
    session.commit()
    expired = current_profile(session, ctx_for())
    assert expired["status"] == "expired"
    assert expired["usable"] is False
    assert expired["risk_level"] is None, "过期测评不得再作为有效风险等级"
    assert expired["score"] == 12, "分数仍然回显，便于用户理解"
    assert "assessment_required" in expired["reason_codes"]
    assert expired["stale_note"]


# ---------------------------------------------------------------------------
# 适当性硬约束
# ---------------------------------------------------------------------------


def test_purchase_without_assessment_is_blocked(session, seed):
    add_product(session, "p_r2", issuer_risk_level="R2")
    add_navs(session, "p_r2", 60)
    session.commit()

    result = validate_domain(session, ctx_for(), purchase("p_r2"), snapshot())
    assert result["valid"] is False
    assert result["reason_codes"] == ["ASSESSMENT_REQUIRED"]
    assert result["blocking_reason_codes"] == ["ASSESSMENT_REQUIRED"]
    assert result["validation_version"] == "suitability-2026.10"
    check = next(c for c in result["checks"] if c["reason_code"] == "ASSESSMENT_REQUIRED")
    assert check["passed"] is False and check["blocking"] is True
    assert check["detail"]["profile_status"] == "none"


def test_expired_assessment_is_treated_as_missing(session, seed):
    add_product(session, "p_r2", issuer_risk_level="R2")
    add_navs(session, "p_r2", 60)
    add_profile(session, risk_level="R4", assessed_days_ago=400, valid_for_days=-35)
    session.commit()

    result = validate_domain(session, ctx_for(), purchase("p_r2"), snapshot())
    assert result["valid"] is False
    assert result["blocking_reason_codes"] == ["ASSESSMENT_REQUIRED"]
    assert result["profile"]["status"] == "expired"
    assert result["profile"]["risk_level"] is None


def test_valid_profile_passes_and_returns_estimate(session, seed):
    add_product(session, "p_r2", issuer_risk_level="R2", min_minor=10_000, fee_bps=30)
    add_navs(session, "p_r2", 60, nav_start_e6=1_023_400, daily_return_bps=0.0)
    add_profile(session, risk_level="R3")
    session.commit()

    result = validate_domain(session, ctx_for(), purchase("p_r2", 100_000), snapshot())
    assert result["valid"] is True
    assert result["reason_codes"] == []
    assert result["blocking_reason_codes"] == []
    assert {(c["rule"], c["passed"]) for c in result["checks"]} >= {
        ("assessment_valid", True),
        ("risk_within_profile", True),
        ("term_within_horizon", True),
        ("budget_within_limits", True),
        ("nav_history_sufficient", True),
    }
    estimate = result["estimate"]
    assert estimate["is_estimate"] is True
    assert estimate["confirmed_shares_e8"] is None
    assert estimate["estimated_shares_e8"] == estimate_shares(100_000, 1_023_400, 300)["estimated_shares_e8"]
    assert estimate["estimate_only"] is False
    # 校验结果必须能直接放进信封
    json.dumps(result, ensure_ascii=False)


def test_risk_mismatch_blocks_above_profile(session, seed):
    add_product(session, "p_r4", issuer_risk_level="R4")
    add_navs(session, "p_r4", 60)
    add_profile(session, risk_level="R2")
    session.commit()

    result = validate_domain(session, ctx_for(), purchase("p_r4"), snapshot())
    assert result["valid"] is False
    assert "RISK_MISMATCH" in result["blocking_reason_codes"]
    check = next(c for c in result["checks"] if c["reason_code"] == "RISK_MISMATCH")
    assert check["detail"]["risk_rank"] == {"product": 4, "user": 2}

    # 同等级不阻断
    add_product(session, "p_r2", issuer_risk_level="R2")
    add_navs(session, "p_r2", 60)
    session.commit()
    assert validate_domain(session, ctx_for(), purchase("p_r2"), snapshot())["valid"] is True


def test_term_conflict_with_use_of_funds_date(session, seed):
    add_product(session, "p_long", issuer_risk_level="R2", term_days=180, settlement_days=1)
    add_navs(session, "p_long", 60)
    add_profile(session, risk_level="R3")
    session.commit()

    conflict = validate_domain(
        session, ctx_for(), purchase("p_long", 100_000, horizon_days=90), snapshot()
    )
    assert conflict["valid"] is False
    assert "TERM_CONFLICT" in conflict["blocking_reason_codes"]
    check = next(c for c in conflict["checks"] if c["reason_code"] == "TERM_CONFLICT")
    assert check["detail"]["required_holding_days"] == 181
    assert "181 天" in check["message"]

    ok = validate_domain(session, ctx_for(), purchase("p_long", 100_000, horizon_days=200), snapshot())
    assert ok["valid"] is True
    assert "TERM_CONFLICT" not in ok["reason_codes"]


def test_budget_checks_cover_min_amount_fee_and_spendable(session, seed):
    add_product(session, "p_min", issuer_risk_level="R2", min_minor=100_000, fee_bps=100)
    add_navs(session, "p_min", 60)
    add_profile(session, risk_level="R3")
    session.commit()

    below_min = validate_domain(session, ctx_for(), purchase("p_min", 50_000), snapshot())
    assert below_min["valid"] is False
    detail = next(c for c in below_min["checks"] if c["reason_code"] == "BUDGET_EXCEEDED")["detail"]
    assert detail["reason_codes"] == ["below_min_amount"]

    fee_over = validate_domain(
        session, ctx_for(), purchase("p_min", 100_000, budget_minor=100_000), snapshot()
    )
    assert fee_over["valid"] is False
    detail = next(c for c in fee_over["checks"] if c["reason_code"] == "BUDGET_EXCEEDED")["detail"]
    assert detail["reason_codes"] == ["fee_over_budget"]
    assert detail["fee_minor"] == 1_000
    assert detail["total_cost_minor"] == 101_000

    over_spendable = validate_domain(session, ctx_for(), purchase("p_min", 100_000), snapshot(50_000))
    assert over_spendable["valid"] is False
    detail = next(c for c in over_spendable["checks"] if c["reason_code"] == "BUDGET_EXCEEDED")["detail"]
    assert detail["reason_codes"] == ["insufficient_spendable"]

    ok = validate_domain(
        session, ctx_for(), purchase("p_min", 100_000, budget_minor=200_000), snapshot(1_000_000)
    )
    assert ok["valid"] is True
    assert ok["fee_minor"] == 1_000
    assert ok["total_cost_minor"] == 101_000


def test_nav_history_boundary_and_missing_nav(session, seed):
    add_product(session, "p_60", issuer_risk_level="R2")
    add_navs(session, "p_60", 60, start=date(2026, 6, 1))
    add_product(session, "p_59", issuer_risk_level="R2")
    add_navs(session, "p_59", 59, start=date(2026, 6, 1))
    add_product(session, "p_10", issuer_risk_level="R2")
    add_navs(session, "p_10", 10, start=date(2026, 8, 1))
    add_product(session, "p_none", issuer_risk_level="R2")
    add_profile(session, risk_level="R3")
    session.commit()

    enough = validate_domain(session, ctx_for(), purchase("p_60"), snapshot())
    assert enough["valid"] is True
    assert "INSUFFICIENT_HISTORY" not in enough["reason_codes"]
    assert enough["estimate"]["estimate_only"] is False

    short = validate_domain(session, ctx_for(), purchase("p_59"), snapshot())
    assert short["valid"] is True, "样本不足不阻断申购，但只能给估算"
    assert "INSUFFICIENT_HISTORY" in short["reason_codes"]
    assert short["blocking_reason_codes"] == []
    check = next(c for c in short["checks"] if c["reason_code"] == "INSUFFICIENT_HISTORY")
    assert check["blocking"] is False
    assert check["detail"]["sample_count"] == 59
    assert short["estimate"]["estimate_only"] is True

    tiny = validate_domain(session, ctx_for(), purchase("p_10"), snapshot())
    assert tiny["valid"] is True
    assert tiny["estimate"]["estimated_shares_e8"] is not None
    assert tiny["estimate"]["range"]["min_shares_e8"] is not None

    none_at_all = validate_domain(session, ctx_for(), purchase("p_none"), snapshot())
    assert none_at_all["valid"] is False
    assert "NAV_UNAVAILABLE" in none_at_all["blocking_reason_codes"]
    assert "INSUFFICIENT_HISTORY" in none_at_all["reason_codes"]
    assert none_at_all["estimate"]["estimated_shares_e8"] is None
    assert none_at_all["estimate"]["status"] == "not_applicable"
    assert none_at_all["estimate"]["reason_code"] == "nav_unavailable"


def test_validate_domain_is_not_applicable_for_other_tools(session, seed):
    add_product(session, "p_r2", issuer_risk_level="R2")
    add_navs(session, "p_r2", 60)
    add_profile(session, risk_level="R3")
    session.commit()

    redeem = {"tool": "wealth.redeem", "product_id": "p_r2", "amount_minor": 1000}
    result = validate_domain(session, ctx_for(), redeem, snapshot())
    assert result["applicable"] is False
    assert result["valid"] is True
    assert result["reason_codes"] == ["NOT_APPLICABLE_TOOL"]

    transfer = {"tool": "transfer.submit", "amount_minor": 1000}
    assert validate_domain(session, ctx_for(), transfer, snapshot())["applicable"] is False

    with pytest.raises(FinPilotError) as missing_product:
        validate_domain(session, ctx_for(), {"tool": "wealth.purchase", "amount_minor": 1000}, snapshot())
    assert missing_product.value.code == "PARAM_INVALID"

    with pytest.raises(FinPilotError) as unknown_product:
        validate_domain(session, ctx_for(), purchase("p_missing"), snapshot())
    assert unknown_product.value.code == "PARAM_INVALID"
    assert unknown_product.value.details["reason_codes"] == ["product_not_found"]

    with pytest.raises(FinPilotError) as bad_amount:
        validate_domain(session, ctx_for(), purchase("p_r2", 0), snapshot())
    assert bad_amount.value.code == "PARAM_INVALID"
    assert bad_amount.value.details["reason_codes"] == ["bad_amount"]


def test_validate_domain_accepts_operation_orm_object(session, seed):
    add_product(session, "p_r2", issuer_risk_level="R2")
    add_navs(session, "p_r2", 60)
    add_profile(session, risk_level="R3")
    operation = models.Operation(
        operation_id="op_wealth_1",
        owner_user_id=USER,
        tool="wealth.purchase",
        typed_args={"product_id": "p_r2", "horizon_days": 30},
        parameters_hash="ph",
        idempotency_key="ik-wealth-1",
        request_hash="rh",
        status="awaiting_authorization",
        required_level="RED",
        source_account_id=ACCOUNT,
        amount_minor=100_000,
        fee_minor=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(operation)
    session.commit()

    result = validate_domain(session, ctx_for(), operation, snapshot())
    assert result["operation_id"] == "op_wealth_1"
    assert result["product_id"] == "p_r2"
    assert result["amount_minor"] == 100_000
    assert result["valid"] is True


def test_stale_snapshot_is_warned_but_not_blocking(session, seed):
    add_product(session, "p_r2", issuer_risk_level="R2")
    add_navs(session, "p_r2", 60)
    add_profile(session, risk_level="R3")
    session.commit()
    stale = format_iso(utcnow() - timedelta(hours=2))
    result = validate_domain(session, ctx_for(), purchase("p_r2"), snapshot(1_000_000, stale))
    assert result["valid"] is True
    assert "STALE_SNAPSHOT" in result["reason_codes"]
    assert "STALE_SNAPSHOT" not in result["blocking_reason_codes"]


# ---------------------------------------------------------------------------
# 条款、到账与撤单
# ---------------------------------------------------------------------------


def test_estimate_shares_is_integer_fixed_point_and_separate_from_confirmed(session, seed):
    # (100000 - 300) * 1e14 // 1023400 = 97420.36349423 份 → 9742036349423（1e-8）
    estimate = estimate_shares(100_000, 1_023_400, 300)
    assert estimate["estimated_shares_e8"] == 9_742_036_349_423
    assert format_shares_e8(estimate["estimated_shares_e8"]) == "97420.36349423"
    assert estimate["is_estimate"] is True
    assert estimate["confirmed_shares_e8"] is None
    assert estimate["rounding"] == "floor"
    assert estimate["net_amount_minor"] == 99_700

    assert estimate_shares(100_000, None, 0)["status"] == "not_applicable"
    assert estimate_shares(100_000, None, 0)["estimated_shares_e8"] is None
    with pytest.raises(FinPilotError):
        estimate_shares(1_000, 1_023_400, 1_000)

    add_product(session, "p_r2")
    order = models.WealthOrder(
        order_id="wo_1",
        operation_id="op_1",
        owner_user_id=USER,
        account_id=ACCOUNT,
        product_id="p_r2",
        side="purchase",
        amount_minor=100_000,
        fee_minor=300,
        estimated_shares_e8=estimate["estimated_shares_e8"],
        confirmed_shares_e8=0,
        status="submitted",
        product_terms_version="2026.10",
        created_at=utcnow(),
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    assert order.estimated_shares_e8 == 9_742_036_349_423, "估算份额单独保存"
    assert order.confirmed_shares_e8 == 0, "受理不等于确认份额，正式份额不得由估算填充"


def test_settlement_date_uses_trading_calendar_and_cutoff():
    product = {
        "open_days": [0, 1, 2, 3, 4],
        "cutoff_time": "15:00",
        "settlement_days": 1,
        "terms_version": "2026.10",
    }
    friday_morning = sh(2026, 10, 23, 10)
    friday_evening = sh(2026, 10, 23, 16)
    saturday = sh(2026, 10, 24, 10)

    assert friday_morning.date().weekday() == 4
    assert expected_settlement_date(product, friday_morning) == date(2026, 10, 26), "周五截止前 T+1 → 周一"
    assert expected_settlement_date(product, friday_evening) == date(2026, 10, 27), "周五截止后顺延到周一交易日"
    assert expected_settlement_date(product, saturday) == date(2026, 10, 27), "周末不是开放日"

    detail = settlement_explanation(product, friday_morning)
    assert detail["trade_day"] == "2026-10-23"
    assert detail["expected_settlement_date_iso"] == "2026-10-26"
    assert detail["guaranteed"] is False
    assert "不构成到账承诺" in detail["note"]

    three_days = {**product, "settlement_days": 3}
    assert expected_settlement_date(three_days, friday_morning) == date(2026, 10, 28)

    all_days = {**product, "open_days": ["daily"], "settlement_days": 1}
    assert expected_settlement_date(all_days, sh(2026, 10, 24, 10)) == date(2026, 10, 25)


def test_can_cancel_is_terms_driven():
    cancellable = {
        "cancel_terms": "确认前可撤单，撤单不收费",
        "cooling_off_days": 7,
        "cutoff_time": "15:00",
        "open_days": [0, 1, 2, 3, 4],
        "terms_version": "2026.10",
    }
    submitted = sh(2026, 10, 20, 10)
    within = terms.can_cancel(cancellable, submitted, sh(2026, 10, 24, 10))
    assert within["can_cancel"] is True
    assert within["reason_code"] == "within_cooling_off"
    assert within["window_end"] == "2026-10-27T10:00:00+08:00"

    outside = terms.can_cancel(cancellable, submitted, sh(2026, 10, 28, 10))
    assert outside["can_cancel"] is False
    assert outside["reason_code"] == "window_closed"

    # 条款明确不可撤：即使仍在上面的窗口内也不得撤
    not_cancellable = {**cancellable, "cancel_terms": "本产品成立后不可撤销"}
    denied = terms.can_cancel(not_cancellable, submitted, sh(2026, 10, 21, 10))
    assert denied["can_cancel"] is False
    assert denied["reason_code"] == "terms_disallow_cancel"

    # 没有条款依据 → 不承诺可撤
    no_terms = {**cancellable, "cancel_terms": ""}
    unknown = terms.can_cancel(no_terms, submitted, sh(2026, 10, 21, 10))
    assert unknown["can_cancel"] is False
    assert unknown["reason_code"] == "terms_not_supported"
    assert "不得普遍承诺" in unknown["note"]

    # 无冷静期：只能当日截止时间前撤
    cutoff_only = {**cancellable, "cooling_off_days": 0}
    before_cutoff = terms.can_cancel(cutoff_only, submitted, sh(2026, 10, 20, 11))
    assert before_cutoff["can_cancel"] is True
    assert before_cutoff["reason_code"] == "within_cutoff_window"
    after_cutoff = terms.can_cancel(cutoff_only, submitted, sh(2026, 10, 20, 16))
    assert after_cutoff["can_cancel"] is False
    assert after_cutoff["window_end"] == "2026-10-20T15:00:00+08:00"


def test_product_terms_view_has_no_guarantee_flags():
    product = {
        "product_id": "p_terms",
        "name": "稳利 90 天",
        "issuer": "模拟银行理财",
        "product_type": "fixed_income",
        "issuer_risk_level": "R2",
        "terms_version": "2026.10",
        "min_minor": 100_000,
        "fee_bps": 30,
        "fee_fixed_minor": 0,
        "term_days": 90,
        "open_days": [0, 1, 2, 3, 4],
        "cutoff_time": "15:00",
        "liquidity_terms": "到期自动赎回",
        "cancel_terms": "",
        "cooling_off_days": 0,
        "settlement_days": 1,
        "history_ref": "h1",
        "is_active": True,
    }
    view = product_terms(product)
    assert view["open_day_rule"]["text"] == "工作日（周一至周五）为开放日"
    assert view["return_guaranteed"] is False
    assert view["settlement_guaranteed"] is False
    assert view["fee_formula"] == "fee = floor(amount_minor * fee_bps / 10000) + fee_fixed_minor"
    assert terms.product_fee_minor(product, 100_000) == 300

    with pytest.raises(FinPilotError):
        terms.cutoff_time({**product, "cutoff_time": "25:00"})
    with pytest.raises(FinPilotError):
        terms.normalize_open_days({**product, "open_days": ["funday"]})


# ---------------------------------------------------------------------------
# 产品比较
# ---------------------------------------------------------------------------


@pytest.fixture()
def catalogue(session, seed):
    """三档产品：R1 低门槛、R3 中门槛、R5 高风险、长期限、高价门槛。"""
    add_product(session, "p_low", issuer_risk_level="R1", min_minor=10_000, fee_bps=0, settlement_days=1)
    add_navs(session, "p_low", 300, daily_return_bps=2.0)
    add_product(session, "p_mid", issuer_risk_level="R3", min_minor=100_000, fee_bps=30, settlement_days=1)
    add_navs(session, "p_mid", 300, daily_return_bps=6.0)
    add_product(session, "p_high", issuer_risk_level="R5", min_minor=10_000, fee_bps=0)
    add_navs(session, "p_high", 300, daily_return_bps=15.0)
    add_product(session, "p_long", issuer_risk_level="R2", min_minor=10_000, term_days=365, settlement_days=1)
    add_navs(session, "p_long", 300, daily_return_bps=8.0)
    add_product(session, "p_pricey", issuer_risk_level="R2", min_minor=10_000_000, fee_bps=50)
    add_navs(session, "p_pricey", 300, daily_return_bps=9.0)
    add_profile(session, risk_level="R3")
    session.commit()
    return ["p_low", "p_mid", "p_high", "p_long", "p_pricey"]


def eligible_ids(result) -> list[str]:
    return [item["product_id"] for item in result if item["eligible"]]


def excluded_map(result) -> dict[str, list[str]]:
    return {item["product_id"]: item["exclusion_reason_codes"] for item in result if not item["eligible"]}


def test_compare_applies_hard_constraints_before_ranking(session, catalogue):
    result = compare_products(
        session,
        ctx_for(),
        budget_minor=500_000,
        horizon_days=180,
        preferences=["liquidity", "fee"],
    )
    assert eligible_ids(result) == ["p_low", "p_mid"]  # 期限都最短，再按费率
    exclusions = excluded_map(result)
    assert exclusions["p_high"] == ["RISK_MISMATCH"]
    assert exclusions["p_long"] == ["TERM_CONFLICT"]
    assert exclusions["p_pricey"] == ["BUDGET_EXCEEDED"]

    # 被排除的产品也返回，附可解释原因，且排在最后
    assert [item["eligible"] for item in result] == [True, True, False, False, False]
    pricey = next(item for item in result if item["product_id"] == "p_pricey")
    assert any(reason["code"] == "EXCLUDED_BUDGET_EXCEEDED" for reason in pricey["reasons"])
    assert pricey["rank"] is None
    assert pricey["min_total_cost_minor"] > 500_000

    ranks = [item["rank"] for item in result if item["eligible"]]
    assert ranks == [1, 2]
    assert all(item["data_version"]["compare_version"] == compare.COMPARE_VERSION for item in result)
    assert all(item["data_version"]["terms_version"] == "2026.10" for item in result)


def test_compare_ranking_follows_explicit_user_preference(session, catalogue):
    # p_high 是 R5 被 R3 测评硬过滤；p_pricey 起购 100000 元超过预算，也被过滤
    by_yield = compare_products(session, ctx_for(), budget_minor=500_000, horizon_days=400,
                                preferences=["yield"])
    assert eligible_ids(by_yield) == ["p_long", "p_mid", "p_low"]
    assert excluded_map(by_yield) == {
        "p_high": ["RISK_MISMATCH"],
        "p_pricey": ["BUDGET_EXCEEDED"],
    }

    by_fee = compare_products(session, ctx_for(), budget_minor=500_000, horizon_days=400,
                              preferences=["fee"])
    assert eligible_ids(by_fee) == ["p_long", "p_low", "p_mid"]

    by_liquidity_then_fee = compare_products(
        session, ctx_for(), budget_minor=500_000, horizon_days=400, preferences=["liquidity", "fee"]
    )
    assert eligible_ids(by_liquidity_then_fee) == ["p_low", "p_mid", "p_long"]

    top = by_yield[0]
    assert top["product_id"] == "p_long"
    assert any(reason["code"] == "PREFERENCE_ORDER_APPLIED" for reason in top["reasons"])
    assert any(reason["code"] == "PREFERENCE_YIELD_VALUE" for reason in top["reasons"])
    assert top["history"]["history_metric"]["status"] == "ok"
    assert top["history"]["history_metric"]["value"] > 0
    # 收益偏好下的排序单调：年化收益从高到低
    yields = [item["history"]["history_metric"]["value"] for item in by_yield if item["eligible"]]
    assert yields == sorted(yields, reverse=True)
    # 比较结果必须能直接放进信封
    json.dumps(by_yield, ensure_ascii=False)


def test_compare_uses_public_default_priority_when_user_gives_none(session, catalogue):
    result = compare_products(session, ctx_for(), budget_minor=500_000, horizon_days=400, preferences={})
    assert eligible_ids(result) == ["p_low", "p_long", "p_mid"], "默认顺序以风险为先"
    top = result[0]
    assert any(reason["code"] == "DEFAULT_PRIORITY_USED" for reason in top["reasons"])
    assert "公开默认顺序" in next(
        reason["detail"] for reason in top["reasons"] if reason["code"] == "DEFAULT_PRIORITY_USED"
    )
    assert [item["rank"] for item in result if item["eligible"]] == [1, 2, 3]


def test_compare_reports_not_applicable_history_instead_of_zero(session, seed):
    add_product(session, "p_short", issuer_risk_level="R2", min_minor=10_000)
    add_navs(session, "p_short", 10, start=date(2026, 8, 1))
    add_product(session, "p_none", issuer_risk_level="R2", min_minor=10_000)
    add_profile(session, risk_level="R3")
    session.commit()

    result = compare_products(
        session, ctx_for(), budget_minor=100_000, horizon_days=90, preferences=["yield", "fee"]
    )
    short = next(item for item in result if item["product_id"] == "p_short")
    metric = short["history"]["history_metric"]
    assert metric["value"] is None, "样本不足不得填零"
    assert metric["status"] == "not_applicable"
    assert metric["reason_code"] == "insufficient_history"
    assert metric["sample_count"] == 10
    assert short["history"]["nav_points"] == 10
    assert any(reason["code"] == "PREFERENCE_YIELD_UNAVAILABLE" for reason in short["reasons"])

    none_item = next(item for item in result if item["product_id"] == "p_none")
    assert none_item["history"]["history_metric"]["value"] is None
    assert none_item["history"]["applicable"] is False


def test_compare_is_advisory_only_without_valid_assessment(session, seed):
    add_product(session, "p_high", issuer_risk_level="R5", min_minor=10_000)
    add_navs(session, "p_high", 300)
    session.commit()
    result = compare_products(session, ctx_for(), budget_minor=100_000, horizon_days=90, preferences=["fee"])
    assert eligible_ids(result) == ["p_high"], "没有有效测评时只做一般信息比较，不偷偷套用风险阈值"
    assert any(reason["code"] == "ADVISORY_ONLY" for reason in result[0]["reasons"])
    assert "必须完成有效测评" in next(
        reason["detail"] for reason in result[0]["reasons"] if reason["code"] == "ADVISORY_ONLY"
    )


def test_compare_honours_explicit_user_risk_ceiling_only_when_given(session, catalogue):
    with_ceiling = compare_products(
        session, ctx_for(), budget_minor=500_000, horizon_days=400,
        preferences={"priority": ["fee"], "risk_level_max": "R2"},
    )
    assert excluded_map(with_ceiling)["p_mid"] == ["RISK_ABOVE_USER_LIMIT"]

    without_ceiling = compare_products(
        session, ctx_for(), budget_minor=500_000, horizon_days=400, preferences={"priority": ["fee"]}
    )
    assert "p_mid" in eligible_ids(without_ceiling)


def test_compare_output_has_no_guarantee_wording(session, catalogue):
    result = compare_products(
        session, ctx_for(), budget_minor=500_000, horizon_days=400, preferences=["yield"]
    )
    assert ensure_no_guarantee_wording(result) == []
    payload = json.dumps(result, ensure_ascii=False, default=str)
    for token in compare.FORBIDDEN_GUARANTEE_TOKENS:
        assert token not in payload, f"输出不得包含收益保证措辞：{token}"
    assert "不构成任何形式的收益担保" in result[0]["disclaimer"]
    assert result[0]["data_version"]["quant_metrics_included"] is True


@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"budget_minor": 0, "horizon_days": 90}, "budget_minor"),
        ({"budget_minor": 100_000, "horizon_days": 0}, "horizon_days"),
    ],
)
def test_compare_rejects_bad_scalars(session, seed, kwargs, field):
    with pytest.raises(FinPilotError) as excinfo:
        compare_products(session, ctx_for(), preferences=["fee"], **kwargs)
    assert excinfo.value.code == "PARAM_INVALID"


def test_compare_rejects_unknown_preference(session, seed):
    with pytest.raises(FinPilotError) as excinfo:
        compare_products(session, ctx_for(), budget_minor=100_000, horizon_days=90, preferences=["安全"])
    assert excinfo.value.code == "PARAM_INVALID"
    assert excinfo.value.details["reason_codes"] == ["unknown_preference"]
    with pytest.raises(FinPilotError) as bad_level:
        compare_products(
            session, ctx_for(), budget_minor=100_000, horizon_days=90,
            preferences={"priority": ["fee"], "risk_level_max": "R9"},
        )
    assert bad_level.value.code == "PARAM_INVALID"


def test_compare_budget_uses_total_cost_ceiling(session, seed):
    add_product(session, "p_fee", issuer_risk_level="R2", min_minor=10_000, fee_bps=100)
    add_navs(session, "p_fee", 300, daily_return_bps=3.0)
    add_product(session, "p_free", issuer_risk_level="R2", min_minor=10_000, fee_bps=0)
    add_navs(session, "p_free", 300, daily_return_bps=3.0)
    add_profile(session, risk_level="R3")
    session.commit()
    result = compare_products(session, ctx_for(), budget_minor=100_000, horizon_days=90, preferences=["fee"])
    fee_item = next(item for item in result if item["product_id"] == "p_fee")
    free_item = next(item for item in result if item["product_id"] == "p_free")
    # 预算内最大本金：p + floor(p) = 100000 → p = 99010（99010 + 990 = 100000）
    assert fee_item["planned_amount_minor"] == 99_010
    assert fee_item["estimated_fee_minor"] == 990
    assert fee_item["estimated_total_cost_minor"] == 100_000 <= 100_000
    assert fee_item["amount_basis"] == "largest_principal_within_budget"
    assert free_item["planned_amount_minor"] == 100_000
    assert free_item["effective_fee_bps"] == 0.0


def test_tool_handler_wealth_compare_is_read_only_and_idempotent(session, seed):
    from finpilot.domains.wealth_rules import TOOL_HANDLERS, wealth_compare_handler

    add_product(session, "p_low", issuer_risk_level="R1", min_minor=10_000)
    add_navs(session, "p_low", 300, daily_return_bps=2.0)
    add_product(session, "p_expensive", issuer_risk_level="R2", min_minor=9_000_000)
    add_navs(session, "p_expensive", 300, daily_return_bps=3.0)
    add_profile(session, risk_level="R3")
    session.commit()

    assert TOOL_HANDLERS["wealth.compare"] is wealth_compare_handler
    products_before = session.query(models.WealthProduct).count()
    args = {
        "tool": "wealth.compare",
        "typed_args": {"budget_minor": 200_000, "horizon_days": 90, "preferences": ["fee"]},
    }
    first = wealth_compare_handler(session, ctx_for(), args)
    second = wealth_compare_handler(session, ctx_for(), args)
    assert first["type"] == "wealth_compare"
    assert first["version"] == compare.COMPARE_VERSION
    assert first["data"]["eligible_count"] == 1
    assert first["data"]["excluded_count"] == 1
    assert first["data"]["excluded_reason_codes"] == {"p_expensive": ["BUDGET_EXCEEDED"]}
    assert [w["code"] for w in first["warnings"]] == ["EXCLUDED_BUDGET_EXCEEDED"]
    assert first == second, "只读处理器必须幂等"
    assert session.query(models.WealthProduct).count() == products_before
    assert ensure_no_guarantee_wording(first["data"]) == []