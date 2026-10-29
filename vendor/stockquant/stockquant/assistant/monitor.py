"""Holdings monitor: model-based analysis of the user's actual positions.

Unlike the daily assistant (which runs a full backtest plus alert generation),
this module is deliberately lightweight: given the current market data and the
ledger's real (external) holdings, it scores each position with the factor
model and turns that into an actionable view (hold / reduce / stop-loss / take
profit) together with ATR-based stop and target levels.

It also owns two small state files:

* ``monitor_settings.json`` - enabled flag, polling interval, dataset key.
* ``position_monitor.json`` - the latest analysis snapshot.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from stockquant.domain import MarketData
from stockquant.factors import FactorEngine

DEFAULT_SETTINGS: Dict[str, object] = {
    "enabled": False,
    "notify": True,
    "interval_seconds": 300,
    "dataset": "csi300_full",
    "stop_atr_multiple": 2.0,
    "take_profit_atr_multiple": 3.0,
}

ACTION_STOP = "止损/减仓"
ACTION_TAKE = "止盈/分批减仓"
ACTION_REDUCE = "减仓"
ACTION_WATCH = "持有观察"
ACTION_ADD = "持有/可加仓"
ACTION_NODATA = "数据不足"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MonitorStore:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.state_dir / "monitor_settings.json"
        self.snapshot_path = self.state_dir / "position_monitor.json"

    def load_settings(self) -> Dict[str, object]:
        payload = dict(DEFAULT_SETTINGS)
        try:
            payload.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        return payload

    def save_settings(self, updates: Dict[str, object]) -> Dict[str, object]:
        settings = self.load_settings()
        for key in DEFAULT_SETTINGS:
            if key in updates:
                settings[key] = updates[key]
        settings["enabled"] = bool(settings.get("enabled"))
        try:
            settings["interval_seconds"] = max(60, int(settings.get("interval_seconds", 300)))
        except Exception:
            settings["interval_seconds"] = 300
        self.settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        return settings

    def load_snapshot(self) -> Dict[str, object]:
        try:
            return json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_snapshot(self, snapshot: Dict[str, object]) -> None:
        self.snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class HoldingsMonitor:
    @staticmethod
    def _atr(market: MarketData, symbol: str, date: str, lookback: int = 14) -> float:
        bars = market.history(symbol, date, lookback=lookback + 1)
        values = []
        for prev, cur in zip(bars, bars[1:]):
            values.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        return statistics.mean(values) if values else 0.0

    @staticmethod
    def _realized_vol(market: MarketData, symbol: str, date: str, lookback: int = 60) -> float:
        bars = market.history(symbol, date, lookback=lookback + 1)
        rets = [b.close / a.close - 1.0 for a, b in zip(bars, bars[1:]) if a.close > 0 and b.close > 0]
        return statistics.pstdev(rets) * math.sqrt(252.0) if len(rets) > 10 else 0.0

    def analyze(
        self,
        market: MarketData,
        holdings: List[Dict[str, object]],
        config,
        latest_date: Optional[str] = None,
        settings: Optional[Dict[str, object]] = None,
        price_overrides: Optional[Dict[str, float]] = None,
    ) -> Dict[str, object]:
        settings = settings or DEFAULT_SETTINGS
        stop_mult = float(settings.get("stop_atr_multiple", 2.0) or 2.0)
        take_mult = float(settings.get("take_profit_atr_multiple", 3.0) or 3.0)
        date = latest_date or (market.dates[-1] if market.dates else None)
        if not date:
            return {"generated_at_utc": _utc_now(), "signal_date": None, "rows": [], "summary": {}, "holdings": 0}

        engine = FactorEngine(
            factors=config.factors,
            min_history=config.min_history,
            sector_neutral=config.sector_neutral,
        )
        scores, _ = engine.score(market, date)
        total_value = sum(float(h.get("market_value", 0.0) or 0.0) for h in holdings)

        rows: List[Dict[str, object]] = []
        summary = {ACTION_STOP: 0, ACTION_TAKE: 0, ACTION_REDUCE: 0, ACTION_WATCH: 0, ACTION_ADD: 0, ACTION_NODATA: 0}
        for holding in holdings:
            symbol = str(holding.get("symbol"))
            shares = int(holding.get("shares", 0) or 0)
            cost = float(holding.get("average_cost", 0.0) or 0.0)
            bar = market.bar(symbol, date)
            live = (price_overrides or {}).get(symbol)
            if live and float(live) > 0:
                price = float(live)
                price_source = "realtime"
            else:
                price = (bar.raw_close if bar and bar.raw_close > 0 else (bar.close if bar else 0.0)) or float(holding.get("price", 0.0) or 0.0)
                price_source = "daily_close"
            pnl_pct = (price / cost - 1.0) if cost > 0 else 0.0
            atr = self._atr(market, symbol, date)
            # ATR is computed on the (adjusted) price series, so the ratio must
            # use the same-series close -- not the raw/unadjusted mark price,
            # otherwise the stop/target levels are on the wrong scale.
            series_close = bar.close if bar and bar.close > 0 else price
            atr_ratio = atr / series_close if series_close > 0 else 0.0
            score = scores.get(symbol)
            vol = self._realized_vol(market, symbol, date)

            if bar is None or price <= 0:
                action, reason = ACTION_NODATA, "缺少行情数据"
            elif cost > 0 and price <= cost * (1.0 - stop_mult * atr_ratio):
                action, reason = ACTION_STOP, f"价格跌破 {stop_mult:.0f}×ATR 止损线"
            elif cost > 0 and price >= cost * (1.0 + take_mult * atr_ratio):
                action, reason = ACTION_TAKE, f"价格触及 {take_mult:.0f}×ATR 止盈线"
            elif score is None:
                action, reason = ACTION_WATCH, "因子数据不足"
            elif score < -0.5:
                action, reason = ACTION_REDUCE, "模型分数转负"
            elif score > 0.5 and pnl_pct > 0 and vol < 0.45:
                action, reason = ACTION_ADD, "因子与盈利缓冲仍在"
            else:
                action, reason = ACTION_WATCH, "信号中性"

            if vol >= 0.60:
                risk_level = "high"
            elif vol >= 0.35:
                risk_level = "medium"
            else:
                risk_level = "low"

            rows.append(
                {
                    "symbol": symbol,
                    "shares": shares,
                    "average_cost": cost,
                    "price": price,
                    "price_source": price_source,
                    "pnl_pct": pnl_pct,
                    "market_value": float(holding.get("market_value", 0.0) or 0.0),
                    "weight": (float(holding.get("market_value", 0.0) or 0.0) / total_value) if total_value > 0 else 0.0,
                    "score": score,
                    "volatility": vol,
                    "atr": atr,
                    "risk_level": risk_level,
                    "action": action,
                    "reason": reason,
                    "stop_loss": max(0.01, cost * (1.0 - stop_mult * atr_ratio)) if cost > 0 else 0.0,
                    "take_profit": cost * (1.0 + take_mult * atr_ratio) if cost > 0 else 0.0,
                }
            )
            summary[action] = summary.get(action, 0) + 1

        rows.sort(key=lambda r: (float(r.get("weight", 0.0) or 0.0)), reverse=True)
        max_weight = max((float(r.get("weight", 0.0) or 0.0) for r in rows), default=0.0)
        return {
            "generated_at_utc": _utc_now(),
            "signal_date": date,
            "holdings": len(rows),
            "total_market_value": total_value,
            "max_weight": max_weight,
            "summary": summary,
            "rows": rows,
        }
