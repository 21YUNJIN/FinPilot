from __future__ import annotations

import json
import math
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from stockquant.backtest import BacktestConfig
from stockquant.domain import MarketData
from stockquant.factors import FactorEngine


class WatchlistStore:
    def __init__(self, state_dir: str):
        self.path = Path(state_dir) / "watchlist.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def load(self) -> Dict[str, Dict[str, object]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, payload: Dict[str, Dict[str, object]]) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, symbol: str, note: str = "") -> Dict[str, Dict[str, object]]:
        payload = self.load()
        payload[symbol.upper()] = {"note": note, "added_at_utc": datetime.now(timezone.utc).isoformat()}
        self.save(payload)
        return payload

    def remove(self, symbol: str) -> Dict[str, Dict[str, object]]:
        payload = self.load()
        payload.pop(symbol.upper(), None)
        self.save(payload)
        return payload


class EntryAlertEngine:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.watchlist = WatchlistStore(str(self.state_dir))

    @staticmethod
    def _atr(market: MarketData, symbol: str, date: str, lookback: int = 14) -> float:
        bars = market.history(symbol, date, lookback=lookback + 1)
        vals = [max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)) for prev, cur in zip(bars, bars[1:])]
        return statistics.mean(vals) if vals else 0.0

    @staticmethod
    def _vol(market: MarketData, symbol: str, date: str, lookback: int = 60) -> float:
        bars = market.history(symbol, date, lookback=lookback + 1)
        rets = [b.close / a.close - 1.0 for a, b in zip(bars, bars[1:]) if a.close > 0 and b.close > 0]
        return statistics.pstdev(rets) * math.sqrt(252.0) if len(rets) > 10 else 0.0

    def _notify(self, ready: List[Dict[str, object]]) -> None:
        if not ready:
            return
        names = "、".join(str(x["symbol"]) for x in ready[:3])
        message = f"出现 {len(ready)} 个入场提醒：{names}"
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "StockQuant 入场提醒"'],
                check=False,
                timeout=5,
            )
        except Exception:
            pass

    def generate(self, market: MarketData, config: BacktestConfig, daily_plan: Dict[str, object]) -> Dict[str, object]:
        latest = market.dates[-1]
        snapshot = market.snapshot(latest)
        engine = FactorEngine(config.factors, min_history=config.min_history, sector_neutral=config.sector_neutral)
        scores, _ = engine.score(market, latest)
        existing_plans = {str(x["symbol"]): x for x in daily_plan.get("plans", [])}
        stock_news = {}
        for item in daily_plan.get("risk_events", []):
            if item.get("symbol"):
                stock_news.setdefault(str(item["symbol"]), []).append(item)
        symbols = sorted(set(self.watchlist.load()) | set(existing_plans) | {str(x["symbol"]) for x in daily_plan.get("recommendations", []) if x.get("symbol")})
        rows = []
        for symbol in symbols:
            bar = snapshot.get(symbol) or market.bar(symbol, latest)
            if bar is None:
                continue
            score = float(scores.get(symbol, 0.0))
            vol = self._vol(market, symbol, latest)
            atr = self._atr(market, symbol, latest)
            raw_price = bar.raw_close if bar.raw_close > 0 else bar.close
            atr_ratio = atr / bar.close if bar.close > 0 else 0.0
            bars20 = market.history(symbol, latest, lookback=20)
            ma20_adj = statistics.mean([b.close for b in bars20]) if bars20 else bar.close
            raw_ratio = raw_price / bar.close if bar.close > 0 else 1.0
            ma20_raw = ma20_adj * raw_ratio
            high20_adj = max((b.high for b in bars20), default=bar.high)
            high20_raw = high20_adj * raw_ratio
            avg_volume = statistics.mean([b.volume for b in bars20[:-1]]) if len(bars20) > 1 else bar.volume
            volume_ratio = bar.volume / avg_volume if avg_volume > 0 else 1.0
            entry_low = ma20_raw * (1.0 - 0.5 * atr_ratio)
            entry_high = ma20_raw * (1.0 + 0.5 * atr_ratio)
            plan = existing_plans.get(symbol, {})
            direct_negative = bool(plan.get("negative_news"))
            data_stale = bool(daily_plan.get("data_freshness", {}).get("stale", False))
            regime = str(daily_plan.get("market_regime", {}).get("regime", "unknown"))
            if data_stale:
                status = "blocked"
                reason = "数据过期"
            elif regime == "risk_off":
                status = "blocked"
                reason = "市场处于风险收缩状态"
            elif direct_negative:
                status = "blocked"
                reason = "存在直接负面新闻"
            elif score <= 0.15 or vol >= 0.55:
                status = "watch"
                reason = "因子优势不足或波动过高"
            near_ma = entry_low <= raw_price <= entry_high and score >= 0.35
            breakout = high20_raw * 0.995 <= raw_price <= high20_raw * 1.02 and volume_ratio >= 1.2 and score >= 0.35
            if near_ma or breakout:
                status = "ready"
                reason = "回踩20日均线且模型优势达标" if near_ma else "放量突破20日高点且模型优势达标"
            elif raw_price <= entry_high * 1.03:
                status = "near"
                reason = "接近20日均线买入区间"
            else:
                status = "watch"
                reason = "等待回踩20日均线或放量突破"
            rows.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "reason": reason,
                    "score": score,
                    "volatility": vol,
                    "current_price": raw_price,
                    "entry_zone": [entry_low, entry_high],
                    "ma20": ma20_raw,
                    "breakout_high": high20_raw,
                    "volume_ratio": volume_ratio,
                    "stop_loss": raw_price * (1.0 - 2.0 * atr_ratio),
                    "take_profit": raw_price * (1.0 + 3.0 * atr_ratio),
                    "direct_negative_news": plan.get("negative_news", []),
                    "note": self.watchlist.load().get(symbol, {}).get("note", ""),
                }
            )
        rows.sort(key=lambda x: ({"ready": 0, "near": 1, "watch": 2, "blocked": 3}.get(str(x["status"]), 9), -float(x["score"])))
        ready = [x for x in rows if x["status"] == "ready"]
        state_path = self.state_dir / "alert_state.json"
        try:
            alert_state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            alert_state = {}
        new_ready = []
        for row in ready:
            symbol = str(row["symbol"])
            if alert_state.get(symbol) != latest:
                new_ready.append(row)
                alert_state[symbol] = latest
        self._notify(new_ready)
        state_path.write_text(json.dumps(alert_state, ensure_ascii=False, indent=2), encoding="utf-8")
        payload = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "latest_market_date": latest,
            "rows": rows,
            "ready_count": len(ready),
            "new_alert_count": len(new_ready),
            "watchlist": self.watchlist.load(),
        }
        (self.state_dir / "entry_alerts.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
