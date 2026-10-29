"""Synthetic bank wealth catalog.

FinPilot's SRS requires a *mock bank*; there is no licensed fund NAV feed in the
competition environment.  This generator produces reproducible NAV series with
regime shifts, fat-tailed jumps and one deliberately short history, so the
service can be exercised end-to-end without pretending the data is real.

Every product carries ``nav_source = "synthetic_bank_catalog_v2"``; the report
echoes it.  Do not present these numbers as market evidence.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED = 20261028

PRODUCTS = [
    # id, name, issuer, type, risk, min_yuan, buy_fee_bps, redeem_fee_bps, term_days, settle_days, vol, mu, jump_prob, jump_size, points
    ("WM-CASH-001", "日日盈现金管理", "示例银行理财子公司", "现金管理类", "R1", 100, 0, 0, 0, 0, 0.0018, 0.018, 0.0, 0.0, None),
    ("WM-BOND-101", "稳健短债90天", "示例银行理财子公司", "短债", "R2", 1000, 10, 0, 90, 1, 0.0090, 0.031, 0.004, -0.004, None),
    ("WM-BOND-102", "纯债增强", "示例基金公司", "中长期纯债", "R2", 100, 20, 10, 0, 2, 0.0165, 0.038, 0.006, -0.007, None),
    ("WM-FIX-201", "固收+增强", "示例基金公司", "固收+", "R3", 100, 40, 20, 0, 2, 0.0380, 0.048, 0.010, -0.012, None),
    ("WM-MIX-301", "平衡配置混合", "示例基金公司", "混合型", "R3", 100, 60, 50, 0, 3, 0.0720, 0.055, 0.014, -0.018, None),
    ("WM-MIX-302", "进取配置混合", "示例基金公司", "混合型", "R4", 100, 60, 50, 0, 3, 0.1120, 0.068, 0.016, -0.022, None),
    ("WM-EQ-401", "沪深300指数增强", "示例基金公司", "股票指数", "R4", 100, 80, 50, 0, 3, 0.1850, 0.075, 0.020, -0.028, None),
    ("WM-EQ-402", "科技成长精选", "示例基金公司", "股票型", "R5", 100, 100, 50, 0, 4, 0.2850, 0.090, 0.026, -0.038, None),
    ("WM-QDII-501", "全球多元资产", "示例资管", "QDII", "R4", 1000, 80, 50, 0, 5, 0.1250, 0.065, 0.015, -0.020, None),
    ("WM-PEN-601", "养老目标2035", "示例基金公司", "养老FOF", "R3", 1000, 60, 0, 1825, 4, 0.0850, 0.058, 0.012, -0.016, None),
    ("WM-NEW-701", "新发红利精选", "示例基金公司", "股票型", "R4", 100, 80, 50, 0, 3, 0.1600, 0.070, 0.018, -0.024, 45),
    ("WM-FIX-202", "固收定开六个月", "示例银行理财子公司", "定开固收", "R3", 10000, 20, 0, 180, 2, 0.0260, 0.042, 0.008, -0.010, None),
]

START = date(2023, 1, 3)
END = date(2026, 10, 23)


def business_days(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def regime(date_value: date) -> tuple:
    """(mu multiplier, vol multiplier) by calendar regime."""
    if date_value < date(2024, 1, 15):
        return 1.0, 1.0
    if date_value < date(2024, 9, 20):          # 2024 drawdown
        return -1.4, 1.35
    if date_value < date(2025, 3, 1):           # recovery
        return 1.5, 1.15
    if date_value < date(2026, 2, 1):           # calm grind
        return 1.0, 0.95
    if date_value < date(2026, 7, 1):           # 2026 risk-off
        return -1.2, 1.25
    return 1.3, 1.05


def generate(product) -> list:
    (pid, _name, _issuer, _type, _risk, _min_yuan, _buy, _redeem,
     _term, _settle, vol, mu, jump_prob, jump_size, points) = product
    rng = random.Random(f"{SEED}:{pid}")
    days = [d for d in business_days(START, END)]
    if points:
        days = days[-points:]
    daily_mu = mu / 252.0
    daily_sigma = vol / math.sqrt(252.0)
    level = 1.0
    series = []
    for day in days:
        mu_mult, vol_mult = regime(day)
        if vol < 0.005:  # cash-like: never let a regime drawdown dominate
            mu_mult = max(mu_mult, 0.3)
            vol_mult = min(vol_mult, 1.2)
        shock = rng.gauss(0.0, 1.0)
        if rng.random() < jump_prob:
            shock += jump_size / daily_sigma * (1.0 if rng.random() < 0.35 else -1.0)
        daily = daily_mu * mu_mult + daily_sigma * vol_mult * shock
        daily = max(daily, -0.0003 if vol < 0.005 else -0.12)
        series.append((day.isoformat(), round(level, 6)))
        level *= (1.0 + daily)
    return series


def main() -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / "nav").mkdir(exist_ok=True)
    catalog = []
    for product in PRODUCTS:
        (pid, name, issuer, ptype, risk, min_yuan, buy, redeem, term, settle,
         vol, mu, _jp, _js, points) = product
        series = generate(product)
        with (DATA / "nav" / f"{pid}.csv").open("w", encoding="utf-8") as handle:
            handle.write("date,nav\n")
            for day, value in series:
                handle.write(f"{day},{value}\n")
        catalog.append({
            "product_id": pid,
            "product_name": name,
            "issuer": issuer,
            "product_type": ptype,
            "official_risk_level": risk,
            "min_amount_minor": int(min_yuan * 100),
            "purchase_fee_bps": buy,
            "redeem_fee_bps": redeem,
            "total_fee_bps": buy + redeem,
            "term_days": term,
            "settlement_days": settle,
            "open_days": "T+0" if settle == 0 else f"T+{settle}",
            "nav_file": f"data/nav/{pid}.csv",
            "nav_points": len(series),
            "nav_start": series[0][0],
            "nav_end": series[-1][0],
            "nav_source": "synthetic_bank_catalog_v2",
        })
    payload = {
        "catalog_version": "synthetic_bank_catalog_v2",
        "generated_by": "scripts/generate_synthetic_catalog.py",
        "seed": SEED,
        "as_of": END.isoformat(),
        "disclaimer": "合成数据，仅用于联调与消融管道；不是真实产品净值，不能作为收益或风险证据。",
        "products": catalog,
    }
    with (DATA / "products.json").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"wrote {len(catalog)} products")


if __name__ == "__main__":
    main()