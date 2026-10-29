"""Forward paper test: the only true out-of-sample check.

A backtest answers "would this have worked on the past?"; a forward test answers
"does it work going forward?".  This module freezes a target portfolio with a
timestamp, then later measures the realised forward return of those targets
against an equal-weight benchmark, fully outside the backtest window.

State files live under ``data/state``:

* ``forward_freeze.json`` - the latest frozen targets + freeze date.
* ``forward_results.jsonl`` - one evaluation line per completed horizon.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from stockquant.domain import MarketData

STATE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "state"


def freeze_targets(targets: Dict[str, float], as_of: str, path: Optional[str] = None) -> Dict[str, object]:
    """Record the model's target weights at a point in time."""
    p = Path(path) if path else STATE_DIR / "forward_freeze.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "targets": {s: float(w) for s, w in targets.items() if float(w) > 0},
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _forward_return(market: MarketData, symbol: str, start: str, end: str) -> Optional[float]:
    a = market.bar(symbol, start)
    b = market.bar(symbol, end)
    if a is None or b is None or a.close <= 0 or b.close <= 0 or a.suspended or b.suspended:
        return None
    return b.close / a.close - 1.0


def evaluate_frozen(
    market: MarketData,
    path: Optional[str] = None,
    results_path: Optional[str] = None,
) -> Dict[str, object]:
    """Measure the realised forward return of the frozen targets.

    Returns ``{"available": False}`` if nothing is frozen.  Otherwise returns the
    forward return from the frozen date to the latest market date for the frozen
    names, the equal-weight benchmark over the same window, and the excess.
    """
    p = Path(path) if path else STATE_DIR / "forward_freeze.json"
    if not p.exists():
        return {"available": False}
    try:
        frozen = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    start = str(frozen.get("as_of") or "")
    targets = {s: float(w) for s, w in (frozen.get("targets") or {}).items()}
    if not start or not targets:
        return {"available": False}
    end = market.dates[-1] if market.dates else start
    if end <= start:
        return {"available": False, "reason": "no forward data yet", "as_of": start}

    rets = []
    for sym in targets:
        r = _forward_return(market, sym, start, end)
        if r is not None:
            rets.append(r)
    bench = []
    prev = market.snapshot(start)
    cur = market.snapshot(end)
    for sym, a in prev.items():
        b = cur.get(sym)
        if b and a.close > 0 and b.close > 0 and not a.suspended and not b.suspended:
            bench.append(b.close / a.close - 1.0)
    port = sum(rets) / len(rets) if rets else 0.0
    bench_ret = sum(bench) / len(bench) if bench else 0.0
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    out = {
        "available": True,
        "as_of": start,
        "evaluated_at": end,
        "days": days,
        "n_targets": len(targets),
        "n_priced": len(rets),
        "portfolio_return": port,
        "benchmark_return": bench_ret,
        "excess_return": port - bench_ret,
    }
    rp = Path(results_path) if results_path else STATE_DIR / "forward_results.jsonl"
    rp.parent.mkdir(parents=True, exist_ok=True)
    with rp.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
    return out
