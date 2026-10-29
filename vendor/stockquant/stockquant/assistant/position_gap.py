"""Compare the user's real holdings against the model's target portfolio.

Answers the only question that is directly actionable: "given what I own and
what the model wants, what should I add, trim, exit, or start?"

Weights use two different bases on purpose and both are reported explicitly:

* current weight  = position market value / (user cash + holdings value)
* target weight   = the model's portfolio weight (sums to ~1 - cash buffer)

They are not forced onto the same base; the gap is a *direction and magnitude*
signal, and ``min_gap`` filters out noise.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional


def _lot_round(shares: float, lot: int = 100) -> int:
    if shares <= 0:
        return 0
    return int(shares // lot) * lot


def compute_position_gap(
    holdings: List[Dict[str, object]],
    target_weights: Dict[str, float],
    cash: float = 0.0,
    lot_size: int = 100,
    min_gap: float = 0.005,
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """Return per-symbol current-vs-target rows plus a summary."""
    holdings = [h for h in (holdings or []) if str(h.get("symbol"))]
    total = float(cash or 0.0) + sum(float(h.get("market_value", 0.0) or 0.0) for h in holdings)
    if total <= 0:
        total = sum(float(h.get("market_value", 0.0) or 0.0) for h in holdings)

    current: Dict[str, Dict[str, object]] = {}
    for h in holdings:
        symbol = str(h.get("symbol"))
        value = float(h.get("market_value", 0.0) or 0.0)
        current[symbol] = {
            "symbol": symbol,
            "shares": int(h.get("shares", 0) or 0),
            "price": float(h.get("price", 0.0) or 0.0),
            "market_value": value,
            "current_weight": (value / total) if total > 0 else 0.0,
        }

    rows: List[Dict[str, object]] = []
    summary = {"add": 0, "trim": 0, "exit": 0, "new": 0, "hold": 0}
    for symbol in set(current) | {str(s) for s, w in (target_weights or {}).items() if float(w or 0) > 0}:
        cur = current.get(symbol, {
            "symbol": symbol, "shares": 0, "price": 0.0, "market_value": 0.0, "current_weight": 0.0,
        })
        target_weight = float((target_weights or {}).get(symbol, 0.0) or 0.0)
        gap = target_weight - float(cur["current_weight"])
        price = float(cur["price"] or 0.0) or float((prices or {}).get(symbol, 0.0) or 0.0)
        if price <= 0:
            target_shares = 0
        else:
            target_shares = _lot_round((target_weight * total) / price, lot_size)
        held = int(cur["shares"] or 0)

        if held == 0 and target_weight > 0:
            action = "新建"
        elif target_weight <= 0 and held > 0:
            action = "清仓"
        elif gap > min_gap:
            action = "加仓"
        elif gap < -min_gap:
            action = "减仓"
        else:
            action = "持有"
        summary[{"新建": "new", "清仓": "exit", "加仓": "add", "减仓": "trim", "持有": "hold"}[action]] += 1

        note = ""
        if target_weight > 0 and target_shares == 0:
            note = "目标金额不足一手" if price > 0 else "缺少价格"
        elif action == "清仓":
            note = "模型目标中已无此股"

        rows.append({
            "symbol": symbol,
            "shares": held,
            "price": price,
            "market_value": float(cur["market_value"] or 0.0),
            "current_weight": float(cur["current_weight"] or 0.0),
            "target_weight": target_weight,
            "gap": gap,
            "action": action,
            "target_shares": target_shares,
            "shares_delta": target_shares - held,
            "delta_notional": (target_shares - held) * price,
            "note": note,
        })

    rows.sort(key=lambda r: abs(float(r["gap"])), reverse=True)
    return {
        "rows": rows,
        "summary": summary,
        "total_value": total,
        "holdings": len(current),
        "targets": len([w for w in (target_weights or {}).values() if float(w or 0) > 0]),
    }
