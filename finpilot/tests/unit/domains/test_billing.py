"""账单分析领域测试（S2-01～S2-04、T06／T07／T08／T43／T50）。

所有测试跑真实 PostgreSQL（``TEST_DATABASE_URL``），数据由测试自行插入，
手算期望写在断言旁。金额单位：整数分。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from finpilot import models
from finpilot.domains.billing import (
    CATEGORIES,
    bill_report,
    classify,
    correct_category,
    detect_anomalies,
    label_for,
    resolve_period,
)
from finpilot.domains.billing import anomalies as anomaly_rules
from finpilot.domains.billing.classification import CLASSIFIER_VERSION
from finpilot.domains.billing.service import REPORT_VERSION
from finpilot.errors import FinPilotError
from finpilot.kernel.identity import AuthContext
from finpilot.timeutil import SHANGHAI, UTC, business_day, utcnow

USER = "user_demo_01"
ACCOUNT = "account_salary"
ACCOUNT_2 = "account_savings"


def ctx_for(user_id: str = USER) -> AuthContext:
    return AuthContext(user_id=user_id, roles=("customer",), channel="app", session_id="sess_billing_test")


def sh(year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=SHANGHAI)


def add_tx(
    session,
    bt_id: str,
    posted_at: datetime,
    amount_minor: int,
    *,
    direction: str = "out",
    kind: str = "purchase",
    category: str = "other",
    merchant_name: str = "",
    counterparty: str = "",
    memo: str = "",
    linked_bt_id: str | None = None,
    account_id: str = ACCOUNT,
    anomaly_label: str | None = None,
    operation_id: str | None = None,
    owner_user_id: str = USER,
    business_day_override=None,
) -> models.BillingTransaction:
    tx = models.BillingTransaction(
        bt_id=bt_id,
        owner_user_id=owner_user_id,
        account_id=account_id,
        operation_id=operation_id,
        posted_at=posted_at.astimezone(UTC),
        business_day=business_day_override or business_day(posted_at),
        amount_minor=amount_minor,
        direction=direction,
        kind=kind,
        category=category,
        merchant_name=merchant_name,
        counterparty=counterparty,
        memo=memo,
        linked_bt_id=linked_bt_id,
        anomaly_label=anomaly_label,
        source="test",
    )
    session.add(tx)
    return tx


@pytest.fixture()
def october(session, seed):
    """跨年、跨月、含内部互转／资产调拨／手续费／跨期退款的两年流水。"""
    add_tx(session, "bt_oct_dining_1", sh(2026, 10, 5, 12), 8800, merchant_name="星巴克咖啡", memo="午餐")
    add_tx(
        session, "bt_oct_dining_2", sh(2026, 10, 6, 19, 30), 12000,
        category="dining", merchant_name="海底捞",
    )
    add_tx(
        session, "bt_oct_transport", sh(2026, 10, 7, 8, 15), 600,
        category="transport", merchant_name="地铁出行",
    )
    add_tx(
        session, "bt_oct_shopping", sh(2026, 10, 8, 21), 30000,
        category="shopping", merchant_name="京东商城",
    )
    add_tx(
        session, "bt_oct_internal_out", sh(2026, 10, 9, 10), 50000,
        kind="transfer_internal", merchant_name="本人转账", counterparty="储蓄卡",
    )
    add_tx(
        session, "bt_oct_internal_in", sh(2026, 10, 9, 10), 50000,
        direction="in", kind="transfer_internal", merchant_name="本人转账", account_id=ACCOUNT_2,
    )
    add_tx(
        session, "bt_oct_wealth_buy", sh(2026, 10, 10, 11), 100000,
        kind="wealth_purchase", category="finance", merchant_name="稳利理财",
    )
    add_tx(
        session, "bt_oct_wealth_redeem", sh(2026, 10, 20, 11), 25000,
        direction="in", kind="wealth_redeem", category="finance", merchant_name="稳利理财",
    )
    add_tx(
        session, "bt_oct_fee", sh(2026, 10, 11, 9), 200,
        kind="fee", category="finance", merchant_name="账户管理费",
    )
    add_tx(
        session, "bt_oct_refund_same", sh(2026, 10, 12, 15), 3000,
        direction="in", kind="refund", merchant_name="海底捞", linked_bt_id="bt_oct_dining_2",
    )
    add_tx(
        session, "bt_oct_refund_prior", sh(2026, 10, 14, 15), 5000,
        direction="in", kind="refund", merchant_name="京东商城", linked_bt_id="bt_sep_shopping",
    )
    add_tx(
        session, "bt_oct_refund_pending", sh(2026, 10, 18, 15), 1500,
        direction="in", kind="refund", merchant_name="未知商户",
    )
    add_tx(session, "bt_oct_salary", sh(2026, 10, 15, 9), 800000, direction="in", kind="salary")
    add_tx(
        session, "bt_oct_dup_1", sh(2026, 10, 21, 9), 2999,
        kind="subscription", merchant_name="某视频会员",
    )
    add_tx(
        session, "bt_oct_dup_2", sh(2026, 10, 21, 9, 40), 2999,
        kind="subscription", merchant_name="某视频会员",
    )
    add_tx(
        session, "bt_sep_shopping", sh(2026, 9, 25, 20), 15000,
        category="shopping", merchant_name="京东商城",
    )
    add_tx(
        session, "bt_sep_dining", sh(2026, 9, 20, 19), 20000,
        category="dining", merchant_name="海底捞",
    )
    add_tx(
        session, "bt_2025_oct_dining", sh(2025, 10, 5, 12), 10000,
        category="dining", merchant_name="海底捞",
    )
    add_tx(
        session, "bt_apr_2026_shopping", sh(2026, 4, 10, 12), 5000,
        category="shopping", merchant_name="京东商城",
    )
    session.commit()
    return {
        "oct_consumption_gross": 57398,  # 8800 + 12000 + 600 + 30000 + 2999 + 2999
        "oct_same_period_refund": 3000,
        "oct_prior_period_adjustment": 5000,
        "oct_net_consumption": 49398,  # 57398 - 3000 - 5000
        "oct_dining_net": 17800,  # (8800 + 12000) - 3000
        "oct_transport": 600,
        "oct_shopping": 30000,
        "oct_entertainment": 5998,  # 2999 × 2，商户名命中"视频/会员"
        "oct_inflow": 884500,  # 50000 + 25000 + 3000 + 5000 + 1500 + 800000
        "oct_outflow": 207598,  # 8800+12000+600+30000+50000+100000+200+2999+2999
        "oct_income": 800000,
        "oct_external_outflow": 57598,  # 207598 - 50000 - 100000
        "oct_fee": 200,
        "oct_refund_inflow": 9500,  # 3000 + 5000 + 1500
    }


def consumption_ids(report) -> set[str]:
    return set(report["consumption"]["total_consumption_gross_minor"]["source_ids"])


def category(report, name) -> dict:
    for item in report["consumption"]["categories"]:
        assert item["category"] in CATEGORIES
        if item["category"] == name:
            return item
    raise AssertionError(f"分类缺失: {name}")


# ---------------------------------------------------------------------------
# 账期与边界
# ---------------------------------------------------------------------------


def test_period_resolution_closed_open_boundaries():
    month = resolve_period({"kind": "month", "anchor": "2026-10"})
    assert month["start_business_day"].isoformat() == "2026-10-01"
    assert month["end_business_day_exclusive"].isoformat() == "2026-11-01"
    assert month["iso_start"] == "2026-10-01T00:00:00+08:00"
    assert month["iso_end"] == "2026-11-01T00:00:00+08:00"
    assert month["label"] == "2026年10月"
    assert month["previous_year"]["label"] == "2025年10月"

    december = resolve_period({"kind": "month", "anchor": "2026-12"})
    assert december["iso_end"] == "2027-01-01T00:00:00+08:00", "跨年月份必须顺延到次年 1 月 1 日"

    year = resolve_period({"kind": "year", "anchor": "2026"})
    assert year["iso_start"] == "2026-01-01T00:00:00+08:00"
    assert year["iso_end"] == "2027-01-01T00:00:00+08:00"

    custom = resolve_period(
        {"kind": "custom", "start": "2026-01-01T00:00:00+08:00", "end": "2026-02-01T00:00:00+08:00"}
    )
    assert custom["start_business_day"].isoformat() == "2026-01-01"
    assert custom["end_business_day_exclusive"].isoformat() == "2026-02-01"
    assert custom["previous_year"]["start_utc"].date().isoformat() == "2024-12-31", "上年同期按 UTC 瞬时前移一年"


@pytest.mark.parametrize(
    "period",
    [
        {"kind": "week", "anchor": "2026-10"},
        {"kind": "month", "anchor": "2026-13"},
        {"kind": "month", "anchor": "2026/10"},
        {"kind": "year", "anchor": "26"},
        {"kind": "custom", "start": "2026-01-01T00:00:00+08:00", "end": "2025-01-01T00:00:00+08:00"},
        {"kind": "custom", "start": "2026-01-01", "end": "2026-02-01T00:00:00+08:00"},
    ],
)
def test_period_rejects_invalid_input(period):
    with pytest.raises(FinPilotError) as excinfo:
        resolve_period(period)
    assert excinfo.value.code == "PARAM_INVALID"


def test_month_report_boundaries_use_shanghai_business_day(session, seed):
    add_tx(session, "bt_sep_last", sh(2026, 9, 30, 23, 59, 59), 4000)
    add_tx(session, "bt_oct_last", sh(2026, 10, 31, 23, 30), 1000)
    add_tx(session, "bt_nov_first", sh(2026, 11, 1, 0, 0), 2000)
    # UTC 2026-10-31T16:30Z = 上海 2026-11-01T00:30，必须归入 11 月
    add_tx(session, "bt_utc_rollover", datetime(2026, 10, 31, 16, 30, tzinfo=UTC), 8000)
    session.commit()

    october = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert consumption_ids(october) == {"bt_oct_last"}
    assert october["consumption"]["total_consumption_gross_minor"]["value"] == 1000
    assert october["period"]["start_business_day"] == "2026-10-01"
    assert october["period"]["boundary"] == "start_closed_end_open"

    november = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-11"})
    assert consumption_ids(november) == {"bt_nov_first", "bt_utc_rollover"}
    assert november["consumption"]["total_consumption_gross_minor"]["value"] == 10000

    september = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-09"})
    assert consumption_ids(september) == {"bt_sep_last"}

    year = bill_report(session, ctx_for(), period={"kind": "year", "anchor": "2026"})
    assert consumption_ids(year) == {"bt_sep_last", "bt_oct_last", "bt_nov_first", "bt_utc_rollover"}

    custom = bill_report(
        session,
        ctx_for(),
        period={"kind": "custom", "start": "2026-09-30T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00"},
    )
    assert consumption_ids(custom) == {"bt_sep_last"}

    mismatch = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert "business_day_mismatch" not in {w["code"] for w in mismatch["warnings"]}


def test_business_day_mismatch_is_reported_not_silently_rebucketed(session, seed):
    add_tx(
        session, "bt_wrong_day", sh(2026, 10, 5, 12), 1234,
        business_day_override=datetime(2026, 11, 5).date(),
    )
    session.commit()
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    codes = {warning["code"] for warning in report["warnings"]}
    assert "business_day_mismatch" in codes
    # 归期以 Asia/Shanghai 换算结果为准（10 月），但存储值不一致必须提示
    assert consumption_ids(report) == {"bt_wrong_day"}


# ---------------------------------------------------------------------------
# 月报数字与可追溯
# ---------------------------------------------------------------------------


def test_month_report_numbers_and_source_ids_are_traceable(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]

    assert report["report_version"] == REPORT_VERSION
    assert report["classifier_version"] == CLASSIFIER_VERSION
    assert report["timezone"] == "Asia/Shanghai"
    assert report["currency"] == "CNY"
    assert report["period_label"] == "2026年10月"

    gross = consumption["total_consumption_gross_minor"]
    assert gross["value"] == october["oct_consumption_gross"] == 57398
    assert set(gross["source_ids"]) == {
        "bt_oct_dining_1",
        "bt_oct_dining_2",
        "bt_oct_transport",
        "bt_oct_shopping",
        "bt_oct_dup_1",
        "bt_oct_dup_2",
    }
    assert "SUM(amount_minor)" in gross["formula"]

    assert consumption["same_period_refund_minor"]["value"] == october["oct_same_period_refund"]
    assert consumption["prior_period_refund_adjustment_minor"]["value"] == october["oct_prior_period_adjustment"]
    assert consumption["net_consumption_minor"]["value"] == october["oct_net_consumption"] == 49398
    assert consumption["net_consumption_minor"]["formula"] == (
        "total_consumption_gross - same_period_refund - prior_period_refund_adjustment"
    )

    assert category(report, "dining")["net_amount_minor"]["value"] == october["oct_dining_net"] == 17800
    assert category(report, "transport")["net_amount_minor"]["value"] == october["oct_transport"]
    assert category(report, "shopping")["net_amount_minor"]["value"] == october["oct_shopping"]
    assert category(report, "entertainment")["net_amount_minor"]["value"] == october["oct_entertainment"]

    # 分类合计 = 消费毛额 - 同期退款；百分比分母是消费毛额
    category_sum = sum(item["net_amount_minor"]["value"] for item in consumption["categories"])
    assert category_sum == october["oct_consumption_gross"] - october["oct_same_period_refund"] == 54398
    dining_share = category(report, "dining")["share_percent"]
    assert dining_share["value"] == round(17800 / 57398 * 100, 4)
    assert dining_share["status"] == "ok"
    assert dining_share["formula"] == "net_amount_minor['dining'] / total_consumption_gross * 100"

    # 每个汇总数字都带 source_ids 与 formula
    for key, metric in consumption.items():
        if isinstance(metric, dict) and "value" in metric and "unit" in metric:
            assert metric["formula"], f"{key} 缺少 formula"
            assert isinstance(metric["source_ids"], list), f"{key} 缺少 source_ids"
    assert set(report["source_ids"]) >= set(gross["source_ids"])
    assert report["transaction_count"] == 15
    # 报告必须能直接放进 HTTP 信封（不依赖 default=str 兜底）
    json.dumps(report, ensure_ascii=False)


def test_year_report_and_custom_range_totals(session, october):
    year = bill_report(session, ctx_for(), period={"kind": "year", "anchor": "2026"})
    # 2026 年消费 = 10 月 57398 + 9 月 15000 + 20000 + 4 月 5000
    assert year["consumption"]["total_consumption_gross_minor"]["value"] == 57398 + 35000 + 5000
    assert year["period_label"] == "2026年"
    assert year["consumption"]["year_over_year"]["previous_minor"]["value"] == 10000
    assert "bt_apr_2026_shopping" in set(year["source_ids"])
    assert "bt_2025_oct_dining" not in set(year["source_ids"])

    custom = bill_report(
        session,
        ctx_for(),
        period={"kind": "custom", "start": "2026-01-01T00:00:00+08:00", "end": "2026-02-01T00:00:00+08:00"},
    )
    assert custom["consumption"]["total_consumption_gross_minor"]["value"] == 0
    assert custom["empty"] is True
    assert custom["period"]["kind"] == "custom"


def test_empty_month_and_zero_denominator_are_not_applicable(session, seed):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2025-03"})
    consumption = report["consumption"]
    assert report["empty"] is True
    assert consumption["total_consumption_gross_minor"]["value"] == 0
    assert consumption["net_consumption_minor"]["value"] == 0
    for item in consumption["categories"]:
        assert item["share_percent"]["value"] is None
        assert item["share_percent"]["status"] == "not_applicable"
        assert item["share_percent"]["reason_code"] == "zero_denominator"
    assert consumption["year_over_year"]["status"] == "missing_data"
    assert consumption["year_over_year"]["change_percent"]["value"] is None
    assert consumption["year_over_year"]["change_percent"]["status"] == "missing_data"
    codes = {warning["code"] for warning in report["warnings"]}
    assert {"empty_period", "missing_data"} <= codes
    # 现金流口径同样不返回无穷大
    assert report["cashflow"]["inflow_total_minor"]["value"] == 0
    assert report["cashflow"]["by_kind"] == []


def test_year_over_year_missing_data_when_previous_year_absent(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-04"})
    yoy = report["consumption"]["year_over_year"]
    assert report["consumption"]["total_consumption_gross_minor"]["value"] == 5000
    assert yoy["status"] == "missing_data"
    assert yoy["change_minor"]["value"] is None
    assert yoy["change_minor"]["status"] == "missing_data"
    assert yoy["previous_minor"]["reason_code"] == "missing_data"
    assert "missing_data" in {warning["code"] for warning in report["warnings"]}


# ---------------------------------------------------------------------------
# 口径：互转 / 资产调拨 / 手续费 / 退款
# ---------------------------------------------------------------------------


def test_internal_transfer_visible_in_cashflow_but_excluded_from_consumption(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]
    cashflow = report["cashflow"]

    assert consumption["excluded_from_consumption"]["internal_transfer_out_minor"]["value"] == 50000
    assert "bt_oct_internal_out" not in consumption_ids(report)
    assert "bt_oct_internal_in" not in consumption_ids(report)

    assert cashflow["internal_transfers"]["out_minor"]["value"] == 50000
    assert cashflow["internal_transfers"]["in_minor"]["value"] == 50000
    assert set(cashflow["internal_transfers"]["out_minor"]["source_ids"]) == {"bt_oct_internal_out"}
    assert cashflow["inflow_total_minor"]["value"] == october["oct_inflow"]
    assert cashflow["outflow_total_minor"]["value"] == october["oct_outflow"]
    assert cashflow["income_minor"]["value"] == october["oct_income"] == 800000
    assert cashflow["external_outflow_minor"]["value"] == october["oct_external_outflow"] == 57598
    assert cashflow["consumption_outflow_minor"]["value"] == october["oct_consumption_gross"]
    assert cashflow["net_cashflow_minor"]["value"] == october["oct_inflow"] - october["oct_outflow"]

    # 现金流分类分组合计必须等于总流出，避免漏项
    assert sum(item["amount_minor"]["value"] for item in cashflow["by_category"]) == october["oct_outflow"]
    inflow_by_kind = sum(
        item["amount_minor"]["value"] for item in cashflow["by_kind"] if item["direction"] == "in"
    )
    assert inflow_by_kind == october["oct_inflow"]


def test_wealth_purchase_and_redeem_are_asset_allocation(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]
    cashflow = report["cashflow"]

    assert "bt_oct_wealth_buy" not in consumption_ids(report)
    assert "bt_oct_wealth_redeem" not in consumption_ids(report)
    assert consumption["excluded_from_consumption"]["asset_allocation_out_minor"]["value"] == 100000

    assert cashflow["asset_allocation"]["out_minor"]["value"] == 100000
    assert cashflow["asset_allocation"]["in_minor"]["value"] == 25000
    assert "不算消费" in cashflow["asset_allocation"]["note"]
    # 赎回不是收入
    assert cashflow["income_minor"]["value"] == 800000
    assert "bt_oct_wealth_redeem" not in set(cashflow["income_minor"]["source_ids"])

    classification = classify({"kind": "wealth_purchase", "category": "other"})
    assert classification == ("finance", "rule.kind.asset_allocation", 1.0)


def test_fee_is_listed_separately(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]
    cashflow = report["cashflow"]

    assert consumption["fee_minor"]["value"] == october["oct_fee"] == 200
    assert consumption["fee_minor"]["source_ids"] == ["bt_oct_fee"]
    assert cashflow["fee_outflow_minor"]["value"] == 200
    # 手续费不计入任何消费分类
    assert "bt_oct_fee" not in consumption_ids(report)
    assert category(report, "finance")["amount_minor"]["value"] == 0
    # 但它是真实流出，必须留在现金流总额里
    assert october["oct_external_outflow"] == october["oct_consumption_gross"] + october["oct_fee"]


def test_cross_period_refund_keeps_two_bases_separate(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]
    cashflow = report["cashflow"]

    adjustments = consumption["prior_period_refund_adjustments"]
    assert [item["bt_id"] for item in adjustments] == ["bt_oct_refund_prior"]
    assert adjustments[0]["origin_bt_id"] == "bt_sep_shopping"
    assert adjustments[0]["origin_business_day"] == "2026-09-25"
    assert adjustments[0]["scope"] == "prior_period"
    assert consumption["prior_period_refund_adjustment_minor"]["value"] == 5000
    assert "不静默改写" in consumption["prior_period_refund_adjustment_minor"]["note"]

    # 本期现金流记退款入账
    assert "bt_oct_refund_prior" in set(cashflow["refund_inflow_minor"]["source_ids"])
    assert cashflow["refund_inflow_minor"]["value"] == october["oct_refund_inflow"] == 9500

    # 上期已冻结的消费报告不被改写：9 月报表的消费仍是原始 15000
    september = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-09"})
    assert category(september, "shopping")["amount_minor"]["value"] == 15000
    assert september["consumption"]["same_period_refund_minor"]["value"] == 0
    assert september["consumption"]["prior_period_refund_adjustment_minor"]["value"] == 0


def test_same_period_refund_nets_category_without_touching_cashflow_total(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]
    same = consumption["same_period_refunds"]
    assert [item["bt_id"] for item in same] == ["bt_oct_refund_same"]
    assert same[0]["scope"] == "same_period"
    assert same[0]["category"] == "dining"
    assert category(report, "dining")["amount_minor"]["value"] == 20800
    assert category(report, "dining")["same_period_refund_minor"]["value"] == 3000
    assert category(report, "dining")["net_amount_minor"]["value"] == 17800
    assert consumption["year_over_year"]["current_minor"]["value"] == 57398, "同比用消费毛额，口径稳定"


def test_unlinked_refund_is_pending_verification(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    consumption = report["consumption"]
    pending = consumption["pending_verification_refunds"]
    assert [item["bt_id"] for item in pending] == ["bt_oct_refund_pending"]
    assert pending[0]["reason_code"] == "origin_not_linked"
    assert pending[0]["status"] == "pending_verification"
    assert "待核实" in pending[0]["note"]

    codes = {warning["code"] for warning in report["warnings"]}
    assert "unverified_refunds" in codes
    warning = next(w for w in report["warnings"] if w["code"] == "unverified_refunds")
    assert warning["source_ids"] == ["bt_oct_refund_pending"]

    # 待核实退款不影响任何消费合计
    assert consumption["same_period_refund_minor"]["value"] == 3000
    assert consumption["prior_period_refund_adjustment_minor"]["value"] == 5000
    assert consumption["net_consumption_minor"]["value"] == 49398


def test_refund_referencing_inaccessible_origin_is_pending(session, seed):
    add_tx(session, "bt_mine", sh(2026, 10, 5, 12), 1000)
    add_tx(
        session, "bt_refund_foreign", sh(2026, 10, 8, 12), 1000,
        direction="in", kind="refund", linked_bt_id="bt_someone_else",
    )
    add_tx(
        session, "bt_refund_alloc", sh(2026, 10, 9, 12), 1000,
        direction="in", kind="refund", linked_bt_id="bt_alloc_origin",
    )
    add_tx(
        session, "bt_alloc_origin", sh(2026, 9, 9, 12), 100000,
        kind="wealth_purchase", category="finance",
    )
    session.commit()
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    reasons = {item["bt_id"]: item["reason_code"] for item in report["consumption"]["pending_verification_refunds"]}
    assert reasons["bt_refund_foreign"] == "origin_not_found"
    assert reasons["bt_refund_alloc"] == "origin_not_consumption"


# ---------------------------------------------------------------------------
# 分类与纠正
# ---------------------------------------------------------------------------


def test_classifier_is_deterministic_and_versioned():
    tx = {"kind": "purchase", "category": "other", "merchant_name": "星巴克咖啡", "memo": "早餐"}
    first = classify(tx)
    assert first == ("dining", "rule.keyword.dining", 0.9)
    assert classify(tx) == first, "同一输入必须得到同一分类结果"

    assert classify({"kind": "purchase", "category": "transport", "merchant_name": "京东商城"}) == (
        "transport",
        "rule.stored.valid_category",
        1.0,
    )
    assert classify({"kind": "transfer_internal", "category": "other"}) == (
        "other",
        "rule.kind.internal_transfer",
        1.0,
    )
    assert classify({"kind": "purchase", "category": "other", "merchant_name": "某某"}) == (
        "other",
        "rule.fallback.other",
        0.3,
    )
    assert label_for("education") == "教育"
    assert set(CATEGORIES) == {
        "dining", "transport", "shopping", "housing", "entertainment",
        "medical", "education", "finance", "other",
    }


def test_correct_category_records_correction_without_mutating_transaction(session, october):
    before = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert category(before, "dining")["net_amount_minor"]["value"] == 17800
    assert category(before, "transport")["net_amount_minor"]["value"] == 600

    result = correct_category(session, ctx_for(), "bt_oct_transport", "dining", "这是商务餐费")
    assert result["from_category"] == "transport"
    assert result["to_category"] == "dining"
    assert result["transaction_category_unchanged"] is True

    row = session.get(models.BillingTransaction, "bt_oct_transport")
    session.refresh(row)
    assert row.category == "transport", "原流水分类不得被改写"
    corrections = session.query(models.CategoryCorrection).all()
    assert len(corrections) == 1
    assert corrections[0].bt_id == "bt_oct_transport"
    assert corrections[0].from_category == "transport"
    assert corrections[0].to_category == "dining"
    assert corrections[0].reason == "这是商务餐费"

    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert report["corrections_applied"] == [
        {
            "bt_id": "bt_oct_transport",
            "corrected": True,
            "to_category": "dining",
            "to_category_label": "餐饮",
            "base_category": "transport",
            "category_rule_id": "rule.correction.user",
            "transaction_category_unchanged": True,
        }
    ]
    dining = category(report, "dining")
    assert dining["net_amount_minor"]["value"] == 18400  # 17800 + 600
    assert "bt_oct_transport" in dining["amount_minor"]["source_ids"]
    assert category(report, "transport")["net_amount_minor"]["value"] == 0
    # 合计不受纠正影响：纠正只改归类，不造钱
    assert report["consumption"]["total_consumption_gross_minor"]["value"] == 57398


def test_correct_category_rejects_bad_input_and_foreign_transactions(session, october):
    with pytest.raises(FinPilotError) as bad_category:
        correct_category(session, ctx_for(), "bt_oct_transport", "luxury")
    assert bad_category.value.code == "PARAM_INVALID"
    assert bad_category.value.details["allowed"] == list(CATEGORIES)

    with pytest.raises(FinPilotError) as same:
        correct_category(session, ctx_for(), "bt_oct_transport", "transport")
    assert same.value.code == "PARAM_INVALID"

    add_tx(
        session, "bt_other_user", sh(2026, 10, 5, 12), 100,
        owner_user_id="user_demo_02", account_id="account_demo2",
    )
    session.commit()
    with pytest.raises(FinPilotError) as foreign:
        correct_category(session, ctx_for(), "bt_other_user", "dining")
    assert foreign.value.code == "OBJECT_NOT_ACCESSIBLE"

    with pytest.raises(FinPilotError) as missing:
        correct_category(session, ctx_for(), "bt_does_not_exist", "dining")
    assert missing.value.code == "OBJECT_NOT_ACCESSIBLE"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


def test_duplicate_same_amount_rule_threshold_and_basis(session, october):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    duplicates = [item for item in report["anomalies"] if item["type"] == "duplicate_same_amount"]
    assert len(duplicates) == 1
    anomaly = duplicates[0]
    assert anomaly["bt_ids"] == ["bt_oct_dup_1", "bt_oct_dup_2"]
    assert anomaly["threshold_version"] == anomaly_rules.THRESHOLD_VERSION == "bill-anomaly-2026.10"
    assert anomaly["basis"]["merchant"] == "某视频会员"
    assert anomaly["basis"]["amount_minor"] == 2999
    assert anomaly["basis"]["window_hours"] == 24
    assert anomaly["basis"]["min_count"] == 2
    assert "24 小时" in anomaly["explanation"]
    assert anomaly["is_fraud_conclusion"] is False
    assert anomaly["severity"] == "medium"

    assert report["anomaly_summary"]["count"] == 1
    assert report["anomaly_summary"]["by_type"] == {"duplicate_same_amount": 1}
    assert report["anomaly_summary"]["types"] == ["duplicate_same_amount"]
    assert report["anomaly_summary"]["threshold_version"] == anomaly_rules.THRESHOLD_VERSION
    assert report["anomaly_summary"]["is_fraud_conclusion"] is False
    assert report["anomaly_summary"]["changes_card_state"] is False
    assert report["anomaly_summary"]["cancels_transaction"] is False
    assert "不等于欺诈" in report["anomaly_summary"]["note"]

    # 阈值边界：24 小时内的第 2 笔命中，超过 24 小时不命中
    just_in = [
        {"bt_id": "a1", "posted_at": sh(2026, 10, 1, 0), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
        {"bt_id": "a2", "posted_at": sh(2026, 10, 1, 23, 59), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
    ]
    assert len(anomaly_rules.detect_duplicate_same_amount(just_in)) == 1
    just_out = [
        {"bt_id": "b1", "posted_at": sh(2026, 10, 1, 0), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
        {"bt_id": "b2", "posted_at": sh(2026, 10, 2, 0, 1), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
    ]
    assert anomaly_rules.detect_duplicate_same_amount(just_out) == []
    # 金额不同不命中
    different_amount = [
        {"bt_id": "c1", "posted_at": sh(2026, 10, 1, 0), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
        {"bt_id": "c2", "posted_at": sh(2026, 10, 1, 1), "amount_minor": 101, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
    ]
    assert anomaly_rules.detect_duplicate_same_amount(different_amount) == []


def test_merchant_short_window_repeat_requires_three_in_two_hours(session, seed):
    for index, hour in enumerate((9, 10, 11), start=1):
        add_tx(session, f"bt_repeat_{index}", sh(2026, 10, 12, hour), 1000 + index * 10,
               merchant_name="连锁便利店")
    add_tx(session, "bt_repeat_late", sh(2026, 10, 12, 14), 1050, merchant_name="连锁便利店")
    session.commit()

    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    repeats = [item for item in report["anomalies"] if item["type"] == "merchant_short_window_repeat"]
    assert len(repeats) == 1
    assert repeats[0]["bt_ids"] == ["bt_repeat_1", "bt_repeat_2", "bt_repeat_3"]
    assert repeats[0]["basis"]["min_count"] == 3
    assert repeats[0]["basis"]["window_hours"] == 2
    assert repeats[0]["basis"]["total_minor"] == 1010 + 1020 + 1030 == 3060
    assert "bt_repeat_late" not in repeats[0]["bt_ids"]

    two_only = [
        {"bt_id": "d1", "posted_at": sh(2026, 10, 1, 9), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
        {"bt_id": "d2", "posted_at": sh(2026, 10, 1, 10), "amount_minor": 120, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
    ]
    assert anomaly_rules.detect_merchant_short_window_repeat(two_only) == []


def test_history_deviation_uses_median_and_three_mad(session, seed):
    history_amounts = [9000, 9500, 10000, 10000, 10500, 11000, 10000, 10000, 9800, 10200]
    for index, amount in enumerate(history_amounts):
        add_tx(
            session, f"bt_hist_{index}", sh(2026, 6, 5 + index, 12), amount,
            category="shopping", merchant_name="京东商城",
        )
    add_tx(session, "bt_oct_normal", sh(2026, 10, 6, 12), 10500, category="shopping", merchant_name="京东商城")
    add_tx(session, "bt_oct_spike", sh(2026, 10, 7, 12), 200000, category="shopping", merchant_name="京东商城")
    session.commit()

    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    deviations = [item for item in report["anomalies"] if item["type"] == "deviation_from_personal_history"]
    assert len(deviations) == 1
    deviation = deviations[0]
    assert deviation["bt_ids"] == ["bt_oct_spike"]
    assert deviation["basis"]["sample_count"] == 10
    assert deviation["basis"]["median_minor"] == 10000
    assert deviation["basis"]["mad_minor"] == 200
    assert deviation["basis"]["threshold_kind"] == "median_plus_minus_3_mad"
    assert deviation["basis"]["limit_minor"] == pytest.approx(3 * 1.4826 * 200)
    assert deviation["threshold_version"] == anomaly_rules.THRESHOLD_VERSION
    assert "MAD" in deviation["explanation"]

    # 样本不足时不出结论
    sparse = [
        {"bt_id": "e1", "posted_at": sh(2026, 1, 1, 12), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "category": "shopping", "merchant_name": "M"},
    ]
    spike = [
        {"bt_id": "e2", "posted_at": sh(2026, 10, 1, 12), "amount_minor": 999999, "direction": "out",
         "kind": "purchase", "category": "shopping", "merchant_name": "M"},
    ]
    assert anomaly_rules.detect_history_deviation(spike, sparse) == []
    assert len(history_amounts) >= anomaly_rules.MIN_HISTORY_SAMPLES


def test_anomalies_never_touch_card_state_or_claim_fraud(session, october):
    card = models.Card(
        card_id="card_test_1",
        owner_user_id=USER,
        account_id=ACCOUNT,
        lifecycle_status="active",
        credit_limit_minor=1_000_000,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(card)
    session.commit()

    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert report["anomalies"], "本数据集应至少命中重复同额扣费"

    session.refresh(card)
    assert card.lifecycle_status == "active", "异常提示不得改变卡片状态"
    assert session.query(models.CardControl).count() == 0, "异常提示不得新增卡控"
    assert session.query(models.BillingTransaction).filter_by(bt_id="bt_oct_dup_1").one().amount_minor == 2999


def test_anomaly_labeled_evaluation_reports_tp_fp_fn(session, seed):
    add_tx(session, "bt_lab_dup_1", sh(2026, 10, 21, 9), 2999, merchant_name="某视频会员",
           anomaly_label="duplicate_charge")
    add_tx(session, "bt_lab_dup_2", sh(2026, 10, 21, 9, 40), 2999, merchant_name="某视频会员")
    add_tx(session, "bt_lab_normal", sh(2026, 10, 22, 9), 1000, merchant_name="地铁出行",
           anomaly_label="unusual_merchant")
    session.commit()

    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    evaluation = report["anomaly_summary"]["labeled_evaluation"]
    assert evaluation["status"] == "ok"
    assert evaluation["label_source"] == "synthetic"
    assert (evaluation["tp"], evaluation["fp"], evaluation["fn"]) == (1, 1, 1)
    assert evaluation["precision"] == 0.5
    assert evaluation["recall"] == 0.5

    unlabeled = anomalies_only()
    assert unlabeled["status"] == "not_applicable"
    assert unlabeled["precision"] is None


def anomalies_only() -> dict:
    from finpilot.domains.billing.anomalies import evaluate_against_labels

    rows = [
        {"bt_id": "x1", "posted_at": sh(2026, 10, 1, 12), "amount_minor": 100, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
    ]
    return evaluate_against_labels(rows, [])


# ---------------------------------------------------------------------------
# 报告元信息、未完成交易与入参
# ---------------------------------------------------------------------------


def test_report_as_of_timezone_and_incomplete_transactions(session, seed):
    add_tx(session, "bt_pending_kind", sh(2026, 10, 6, 12), 500, kind="pending", merchant_name="待结算")
    session.add(
        models.Operation(
            operation_id="op_pending_1",
            owner_user_id=USER,
            tool="transfer.submit",
            typed_args={"amount_minor": 1000},
            parameters_hash="ph",
            idempotency_key="ik-1",
            request_hash="rh",
            status="pending",
            required_level="YELLOW",
            source_account_id=ACCOUNT,
            amount_minor=1000,
            fee_minor=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    add_tx(
        session, "bt_op_pending", sh(2026, 10, 7, 12), 1000,
        merchant_name="待对账商户", operation_id="op_pending_1",
    )
    session.commit()

    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert report["as_of"] == "2026-10-22T10:00:00+08:00", "as_of 取可注入时钟的当前时刻（上海时区 ISO）"
    assert report["data_through"] == "2026-10-07T12:00:00+08:00"
    assert report["timezone"] == "Asia/Shanghai"

    codes = {warning["code"] for warning in report["warnings"]}
    assert "incomplete_transactions" in codes
    warning = next(w for w in report["warnings"] if w["code"] == "incomplete_transactions")
    assert set(warning["source_ids"]) == {"bt_pending_kind", "bt_op_pending"}
    assert warning["operation_ids"] == ["op_pending_1"]
    pending = {item["bt_id"]: item["reason_code"] for item in report["pending_items"]["incomplete_transactions"]}
    assert pending["bt_pending_kind"] == "kind_not_final"
    assert pending["bt_op_pending"] == "operation_not_final"

    # 未完成交易仍按已记账金额参与合计，但必须单列
    assert report["consumption"]["total_consumption_gross_minor"]["value"] == 1500


@pytest.mark.parametrize("basis", ["consumption", "cashflow", "both"])
def test_basis_controls_sections(session, october, basis):
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"}, basis=basis)
    assert report["basis"] == basis
    assert (report["consumption"] is not None) is (basis in ("consumption", "both"))
    assert (report["cashflow"] is not None) is (basis in ("cashflow", "both"))
    assert report["period_label"] == "2026年10月"
    assert report["timezone"] == "Asia/Shanghai"
    assert isinstance(report["warnings"], list)


def test_bill_report_rejects_unknown_basis_and_foreign_user_scope(session, october):
    with pytest.raises(FinPilotError) as bad_basis:
        bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"}, basis="all")
    assert bad_basis.value.code == "PARAM_INVALID"

    operator = AuthContext(user_id=USER, roles=("operator",), channel="operator", session_id="sess_op")
    with pytest.raises(FinPilotError) as forbidden:
        bill_report(session, operator, period={"kind": "month", "anchor": "2026-10"})
    assert forbidden.value.code == "FORBIDDEN"


def test_report_is_owned_scoped(session, october):
    add_tx(
        session, "bt_demo2_oct", sh(2026, 10, 5, 12), 999999,
        owner_user_id="user_demo_02", account_id="account_demo2",
    )
    session.commit()
    report = bill_report(session, ctx_for(), period={"kind": "month", "anchor": "2026-10"})
    assert "bt_demo2_oct" not in set(report["source_ids"])
    assert report["consumption"]["total_consumption_gross_minor"]["value"] == 57398

    other = bill_report(session, ctx_for("user_demo_02"), period={"kind": "month", "anchor": "2026-10"})
    assert set(other["source_ids"]) == {"bt_demo2_oct"}
    assert other["consumption"]["total_consumption_gross_minor"]["value"] == 999999


def test_detect_anomalies_is_read_only_over_rows():
    """异常识别只吃行数据，不写库：同输入两次得到完全相同的结果。"""
    rows = [
        {"bt_id": "z1", "posted_at": sh(2026, 10, 1, 9), "amount_minor": 500, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
        {"bt_id": "z2", "posted_at": sh(2026, 10, 1, 10), "amount_minor": 500, "direction": "out",
         "kind": "purchase", "merchant_name": "M"},
    ]
    first = detect_anomalies(rows, history_rows=[], context_rows=[])
    second = detect_anomalies(rows, history_rows=[], context_rows=[])
    assert first == second
    assert len(first) == 1
    assert first[0]["bt_ids"] == ["z1", "z2"]
    assert "references_rows_outside_period" in first[0]


def test_tool_handler_bill_report_is_read_only_and_idempotent(session, october):
    from finpilot.domains.billing import TOOL_HANDLERS, bill_report_handler

    handler = TOOL_HANDLERS["bill.report"]
    assert handler is bill_report_handler
    before = session.query(models.BillingTransaction).count()

    args = {"tool": "bill.report", "typed_args": {"period": {"kind": "month", "anchor": "2026-09"}}}
    first = handler(session, ctx_for(), args)
    second = handler(session, ctx_for(), args)
    assert first["type"] == "bill_report"
    assert first["source"] == "billing"
    assert first["version"] == REPORT_VERSION
    assert isinstance(first["warnings"], list)
    assert first["data"]["period_label"] == "2026年9月", "必须读取 typed_args 里的账期"
    assert first["data"]["consumption"]["total_consumption_gross_minor"]["value"] == 35000
    assert first == second, "只读处理器必须幂等：同一入参得到同一结果"
    assert session.query(models.BillingTransaction).count() == before, "处理器不得写流水"

    # Operation ORM 对象同样支持；未给账期时默认当月
    operation = models.Operation(
        operation_id="op_bill_1",
        owner_user_id=USER,
        tool="bill.report",
        typed_args={"basis": "cashflow"},
        parameters_hash="ph",
        idempotency_key="ik-bill-1",
        request_hash="rh",
        status="succeeded",
        required_level="GREEN",
        amount_minor=0,
        fee_minor=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(operation)
    session.flush()
    from_object = handler(session, ctx_for(), operation)
    assert from_object["data"]["period"]["kind"] == "month"
    assert from_object["data"]["period"]["label"] == "2026年10月", "默认账期取可注入时钟的当月"
    assert from_object["data"]["cashflow"] is not None
    assert from_object["data"]["consumption"] is None