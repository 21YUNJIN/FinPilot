"""Report assembly, provenance and per-product isolation."""
from stockquant_bank.canonical import series_source_hash
from stockquant_bank.report import build_product_report, build_risk_report, engine_info


def make_product(pid="P1", nav=None, risk="R3"):
    return {
        "product_id": pid,
        "product_name": "测试产品",
        "official_risk_level": risk,
        "total_fee_bps": 30,
        "term_days": 0,
        "settlement_days": 2,
        "min_amount_minor": 100,
        "nav": nav or [{"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "value": 1.0 + i / 5000} for i in range(300)],
    }


def test_engine_reports_real_stockquant_kernel():
    info = engine_info()
    assert info["name"] == "stockquant"
    assert info["module"] == "stockquant.backtest.metrics"
    assert info["trading_days_per_year"] == 252


def test_provenance_hash_is_reproducible_and_matches_client_definition():
    product = make_product()
    report = build_product_report(product, as_of="2026-10-01")
    assert report["provenance"]["source_hash"] == series_source_hash(product["nav"])
    again = build_product_report(product, as_of="2026-10-01")
    assert again["provenance"]["source_hash"] == report["provenance"]["source_hash"]


def test_observed_band_mismatch_becomes_a_warning():
    product = make_product(risk="R2")
    report = build_product_report(product, as_of="2026-10-01")
    band = report["observed_market_risk_band"]["band"]
    assert band is not None
    if band != "R2":
        assert any("不一致" in warning for warning in report["warnings"])


def test_disclaimers_are_attached():
    report = build_product_report(make_product(), as_of="2026-10-01")
    joined = " ".join(report["disclaimers"])
    assert "不代表未来收益" in joined
    assert "不是损失上限" in joined


def test_one_bad_series_does_not_hide_the_others():
    good = make_product("GOOD")
    bad = make_product("BAD", nav=[{"date": "2026-01-02", "value": 0.0}])
    result = build_risk_report([good, bad], as_of="2026-10-01")
    assert result["summary"] == {"requested": 2, "computed": 1, "usable_for_ranking": 1, "failed": 1}
    assert result["failures"][0]["product_id"] == "BAD"
    assert result["failures"][0]["code"] == "UNPROCESSABLE_DATA"


def test_short_history_is_marked_not_usable():
    nav = [{"date": f"2026-09-{1 + i:02d}", "value": 1.0 + i / 10000} for i in range(30)]
    report = build_product_report(make_product(nav=nav), as_of="2026-10-01")
    assert report["usable_for_ranking"] is False
    assert report["metrics"]["annual_volatility"]["na_reason"] == "insufficient_history_min_60_returns"