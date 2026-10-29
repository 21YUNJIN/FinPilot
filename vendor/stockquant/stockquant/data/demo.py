from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List

from stockquant.data.csv_loader import write_csv


@dataclass(frozen=True)
class DemoSpec:
    symbol: str
    sector: str
    beta: float
    quality: float
    value: float
    initial_price: float
    market_cap: float


def _business_days(start: date, end: date) -> List[date]:
    cur = start
    out: List[date] = []
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def generate_demo_market(
    start: str = "2022-01-03",
    end: str = "2025-12-31",
    seed: int = 20261003,
) -> List[Dict[str, object]]:
    """Generate a deterministic market for smoke tests and demos.

    The data is synthetic. It is useful for validating the engine, not for
    making claims about real-world strategy performance.
    """
    rng = random.Random(seed)
    specs = [
        DemoSpec("000001.SZ", "Bank", 0.90, 0.75, 0.95, 12.0, 180e9),
        DemoSpec("000002.SZ", "RealEstate", 1.05, 0.35, 0.75, 15.0, 90e9),
        DemoSpec("000063.SZ", "Tech", 1.15, 0.65, 0.60, 28.0, 130e9),
        DemoSpec("000333.SZ", "Consumer", 0.85, 0.85, 0.70, 55.0, 300e9),
        DemoSpec("000651.SZ", "Consumer", 0.80, 0.88, 0.90, 32.0, 190e9),
        DemoSpec("000858.SZ", "Consumer", 0.95, 0.82, 0.85, 160.0, 520e9),
        DemoSpec("002415.SZ", "Tech", 1.05, 0.78, 0.50, 35.0, 280e9),
        DemoSpec("002594.SZ", "Auto", 1.20, 0.72, 0.40, 240.0, 650e9),
        DemoSpec("300015.SZ", "Healthcare", 1.00, 0.70, 0.45, 40.0, 160e9),
        DemoSpec("300059.SZ", "Broker", 1.25, 0.55, 0.70, 20.0, 250e9),
        DemoSpec("300124.SZ", "Auto", 1.05, 0.80, 0.55, 65.0, 210e9),
        DemoSpec("600000.SH", "Bank", 0.80, 0.70, 1.10, 8.0, 220e9),
        DemoSpec("600009.SH", "Transport", 0.90, 0.60, 0.75, 48.0, 100e9),
        DemoSpec("600036.SH", "Bank", 0.82, 0.90, 0.95, 38.0, 900e9),
        DemoSpec("600050.SH", "Telecom", 0.75, 0.72, 0.80, 5.0, 180e9),
        DemoSpec("600104.SH", "Auto", 0.95, 0.66, 0.75, 17.0, 180e9),
        DemoSpec("600276.SH", "Healthcare", 0.90, 0.92, 0.50, 45.0, 280e9),
        DemoSpec("600309.SH", "Materials", 1.00, 0.86, 0.60, 85.0, 260e9),
        DemoSpec("600519.SH", "Consumer", 0.88, 0.96, 0.65, 1700.0, 2100e9),
        DemoSpec("600585.SH", "Materials", 0.95, 0.74, 0.92, 32.0, 150e9),
        DemoSpec("600690.SH", "Consumer", 0.85, 0.82, 0.85, 26.0, 230e9),
        DemoSpec("600887.SH", "Consumer", 0.80, 0.88, 0.90, 28.0, 170e9),
        DemoSpec("601012.SH", "Materials", 1.18, 0.62, 0.42, 42.0, 320e9),
        DemoSpec("601888.SH", "Consumer", 1.08, 0.75, 0.50, 190.0, 350e9),
    ]
    days = _business_days(date.fromisoformat(start), date.fromisoformat(end))
    prices: Dict[str, float] = {s.symbol: s.initial_price for s in specs}
    sector_state: Dict[str, float] = {s.sector: 0.0 for s in specs}
    rows: List[Dict[str, object]] = []

    for i, day in enumerate(days):
        # Market regimes create trends, reversals, and stress periods.
        regime = i // 63
        market_drift = [0.00045, -0.00025, 0.00030, -0.00015, 0.00055, -0.00035][regime % 6]
        market_shock = rng.gauss(0.0, 0.0085 + 0.0030 * (regime % 3 == 1))
        for sector in sector_state:
            sector_state[sector] = 0.85 * sector_state[sector] + rng.gauss(0.0, 0.0035)

        quarter = (day.month - 1) // 3
        for spec in specs:
            prev_close = prices[spec.symbol]
            idio = rng.gauss(0.0, 0.018 - 0.004 * spec.quality)
            ret = (
                market_drift
                + spec.beta * market_shock
                + sector_state[spec.sector]
                + idio
                + 0.00015 * (spec.quality - 0.6)
            )
            ret = _clip(ret, -0.095, 0.095)
            new_close = max(0.5, prev_close * (1.0 + ret))
            open_ret = _clip(0.4 * ret + rng.gauss(0.0, 0.004), -0.07, 0.07)
            open_price = max(0.5, prev_close * (1.0 + open_ret))
            high = max(open_price, new_close) * (1.0 + abs(rng.gauss(0.0, 0.006)))
            low = min(open_price, new_close) * (1.0 - abs(rng.gauss(0.0, 0.006)))
            volume = max(10000.0, (1.0 + abs(ret) * 25.0) * rng.lognormvariate(14.0, 0.35))
            suspended = rng.random() < 0.0018 or (spec.quality < 0.5 and rng.random() < 0.003)
            if suspended:
                open_price = new_close = high = low = prev_close
                volume = 0.0

            pe_base = _clip(12.0 + 26.0 * (1.0 - spec.value) + 8.0 * (1.0 - spec.quality), 5.0, 70.0)
            pb_base = _clip(0.6 + 2.8 * (1.0 - spec.value) + 1.8 * spec.quality, 0.4, 10.0)
            roe_base = _clip(0.04 + 0.16 * spec.quality + rng.gauss(0.0, 0.015), 0.01, 0.30)
            market_cap = spec.market_cap * (new_close / spec.initial_price)
            rows.append(
                {
                    "date": day.isoformat(),
                    "symbol": spec.symbol,
                    "open": round(open_price, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "close": round(new_close, 4),
                    "volume": round(volume, 2),
                    "sector": spec.sector,
                    "market_cap": round(market_cap, 2),
                    "pe": round(pe_base * (new_close / spec.initial_price), 3),
                    "pb": round(pb_base * (new_close / spec.initial_price), 3),
                    "roe": round(roe_base + 0.002 * quarter, 5),
                    "suspended": str(suspended).lower(),
                    "limit_up": str(ret >= 0.094).lower(),
                    "limit_down": str(ret <= -0.094).lower(),
                    "is_st": str(spec.quality < 0.40 and day.year >= 2024).lower(),
                    "source": "synthetic-demo",
                    "raw_close": round(new_close, 4),
                    "eps": round(max(0.05, new_close / pe_base), 4),
                    "bps": round(max(0.1, new_close / pb_base), 4),
                    "revenue_growth": round(0.04 + 0.12 * spec.quality + rng.gauss(0.0, 0.02), 4),
                    "profit_growth": round(0.03 + 0.15 * spec.quality + rng.gauss(0.0, 0.03), 4),
                    "gross_margin": round(0.15 + 0.35 * spec.quality, 4),
                    "available_at": f"{day.isoformat()} 15:00:00",
                }
            )
            prices[spec.symbol] = new_close
    return rows


def write_demo_data(path: str, **kwargs: object) -> List[Dict[str, object]]:
    rows = generate_demo_market(**kwargs)
    write_csv(rows, path)
    return rows
