from __future__ import annotations

import json
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .ccxt_broker import CcxtBroker


class CryptoMonitor:
    def __init__(self, state_dir: str, exchange: str = "okx", proxy_url: str = "http://127.0.0.1:7897"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.broker = CcxtBroker(exchange, testnet=False, proxy_url=proxy_url, public=True)
        self.exchange = self.broker.exchange

    @staticmethod
    def _ma(values: List[float], period: int) -> float:
        return sum(values[-period:]) / period if len(values) >= period else 0.0

    def _notify(self, ready: List[Dict[str, object]]) -> None:
        if not ready:
            return
        names = "、".join(str(x["symbol"]) for x in ready[:3])
        try:
            subprocess.run(["osascript", "-e", f'display notification "{names} 出现加密入场条件" with title "StockQuant 加密盯盘"'], check=False, timeout=5)
        except Exception:
            pass

    def snapshot(self, symbols: List[str]) -> Dict[str, object]:
        rows = []
        for symbol in symbols:
            try:
                # Drop the currently forming candle.  Signals must come from a
                # completed bar, otherwise the dashboard and execution loop can
                # repaint intrabar and produce a different signal every minute.
                raw_ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe="1h", limit=121)
                ohlcv = raw_ohlcv[:-1] if raw_ohlcv else []
                if not ohlcv or len(ohlcv) < 60:
                    continue
                closes = [float(x[4]) for x in ohlcv]
                highs = [float(x[2]) for x in ohlcv]
                lows = [float(x[3]) for x in ohlcv]
                volumes = [float(x[5]) for x in ohlcv]
                last = closes[-1]
                ma20 = self._ma(closes, 20)
                ma50 = self._ma(closes, 50)
                atr_vals = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
                atr = statistics.mean(atr_vals[-14:]) if len(atr_vals) >= 14 else 0.0
                avg_volume = statistics.mean(volumes[-21:-1]) if len(volumes) > 21 else statistics.mean(volumes)
                volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0
                high20 = max(highs[-20:])
                if last > ma20 > ma50 and volume_ratio >= 1.2:
                    status, reason = "ready", "趋势向上且放量突破"
                elif last <= ma20 * 1.01 and ma20 > ma50:
                    status, reason = "near", "回踩MA20且趋势仍向上"
                elif last < ma20 < ma50:
                    status, reason = "bearish", "趋势转弱"
                else:
                    status, reason = "watch", "等待趋势确认"
                rows.append({"symbol": symbol, "status": status, "reason": reason, "last": last, "ma20": ma20, "ma50": ma50, "atr": atr, "high20": high20, "volume_ratio": volume_ratio, "stop_loss": last - 2.0 * atr, "take_profit": last + 3.0 * atr})
            except Exception as exc:
                rows.append({"symbol": symbol, "status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        ready = [x for x in rows if x.get("status") == "ready"]
        self._notify(ready)
        payload = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "exchange": self.exchange.id, "rows": rows, "ready_count": len(ready)}
        (self.state_dir / "crypto_monitor.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
