"""StockQuant 只读适配器测试（QUANT-01～04、T46／T50）。

覆盖：60 个收益可算波动、59 个不可算；< 252 个收益 CVaR 为 null 且有原因；
最大回撤数值可手算核对；样本不足／非净值产品 NA；同一数据两次计算一致；
``risk_rank_without_quant`` 与含量化排序在特定偏好下产生可解释差异。

注意：``add_product`` 必须先于 ``add_returns`` 调用（``product_navs`` 有外键）；
``add_product`` 内部会 flush，保证插入顺序满足外键约束。
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date, timedelta

import pytest

from finpilot import models
from finpilot.adapters.stockquant import ADAPTER_ID
from finpilot.adapters.stockquant import risk_report
from finpilot.adapters.stockquant.risk_report import (
    ANNUALIZATION_FACTOR,
    METHOD_VERSION,
    MIN_CVAR_POINTS,
    MIN_VOL_POINTS,
    risk_rank_without_quant,
)
from finpilot.domains.wealth_rules import compare
from finpilot.errors import FinPilotError
from finpilot.kernel.identity import AuthContext
from finpilot.timeutil import utcnow

USER = "user_demo_01"
ASSET_START = date(2025, 1, 1)


def ctx_for(user_id: str = USER) -> AuthContext:
    return AuthContext(user_id=user_id, roles=("customer",), channel="app", session_id="sess_quant_test")


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
        "liquidity_terms": "工作日可赎回",
        "cancel_terms": "",
        "cooling_off_days": 0,
        "settlement_days": 1,
        "history_ref": "",
        "meta": {},
        "is_active": True,
    }
    payload.update(overrides)
    product = models.WealthProduct(product_id=product_id, **payload)
    session.add(product)
    session.flush()  # product_navs 通过外键指向产品，模型没有 relationship
    return product


def add_returns(
    session,
    product_id: str,
    returns,
    *,
    start: date = ASSET_START,
    nav_start_e6: int = 1_000_000,
) -> list[float]:
    if session.get(models.WealthProduct, product_id) is None:
        raise AssertionError(f"必须先 add_product({product_id}) 再插入净值")
    nav = float(nav_start_e6)
    values: list[float] = []
    for index, value in enumerate(returns):
        day = start + timedelta(days=index)
        session.add(
            models.ProductNav(
                nav_id=f"nav_{product_id}_{index:04d}",
                product_id=product_id,
                as_of=day,
                nav_e6=int(round(nav)),
                daily_return_bps=float(value),
                terms_version="2026.10",
            )
        )
        values.append(float(value))
        nav *= 1.0 + float(value) / 10_000.0
    return values


def pattern(count: int) -> list[float]:
    """确定性伪随机日收益序列（纯 Python，范围 -100…100 bps）。"""
    return [float(((index * 37) % 201) - 100) for index in range(count)]


def add_profile(session, risk_level: str = "R3") -> models.RiskProfile:
    now = utcnow()
    profile = models.RiskProfile(
        owner_user_id=USER,
        questionnaire_version="2026.10",
        answers={},
        score=12,
        risk_level=risk_level,
        max_loss_tolerance="medium",
        assessed_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=300),
        version=1,
        is_active=True,
    )
    session.add(profile)
    return profile


# ---------------------------------------------------------------------------
# 波动
# ---------------------------------------------------------------------------


def test_annualized_volatility_uses_60_returns_and_factor_252(session, seed):
    add_product(session, "p_vol60")
    values = add_returns(session, "p_vol60", pattern(60))
    session.commit()

    report = risk_report(
        session,
        asset_ids=["p_vol60"],
        weights=[1.0],
        as_of=ASSET_START + timedelta(days=59),
        history_snapshot_id="snap_vol60",
    )
    asset = report["assets"][0]
    assert asset["applicable"] is True
    volatility = asset["volatility"]
    assert volatility["status"] == "ok"
    assert volatility["sample_count"] == 60 == MIN_VOL_POINTS
    assert volatility["annualization_factor"] == ANNUALIZATION_FACTOR == 252

    expected = statistics.stdev(values) * math.sqrt(252)
    assert volatility["value"] == pytest.approx(round(expected, 4), abs=1e-4)
    assert volatility["daily_stdev_bps"] == pytest.approx(round(statistics.stdev(values), 6))
    assert volatility["method"] == "sample_stdev_daily_return_bps * sqrt(252)"
    assert volatility["window"]["points"] == 60
    assert volatility["window"]["start"] == "2025-01-01"
    assert volatility["window"]["end"] == "2025-03-01"

    assert report["applicable"] is True
    assert report["portfolio"]["volatility"]["value"] == volatility["value"]
    assert report["method_version"] == METHOD_VERSION == "sq-risk-2026.10"
    assert report["read_only"] is True and report["side_effects"] == []
    assert report["source_hash"]


def test_annualized_volatility_is_null_with_59_returns(session, seed):
    add_product(session, "p_vol59")
    add_returns(session, "p_vol59", pattern(59))
    session.commit()

    report = risk_report(session, asset_ids=["p_vol59"], weights=[1.0], history_snapshot_id="snap59")
    volatility = report["assets"][0]["volatility"]
    assert volatility["value"] is None, "样本不足不得填零"
    assert volatility["status"] == "not_applicable"
    assert volatility["reason_code"] == "insufficient_history_for_volatility"
    assert volatility["required_sample_count"] == 60
    assert volatility["sample_count"] == 59
    assert "少于 60 个" in volatility["reason"]
    assert "insufficient_history_for_volatility" in {w["code"] for w in report["warnings"]}


# ---------------------------------------------------------------------------
# CVaR
# ---------------------------------------------------------------------------


def test_cvar_is_null_below_252_and_hand_checkable_at_300(session, seed):
    add_product(session, "p_cvar200")
    add_returns(session, "p_cvar200", pattern(200))
    add_product(session, "p_cvar300")
    long_values = add_returns(session, "p_cvar300", pattern(300))
    session.commit()

    short = risk_report(session, asset_ids=["p_cvar200"], weights=[1.0], history_snapshot_id="snap200")
    cvar_short = short["assets"][0]["cvar_95"]
    assert cvar_short["value"] is None, "少于 252 个有效收益不得输出正式 CVaR 比较值"
    assert cvar_short["status"] == "not_applicable"
    assert cvar_short["reason_code"] == "insufficient_history_for_cvar"
    assert cvar_short["required_sample_count"] == MIN_CVAR_POINTS == 252
    assert cvar_short["sample_count"] == 200
    assert "252" in cvar_short["reason"]

    long = risk_report(session, asset_ids=["p_cvar300"], weights=[1.0], history_snapshot_id="snap300")
    cvar_long = long["assets"][0]["cvar_95"]
    ordered = sorted(long_values)
    tail_count = max(1, int(len(ordered) * 0.05))
    assert cvar_long["status"] == "ok"
    assert cvar_long["tail_count"] == tail_count == 15
    assert cvar_long["value"] == pytest.approx(round(statistics.fmean(ordered[:tail_count]), 4))
    assert cvar_long["threshold_bps"] == pytest.approx(round(ordered[tail_count - 1], 4))
    assert cvar_long["confidence"] == 0.95
    assert cvar_long["sample_count"] == 300
    assert "不是损失上限" in cvar_long["note"]


# ---------------------------------------------------------------------------
# 最大回撤（手算核对）
# ---------------------------------------------------------------------------


def test_max_drawdown_is_hand_computable(session, seed):
    # 日收益 0 / -1000 / -1000 / +500 bps
    # 累计净值 1 → 0.9 → 0.81 → 0.8505
    # 回撤 0 → -10% → -19% → -14.95%  ⇒ 最大回撤 -19% = -1900 bps
    add_product(session, "p_dd")
    add_returns(session, "p_dd", [0.0, -1000.0, -1000.0, 500.0])
    session.commit()

    report = risk_report(
        session,
        asset_ids=["p_dd"],
        weights=[1.0],
        as_of=ASSET_START + timedelta(days=3),
        history_snapshot_id="snap_dd",
    )
    drawdown = report["assets"][0]["max_drawdown"]
    assert drawdown["status"] == "ok"
    assert drawdown["value"] == pytest.approx(-1900.0, abs=1e-6)
    assert drawdown["magnitude_bps"] == pytest.approx(1900.0, abs=1e-6)
    assert drawdown["unit"] == "bps"
    assert "负值表示回撤" in drawdown["sign_convention"]
    # 必须注明观察窗口
    assert drawdown["window"]["observation"] == "全部观察窗口（as_of 及之前的所有有效日收益）"
    assert drawdown["window"]["start"] == "2025-01-01"
    assert drawdown["window"]["end"] == "2025-01-04"
    assert drawdown["sample_count"] == 4
    assert "不是未来损失上限" in drawdown["note"]

    # 短窗口仍然给出回撤，但必须提示解释力有限
    assert "short_window_for_drawdown" in {w["code"] for w in report["warnings"]}
    assert report["assets"][0]["volatility"]["status"] == "not_applicable"


def test_drawdown_requires_at_least_two_points(session, seed):
    add_product(session, "p_dd1")
    add_returns(session, "p_dd1", [100.0])
    session.commit()
    report = risk_report(session, asset_ids=["p_dd1"], weights=[1.0], history_snapshot_id="snap_dd1")
    drawdown = report["assets"][0]["max_drawdown"]
    assert drawdown["value"] is None
    assert drawdown["reason_code"] == "insufficient_history_for_drawdown"


# ---------------------------------------------------------------------------
# 不适用的资产
# ---------------------------------------------------------------------------


def test_deposit_and_non_nav_products_are_not_applicable(session, seed):
    add_product(session, "p_deposit", product_type="structured_deposit")
    add_returns(session, "p_deposit", pattern(300))
    add_product(session, "p_empty", product_type="fixed_income")
    session.commit()

    report = risk_report(
        session,
        asset_ids=["p_deposit", "p_empty"],
        weights=[0.5, 0.5],
        history_snapshot_id="snap_nonnav",
    )
    deposit, empty = report["assets"]
    assert deposit["applicable"] is False
    assert deposit["reason_code"] == "non_nav_product"
    assert "不使用证券回测代替真实产品风险等级" in deposit["reason"]
    assert deposit["volatility"] is None and deposit["cvar_95"] is None

    assert empty["applicable"] is False
    assert empty["reason_code"] == "no_nav_history"

    assert report["applicable"] is False
    assert report["portfolio"]["applicable"] is False
    assert report["portfolio"]["reason_code"] == "no_applicable_assets"
    assert report["portfolio"]["volatility"] is None
    codes = {warning["code"] for warning in report["warnings"]}
    assert {"non_nav_product", "no_nav_history"} <= codes
    assert report["method_card"]["not_for"] == "不用于替代产品风险等级、适当性评估、流动性或条款判断"
    assert deposit["issuer_risk_level"] == "R2", "产品自带风险等级照原样回显，不被回测结果替代"


def test_unknown_asset_is_reported_not_silently_dropped(session, seed):
    add_product(session, "p_ok")
    add_returns(session, "p_ok", pattern(60))
    session.commit()
    report = risk_report(
        session, asset_ids=["p_ok", "p_missing"], weights=[1.0, 0.0], history_snapshot_id="snap_missing"
    )
    missing = next(asset for asset in report["assets"] if asset["asset_id"] == "p_missing")
    assert missing["applicable"] is False
    assert missing["reason_code"] == "asset_not_found"
    assert "asset_not_found" in {warning["code"] for warning in report["warnings"]}
    assert report["assets"][0]["applicable"] is True


# ---------------------------------------------------------------------------
# 组合、权重与可复现
# ---------------------------------------------------------------------------


def test_portfolio_weights_are_renormalized_over_applicable_assets(session, seed):
    add_product(session, "p_a")
    returns_a = add_returns(session, "p_a", pattern(300))
    add_product(session, "p_b")
    returns_b = add_returns(session, "p_b", pattern(300)[::-1])
    add_product(session, "p_c", product_type="time_deposit")
    add_returns(session, "p_c", pattern(300))
    session.commit()

    report = risk_report(
        session,
        asset_ids=["p_a", "p_b", "p_c"],
        weights=[0.5, 0.4, 0.1],
        as_of=ASSET_START + timedelta(days=299),
        history_snapshot_id="snap_portfolio",
    )
    weights_used = report["portfolio"]["weights_used"]
    assert set(weights_used) == {"p_a", "p_b"}
    assert weights_used["p_a"] == pytest.approx(0.5 / 0.9, abs=1e-9)
    assert weights_used["p_b"] == pytest.approx(0.4 / 0.9, abs=1e-9)
    assert sum(weights_used.values()) == pytest.approx(1.0)
    renormalize = next(w for w in report["warnings"] if w["code"] == "weights_renormalized")
    assert renormalize["excluded_assets"] == ["p_c"]
    assert renormalize["renormalized"] is True

    combined = [
        weights_used["p_a"] * returns_a[index] + weights_used["p_b"] * returns_b[index]
        for index in range(300)
    ]
    expected_vol = round(statistics.stdev(combined) * math.sqrt(252), 4)
    assert report["portfolio"]["volatility"]["value"] == pytest.approx(expected_vol, abs=1e-3)
    assert report["portfolio"]["alignment"] == "intersection_of_observation_dates"
    assert report["portfolio"]["method"] == "fixed_weight_daily_return_sum"
    assert report["portfolio"]["sample_count"] == 300


def test_weight_validation_and_method_version_pinning(session, seed):
    add_product(session, "p_w")
    add_returns(session, "p_w", pattern(60))
    session.commit()

    with pytest.raises(FinPilotError) as empty:
        risk_report(session, asset_ids=[], weights=None, history_snapshot_id="s")
    assert empty.value.code == "PARAM_INVALID"

    with pytest.raises(FinPilotError) as duplicate:
        risk_report(session, asset_ids=["p_w", "p_w"], weights=[0.5, 0.5], history_snapshot_id="s")
    assert duplicate.value.code == "PARAM_INVALID"

    with pytest.raises(FinPilotError) as not_normalized:
        risk_report(session, asset_ids=["p_w"], weights=[0.9], history_snapshot_id="s")
    assert not_normalized.value.code == "PARAM_INVALID"
    assert not_normalized.value.details["reason_codes"] == ["weights_not_normalized"]

    with pytest.raises(FinPilotError) as negative:
        risk_report(session, asset_ids=["p_w", "p_w2"], weights=[1.5, -0.5], history_snapshot_id="s")
    assert negative.value.code == "PARAM_INVALID"

    with pytest.raises(FinPilotError) as length:
        risk_report(session, asset_ids=["p_w"], weights=[0.5, 0.5], history_snapshot_id="s")
    assert length.value.code == "PARAM_INVALID"

    with pytest.raises(FinPilotError) as unknown_asset:
        risk_report(session, asset_ids=["p_w"], weights={"p_other": 1.0}, history_snapshot_id="s")
    assert unknown_asset.value.code == "PARAM_INVALID"

    with pytest.raises(FinPilotError) as method:
        risk_report(
            session, asset_ids=["p_w"], weights=[1.0], history_snapshot_id="s",
            method_version="sq-risk-2099.01",
        )
    assert method.value.code == "CAPABILITY_NOT_SUPPORTED"

    # 不给权重时等权，且历史快照缺失要提示
    equal = risk_report(session, asset_ids=["p_w"], weights=None)
    assert equal["weights"] == {"p_w": 1.0}
    assert "history_snapshot_id_missing" in {w["code"] for w in equal["warnings"]}


def test_same_data_gives_identical_report_and_source_hash(session, seed):
    add_product(session, "p_rep")
    add_returns(session, "p_rep", pattern(260))
    session.commit()

    kwargs = {
        "asset_ids": ["p_rep"],
        "weights": [1.0],
        "as_of": ASSET_START + timedelta(days=259),
        "history_snapshot_id": "snap_repeat",
    }
    first = risk_report(session, **kwargs)
    second = risk_report(session, **kwargs)

    assert first["source_hash"] == second["source_hash"], "同一数据同一方法必须得到同一 source_hash"
    first_copy = {key: value for key, value in first.items() if key != "computed_at"}
    second_copy = {key: value for key, value in second.items() if key != "computed_at"}
    assert first_copy == second_copy, "除 computed_at 外必须逐字段一致"
    assert first["computed_at"]

    # 数据窗口变化 → 哈希变化（可追溯，不是恒定值）
    earlier = risk_report(session, **{**kwargs, "as_of": ASSET_START + timedelta(days=258)})
    assert earlier["source_hash"] != first["source_hash"]
    assert earlier["assets"][0]["volatility"]["sample_count"] == 259

    # as_of 之后的净值不参与计算
    assert first["assets"][0]["sample_count"] == 260
    assert first["assets"][0]["window"]["end"] == (ASSET_START + timedelta(days=259)).isoformat()


# ---------------------------------------------------------------------------
# 排序影响与消融
# ---------------------------------------------------------------------------


def test_quant_metrics_change_ranking_explainably(session, seed):
    # p_growth 日收益均值约 +20 bps（年化后显著高于 p_stable 的 +2 bps），但费率更高
    add_product(session, "p_growth", fee_bps=50)
    add_returns(session, "p_growth", [float(15 + ((index * 7) % 11)) for index in range(300)])
    add_product(session, "p_stable", fee_bps=0)
    add_returns(session, "p_stable", [2.0] * 300)
    add_profile(session, risk_level="R3")
    session.commit()

    with_quant = compare.compare_products(
        session, ctx_for(), budget_minor=200_000, horizon_days=365, preferences=["yield", "fee"]
    )
    without_quant = risk_rank_without_quant(
        session, ctx_for(), budget_minor=200_000, horizon_days=365, preferences=["yield", "fee"]
    )

    def eligible(result):
        return [item["product_id"] for item in result if item["eligible"]]

    assert eligible(with_quant) == ["p_growth", "p_stable"], "含量化时按收益偏好排序"
    assert eligible(without_quant) == ["p_stable", "p_growth"], "去掉量化指标后回退到次优偏好（费率）"

    growth_with = next(item for item in with_quant if item["product_id"] == "p_growth")
    growth_without = next(item for item in without_quant if item["product_id"] == "p_growth")
    assert growth_with["history"]["history_metric"]["status"] == "ok"
    assert growth_with["history"]["history_metric"]["value"] > 0
    assert growth_with["data_version"]["quant_metrics_included"] is True

    assert growth_without["history"]["history_metric"]["value"] is None
    assert growth_without["history"]["history_metric"]["reason_code"] == "quant_metrics_removed"
    assert growth_without["data_version"]["quant_metrics_included"] is False
    codes = {reason["code"] for reason in growth_without["reasons"]}
    assert {"QUANT_METRICS_REMOVED_FOR_ABLATION", "PREFERENCE_YIELD_UNAVAILABLE"} <= codes
    # 硬约束与条款仍然生效，差异只来自量化指标
    assert growth_without["exclusion_reason_codes"] == growth_with["exclusion_reason_codes"] == []
    assert growth_without["required_holding_days"] == growth_with["required_holding_days"]
    assert growth_without["estimated_fee_minor"] == growth_with["estimated_fee_minor"]


def test_report_and_compare_output_carry_no_guarantee_wording(session, seed):
    add_product(session, "p_wording")
    add_returns(session, "p_wording", pattern(300))
    add_profile(session, risk_level="R3")
    session.commit()

    report = risk_report(session, asset_ids=["p_wording"], weights=[1.0], history_snapshot_id="snap_wording")
    payload = json.dumps(report, ensure_ascii=False)  # 不依赖 default=str：报告必须可直接进信封
    assert compare.ensure_no_guarantee_wording(report) == []
    for token in compare.FORBIDDEN_GUARANTEE_TOKENS:
        assert token not in payload, f"风险报告不得包含收益保证措辞：{token}"
    assert "不代表未来收益" in report["disclaimer"]
    assert report["method_card"]["cvar_confidence"] == 0.95
    assert report["method_card"]["annualization_factor"] == 252
    # This module is the explicit local baseline, not the remote StockQuant engine.
    assert ADAPTER_ID == "finpilot-local-reference"


def test_method_card_pins_frequency_and_thresholds(session, seed):
    """方法卡必须写死年化因子与门槛，避免不同资产频率混算。"""
    add_product(session, "p_card")
    add_returns(session, "p_card", pattern(60))
    session.commit()
    report = risk_report(session, asset_ids=["p_card"], weights=[1.0], history_snapshot_id="snap_card")
    card = report["method_card"]
    assert card["annualization_factor"] == 252
    assert card["min_vol_points"] == 60
    assert card["min_cvar_points"] == 252
    assert card["data_source"] == "ProductNav.daily_return_bps"
    assert card["alignment"] == "observation_date_intersection"
    assert card["frequency_policy"] == "非日频数据不与其他频率混算"
    assert card["volatility"] == "样本标准差（ddof=1）× √252"
    assert report["assets"][0]["window"]["median_gap_days"] == 1.0
