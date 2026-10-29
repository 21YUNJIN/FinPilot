"""Hard filters, preference ordering, ablation and determinism."""
from datetime import date, timedelta

import pytest

from stockquant_bank.errors import InvalidRequest
from stockquant_bank.rank import rank_products


def nav_series(pattern, start=date(2024, 1, 2), days=320):
    rows = []
    level = 1.0
    current = start
    made = 0
    i = 0
    while made < days:
        if current.weekday() < 5:
            level *= 1.0 + pattern(i)
            rows.append({"date": current.isoformat(), "value": round(level, 6)})
            made += 1
            i += 1
        current += timedelta(days=1)
    return rows


def product(pid, *, risk="R3", nav, fee=30, term=0, settle=1, minimum=100):
    return {
        "product_id": pid,
        "product_name": pid,
        "official_risk_level": risk,
        "total_fee_bps": fee,
        "term_days": term,
        "settlement_days": settle,
        "min_amount_minor": minimum,
        "nav": nav,
    }


LOW_VOL = nav_series(lambda i: 0.00005 + (0.0005 if i % 2 else -0.0005))
HIGH_VOL = nav_series(lambda i: 0.0006 + (0.012 if i % 2 else -0.012))
SHORT = [{"date": f"2026-09-{1 + i:02d}", "value": 1.0 + i / 10000} for i in range(20)]


def user(**over):
    base = {
        "risk_level": "R3",
        "preference_mode": "stability",
        "horizon_days": 365,
        "liquidity_need_days": 30,
        "budget_minor": 1_000_000,
    }
    base.update(over)
    return base


def test_risk_level_hard_filter_cannot_be_outscored():
    result = rank_products(
        [product("SAFE", nav=LOW_VOL), product("HOT", risk="R5", nav=HIGH_VOL)],
        user=user(),
        as_of="2026-10-01",
    )
    assert [row["product_id"] for row in result["ranked"]] == ["SAFE"]
    codes = {reason["code"] for row in result["excluded"] for reason in row["reasons"]}
    assert "RISK_LEVEL_EXCEEDS_USER" in codes


def test_term_and_liquidity_filters():
    result = rank_products(
        [
            product("LOCKED", nav=LOW_VOL, term=1825),
            product("SLOW", nav=LOW_VOL, settle=5),
            product("OK", nav=LOW_VOL),
        ],
        user=user(horizon_days=365, liquidity_need_days=2),
        as_of="2026-10-01",
    )
    assert [row["product_id"] for row in result["ranked"]] == ["OK"]
    excluded = {row["product_id"]: [r["code"] for r in row["reasons"]] for row in result["excluded"]}
    assert "TERM_EXCEEDS_HORIZON" in excluded["LOCKED"]
    assert "LIQUIDITY_NOT_SETTLED_IN_TIME" in excluded["SLOW"]


def test_minimum_amount_filter_uses_minor_units():
    result = rank_products(
        [product("BIG", nav=LOW_VOL, minimum=5_000_000), product("SMALL", nav=LOW_VOL)],
        user=user(budget_minor=1_000_000),
        as_of="2026-10-01",
    )
    assert [row["product_id"] for row in result["ranked"]] == ["SMALL"]
    assert "BELOW_MIN_INVESTMENT" in [r["code"] for row in result["excluded"] for r in row["reasons"]]


def test_short_history_is_eligible_but_not_quant_ranked():
    result = rank_products(
        [product("NEW", nav=SHORT), product("OLD", nav=LOW_VOL)],
        user=user(),
        as_of="2026-10-01",
    )
    assert [row["product_id"] for row in result["ranked"]] == ["OLD"]
    assert result["not_ranked"][0]["product_id"] == "NEW"
    assert result["not_ranked"][0]["code"] == "INSUFFICIENT_NAV_HISTORY"
    # The A4 baseline still ranks it, so the ablation set stays comparable.
    assert "NEW" in {row["product_id"] for row in result["baseline_ranked"]}


def test_preference_changes_order_and_is_explained():
    products = [product("STEADY", nav=LOW_VOL), product("GROWTH", nav=HIGH_VOL)]
    stable = rank_products(products, user=user(preference_mode="stability"), as_of="2026-10-01")
    chasing = rank_products(products, user=user(preference_mode="return"), as_of="2026-10-01")
    assert stable["ranked"][0]["product_id"] == "STEADY"
    assert chasing["ranked"][0]["product_id"] == "GROWTH"
    top = chasing["ranked"][0]
    assert "cagr" in top["score_contributions"]
    assert chasing["weights_used"] != stable["weights_used"]


def test_ablation_is_available_without_second_run():
    result = rank_products(
        [product("CHEAP", nav=HIGH_VOL, fee=0), product("STEADY", nav=LOW_VOL, fee=120)],
        user=user(preference_mode="stability"),
        as_of="2026-10-01",
    )
    assert result["baseline_ranked"]
    changes = {row["product_id"]: row for row in result["rank_changes"]}
    assert set(changes) == {row["product_id"] for row in result["ranked"]}
    assert any(row["delta"] != 0 for row in result["rank_changes"])


def test_scores_do_not_depend_on_catalog_composition():
    one = rank_products([product("A", nav=LOW_VOL)], user=user(), as_of="2026-10-01")
    two = rank_products(
        [product("A", nav=LOW_VOL), product("B", nav=HIGH_VOL)],
        user=user(),
        as_of="2026-10-01",
    )
    score_one = next(row["score"] for row in one["ranked"] if row["product_id"] == "A")
    score_two = next(row["score"] for row in two["ranked"] if row["product_id"] == "A")
    assert score_one == score_two


def test_deterministic_for_same_input():
    products = [product("A", nav=LOW_VOL), product("B", nav=HIGH_VOL)]
    first = rank_products(products, user=user(), as_of="2026-10-01")
    second = rank_products(products, user=user(), as_of="2026-10-01")
    assert [r["product_id"] for r in first["ranked"]] == [r["product_id"] for r in second["ranked"]]
    assert [r["score"] for r in first["ranked"]] == [r["score"] for r in second["ranked"]]


def test_invalid_mode_and_risk_level_raise():
    with pytest.raises(InvalidRequest):
        rank_products([product("A", nav=LOW_VOL)], user=user(preference_mode="yolo"), as_of="2026-10-01")
    with pytest.raises(InvalidRequest):
        rank_products([product("A", nav=LOW_VOL)], user=user(risk_level="X9"), as_of="2026-10-01")


def test_disclaimers_state_no_forecast_and_scale_is_explicit():
    result = rank_products([product("A", nav=LOW_VOL)], user=user(), as_of="2026-10-01")
    assert "scoring_scales" in result
    assert any("不是收益预测" in line for line in [result["rank_basis"]] + result["disclaimers"])

def test_a4_mode_serves_the_baseline_order_and_keeps_short_history():
    result = rank_products(
        [product("NEW", nav=SHORT), product("OLD", nav=LOW_VOL)],
        user=user(),
        as_of="2026-10-01",
        include_quant=False,
    )
    assert result["include_quant"] is False
    assert {row["product_id"] for row in result["ranked"]} == {"NEW", "OLD"}
    assert all(row.get("quant_disabled") for row in result["ranked"])
    assert result["rank_changes"] == []
    assert [row["product_id"] for row in result["ranked"]] == [
        row["product_id"] for row in result["baseline_ranked"]
    ]
