"""Stress scenarios must be transparent and bounded by their assumptions."""
import pytest

from stockquant_bank.nav import validate_series
from stockquant_bank.stress import stress_product


def series(points=300, daily=0.0002):
    rows = []
    level = 1.0
    from datetime import date, timedelta
    current = date(2025, 1, 2)
    while len(rows) < points:
        if current.weekday() < 5:
            level *= 1.0 + daily
            rows.append({"date": current.isoformat(), "value": round(level, 6)})
        current += timedelta(days=1)
    return validate_series("P1", rows, as_of="2026-12-31")


def test_nav_shock_is_applied_linearly_and_stays_minor_units():
    out = stress_product(series(), [{"name": "drop10", "kind": "nav_shock", "shock": -0.10}])
    block = out["scenarios"][0]
    assert block["nav_after"] == pytest.approx(block["nav_before"] * 0.9, rel=1e-9)
    assert block["impact_on_10000_cny_minor"] == -100000
    assert block["assumptions"]
    assert "不是损失上限" in block["not_a_loss_limit"]


def test_historical_worst_window_reports_where_it_happened():
    from datetime import date, timedelta

    rows = []
    level = 1.0
    current = date(2025, 1, 2)
    i = 0
    while len(rows) < 300:
        if current.weekday() < 5:
            daily = -0.004 if i % 40 < 12 else 0.0006
            rows.append({"date": current.isoformat(), "value": round(level, 6)})
            level *= 1.0 + daily
            i += 1
        current += timedelta(days=1)
    dipped = validate_series("P3", rows, as_of="2026-12-31")
    out = stress_product(dipped, [{"name": "worst20", "kind": "historical_worst_window", "window": 20}])
    block = out["scenarios"][0]
    assert block["observed_from"] < block["observed_to"]
    assert block["shock"] < 0


def test_parametric_scenario_requires_enough_history():
    short = validate_series(
        "P2",
        [{"date": f"2025-01-{1 + i:02d}", "value": 1.0 + i / 10000} for i in range(30)],
        as_of="2026-12-31",
    )
    out = stress_product(short, [{"name": "vol2", "kind": "parametric_vol_shock"}])
    block = out["scenarios"][0]
    assert block["available"] is False
    assert block["na_reason"] == "insufficient_history_min_60_returns"


def test_default_scenarios_cover_shock_history_and_vol():
    out = stress_product(series())
    kinds = {block["kind"] for block in out["scenarios"]}
    assert kinds == {"nav_shock", "historical_worst_window", "parametric_vol_shock"}
    assert len(out["scenarios"]) == 3


def test_positive_shock_is_rejected():
    with pytest.raises(ValueError):
        stress_product(series(), [{"name": "up", "kind": "nav_shock", "shock": 0.05}])
    with pytest.raises(ValueError):
        stress_product(series(), [{"name": "weird", "kind": "nope"}])