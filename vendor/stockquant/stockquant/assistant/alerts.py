"""Alert engine: turn monitor output into actionable notifications.

Severity levels:

* ``critical``    - act now (stop-loss breached: "赶紧抛")
* ``warning``     - risk rising (model score turned negative: reduce)
* ``opportunity`` - a buy signal turned ready, or a take-profit level is hit

Alerts are de-duplicated by (symbol, kind, signal_date) so a 24/7 monitor does
not spam the same message on every cycle.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from stockquant.execution.filelock import file_lock

CRITICAL = "critical"
WARNING = "warning"
OPPORTUNITY = "opportunity"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alert_id(symbol: str, kind: str, signal_date: str) -> str:
    raw = f"{symbol}|{kind}|{signal_date}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_alerts(snapshot: Dict[str, object], entry_alerts: Optional[Dict[str, object]] = None) -> List[Dict[str, object]]:
    signal_date = str((snapshot or {}).get("signal_date") or "")
    alerts: List[Dict[str, object]] = []
    for row in (snapshot or {}).get("rows", []) or []:
        symbol = str(row.get("symbol"))
        action = str(row.get("action") or "")
        reason = str(row.get("reason") or "")
        pnl = float(row.get("pnl_pct") or 0.0)
        stop = row.get("stop_loss")
        if action == "止损/减仓":
            alerts.append({
                "id": _alert_id(symbol, "stop_loss", signal_date),
                "severity": CRITICAL,
                "kind": "stop_loss",
                "symbol": symbol,
                "signal_date": signal_date,
                "title": f"{symbol} 触发止损",
                "message": f"现价 {row.get('price')}，盈亏 {pnl*100:.1f}%，止损价 {stop}。- {reason}。建议尽快减仓/离场。",
            })
        elif action == "减仓":
            alerts.append({
                "id": _alert_id(symbol, "reduce", signal_date),
                "severity": WARNING,
                "kind": "reduce",
                "symbol": symbol,
                "signal_date": signal_date,
                "title": f"{symbol} 建议减仓",
                "message": f"模型分数转负（{row.get('score')}）。- {reason}。",
            })
        elif action == "止盈/分批减仓":
            alerts.append({
                "id": _alert_id(symbol, "take_profit", signal_date),
                "severity": OPPORTUNITY,
                "kind": "take_profit",
                "symbol": symbol,
                "signal_date": signal_date,
                "title": f"{symbol} 达到止盈线",
                "message": f"现价 {row.get('price')}，盈亏 {pnl*100:.1f}%，止盈价 {row.get('take_profit')}。可分批止盈。",
            })

    for row in (entry_alerts or {}).get("rows", []) or []:
        if str(row.get("status")) != "ready":
            continue
        symbol = str(row.get("symbol"))
        alerts.append({
            "id": _alert_id(symbol, "entry", signal_date),
            "severity": OPPORTUNITY,
            "kind": "entry",
            "symbol": symbol,
            "signal_date": signal_date,
            "title": f"{symbol} 出现可入信号",
            "message": f"现价 {row.get('current_price')}，买入区间 {row.get('entry_zone')}，止损 {row.get('stop_loss')}。- {row.get('reason','')}",
        })
    return alerts


class AlertStore:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "monitor_alerts.json"

    def load(self) -> List[Dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except Exception:
            return []

    def save(self, rows: List[Dict[str, object]]) -> None:
        self.path.write_text(json.dumps(rows[-200:], ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def append_new(self, alerts: List[Dict[str, object]]) -> List[Dict[str, object]]:
        """Add alerts that aren't already recorded; return the newly added ones."""
        with file_lock(self.path):
            existing = self.load()
            seen = {str(a.get("id")) for a in existing}
            new = []
            for alert in alerts:
                if str(alert.get("id")) in seen:
                    continue
                alert = dict(alert)
                alert["ts"] = _utc_now()
                alert["read"] = False
                existing.append(alert)
                seen.add(str(alert.get("id")))
                new.append(alert)
            self.save(existing)
            return new

    def unread_count(self) -> int:
        return sum(1 for a in self.load() if not a.get("read"))

    def mark_all_read(self) -> int:
        with file_lock(self.path):
            rows = self.load()
            changed = 0
            for row in rows:
                if not row.get("read"):
                    row["read"] = True
                    changed += 1
            self.save(rows)
            return changed
