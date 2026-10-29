"""Transparent, first-order stress scenarios for a single NAV product.

Every scenario states its assumption and shock amplitude.  None of these
numbers is a loss ceiling or a forecast; they answer "if this happened, what
would the NAV look like", nothing more.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .metrics import TRADING_DAYS_PER_YEAR, annual_volatility
from .nav import NavSeries

NOT_A_LIMIT = "情景结果不是损失上限，也不代表发生概率。"


def _worst_window_return(values: Sequence[float], window: int) -> Optional[Dict[str, Any]]:
    if len(values) <= window:
        return None
    worst = None
    for start in range(0, len(values) - window):
        ret = values[start + window] / values[start] - 1.0
        if worst is None or ret < worst["return"]:
            worst = {"return": ret, "start_index": start, "end_index": start + window}
    return worst


def _shock_block(
    *,
    name: str,
    kind: str,
    shock: float,
    last_nav: float,
    assumptions: List[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stressed = last_nav * (1.0 + shock)
    block: Dict[str, Any] = {
        "scenario": name,
        "kind": kind,
        "assumptions": assumptions,
        "shock": round(shock, 10),
        "nav_before": round(last_nav, 6),
        "nav_after": round(max(0.0, stressed), 6),
        "impact_ratio": round(shock, 10),
        "impact_on_10000_cny_minor": int(round(shock * 1_000_000)),
        "not_a_loss_limit": NOT_A_LIMIT,
    }
    if extra:
        block.update(extra)
    return block


def stress_product(
    series: NavSeries,
    scenarios: Sequence[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    values = list(series.values)
    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    vol = annual_volatility(returns) if len(returns) >= 60 else None
    last_nav = values[-1]

    requested = list(scenarios or [])
    if not requested:
        requested = [
            {"name": "equity_shock_-10pct", "kind": "nav_shock", "shock": -0.10},
            {"name": "historical_worst_20d", "kind": "historical_worst_window", "window": 20},
            {"name": "vol_x2_20d_95pct", "kind": "parametric_vol_shock", "vol_multiplier": 2.0,
             "horizon_days": 20, "confidence": 0.95},
        ]

    results: List[Dict[str, Any]] = []
    for spec in requested:
        kind = str(spec.get("kind") or "")
        name = str(spec.get("name") or kind or "unnamed")
        if kind == "nav_shock":
            shock = float(spec.get("shock", 0.0))
            if shock > 0 or shock < -1:
                raise ValueError("nav_shock 必须在 [-1, 0] 之间")
            results.append(_shock_block(
                name=name, kind=kind, shock=shock, last_nav=last_nav,
                assumptions=[
                    "假设产品净值一次性下跌该比例，未建模费用、暂停赎回或分红。",
                    "不同资产类别的实际传导不同，本情景不区分股债相关性。",
                ],
            ))
        elif kind == "historical_worst_window":
            window = int(spec.get("window", 20))
            if window < 2 or window > 250:
                raise ValueError("historical_worst_window 的 window 必须在 2..250")
            worst = _worst_window_return(values, window)
            if worst is None:
                results.append({
                    "scenario": name, "kind": kind, "available": False,
                    "na_reason": "insufficient_history_for_window",
                    "window": window, "not_a_loss_limit": NOT_A_LIMIT,
                })
                continue
            start = series.dates[worst["start_index"]]
            end = series.dates[worst["end_index"]]
            results.append(_shock_block(
                name=name, kind=kind, shock=worst["return"], last_nav=last_nav,
                assumptions=[
                    f"取样本内最差的 {window} 个交易日累计跌幅，按一次性冲击重放。",
                    "这是样本内已发生的情形，不是对未来的预测。",
                ],
                extra={"window": window, "observed_from": start, "observed_to": end},
            ))
        elif kind == "parametric_vol_shock":
            multiplier = float(spec.get("vol_multiplier", 2.0))
            horizon = int(spec.get("horizon_days", 20))
            confidence = float(spec.get("confidence", 0.95))
            if vol is None:
                results.append({
                    "scenario": name, "kind": kind, "available": False,
                    "na_reason": "insufficient_history_min_60_returns",
                    "not_a_loss_limit": NOT_A_LIMIT,
                })
                continue
            if not (0.5 <= confidence < 1.0):
                raise ValueError("confidence 必须在 [0.5, 1) 之间")
            z = statistics.NormalDist().inv_cdf(1.0 - confidence)
            horizon_vol = vol * math.sqrt(horizon / TRADING_DAYS_PER_YEAR) * multiplier
            shock = z * horizon_vol
            results.append(_shock_block(
                name=name, kind=kind, shock=shock, last_nav=last_nav,
                assumptions=[
                    f"以样本年化波动 {round(vol, 6)} 乘 {multiplier} 倍，按正态假设推 {horizon} 个交易日的 {int(confidence*100)}% 分位跌幅。",
                    "真实收益厚尾且可能相关性上升，正态情景通常低估极端损失。",
                ],
                extra={"annual_volatility": round(vol, 8), "vol_multiplier": multiplier,
                       "horizon_days": horizon, "confidence": confidence},
            ))
        else:
            raise ValueError(f"未知压力情景类型: {kind}")

    return {
        "product_id": series.product_id,
        "as_of_end": series.end,
        "scenarios": results,
        "not_a_loss_limit": NOT_A_LIMIT,
    }