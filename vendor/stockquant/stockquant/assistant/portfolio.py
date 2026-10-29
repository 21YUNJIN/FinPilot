from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from stockquant.domain import MarketData
from stockquant.execution.filelock import file_lock


class PortfolioLedger:
    """Paper-only portfolio ledger. No broker connectivity."""

    def __init__(self, path: str, initial_cash: float = 1_000_000.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            payload = {
                "initial_cash": initial_cash,
                "cash": initial_cash,
                "positions": {},
                "trades": [],
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.save(payload)

    def load(self) -> Dict[str, object]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload.setdefault("positions", {})
        payload.setdefault("external_positions", {})
        payload.setdefault("trades", [])
        payload.setdefault("cash", self.load_initial_cash())
        return payload

    def load_initial_cash(self) -> float:
        try:
            return float(json.loads(self.path.read_text(encoding="utf-8")).get("initial_cash", 1_000_000.0))
        except Exception:
            return 1_000_000.0

    def save(self, payload: Dict[str, object]) -> None:
        payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def trade(self, symbol: str, side: str, shares: int, price: float, reason: str = "manual") -> Dict[str, object]:
        with file_lock(self.path):
            symbol = symbol.upper()
            side = side.lower()
            if side not in {"buy", "sell"}:
                raise ValueError("side must be buy or sell")
            if shares <= 0 or price <= 0:
                raise ValueError("shares and price must be positive")
            payload = self.load()
            cash = float(payload["cash"])
            positions = payload["positions"]
            pos = positions.get(symbol, {"shares": 0, "average_cost": 0.0})
            current = int(pos.get("shares", 0))
            avg = float(pos.get("average_cost", 0.0))
            if side == "buy":
                cost = shares * price
                if cost > cash:
                    raise ValueError("insufficient paper cash")
                new_shares = current + shares
                avg = (current * avg + cost) / new_shares if new_shares else 0.0
                positions[symbol] = {"shares": new_shares, "average_cost": avg}
                cash -= cost
            else:
                if shares > current:
                    raise ValueError("insufficient paper shares")
                cash += shares * price
                remaining = current - shares
                if remaining:
                    positions[symbol] = {"shares": remaining, "average_cost": avg}
                else:
                    positions.pop(symbol, None)
            payload["cash"] = cash
            payload["positions"] = positions
            payload["trades"].append(
                {
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "symbol": symbol,
                    "side": side,
                    "shares": shares,
                    "price": price,
                    "reason": reason,
                }
            )
            self.save(payload)
            return payload

    def set_holding(self, symbol: str, shares: int, average_cost: float, note: str = "manual_holding") -> Dict[str, object]:
        with file_lock(self.path):
            symbol = symbol.upper()
            if shares < 0 or average_cost < 0:
                raise ValueError("shares and average_cost must be non-negative")
            payload = self.load()
            external = payload.setdefault("external_positions", {})
            if shares == 0:
                external.pop(symbol, None)
            else:
                external[symbol] = {"shares": int(shares), "average_cost": float(average_cost)}
            payload["trades"].append(
                {
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "symbol": symbol,
                    "side": "mark_holding" if shares else "remove_holding",
                    "shares": int(shares),
                    "price": float(average_cost),
                    "reason": note,
                }
            )
            self.save(payload)
            return payload

    def add_holding(self, symbol: str, shares: int, price: float, note: str = "mark_buy") -> Dict[str, object]:
        with file_lock(self.path):
            """Record a real buy: merge into the external position with a weighted
            average cost. This is how the system learns what the user actually holds."""
            symbol = symbol.upper()
            if shares <= 0:
                raise ValueError("shares must be positive")
            if price <= 0:
                raise ValueError("price must be positive")
            payload = self.load()
            external = payload.setdefault("external_positions", {})
            pos = external.get(symbol, {"shares": 0, "average_cost": 0.0}) or {}
            old_shares = int(pos.get("shares", 0) or 0)
            old_avg = float(pos.get("average_cost", 0.0) or 0.0)
            new_shares = old_shares + int(shares)
            new_avg = ((old_shares * old_avg) + (int(shares) * float(price))) / new_shares if new_shares else 0.0
            external[symbol] = {"shares": new_shares, "average_cost": new_avg}
            payload["trades"].append({
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "side": "mark_buy", "shares": int(shares),
                "price": float(price), "reason": note,
            })
            self.save(payload)
            return payload

    def reduce_holding(self, symbol: str, shares: int, price: float = 0.0, note: str = "mark_sell") -> Dict[str, object]:
        with file_lock(self.path):
            """Record a real sell: reduce (or remove) the external position."""
            symbol = symbol.upper()
            if shares <= 0:
                raise ValueError("shares must be positive")
            payload = self.load()
            external = payload.setdefault("external_positions", {})
            pos = external.get(symbol)
            if not pos:
                raise ValueError(f"no recorded holding for {symbol}")
            old_shares = int(pos.get("shares", 0) or 0)
            remaining = max(0, old_shares - int(shares))
            if remaining:
                external[symbol] = {"shares": remaining, "average_cost": float(pos.get("average_cost", 0.0) or 0.0)}
            else:
                external.pop(symbol, None)
            payload["trades"].append({
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "side": "mark_sell", "shares": int(shares),
                "price": float(price), "reason": note,
            })
            self.save(payload)
            return payload

    def mark_to_market(self, market: MarketData, date: Optional[str] = None) -> Dict[str, object]:
        payload = self.load()
        date = date or market.dates[-1]
        total_value = float(payload["cash"])
        rows = []
        for symbol, pos in payload["positions"].items():
            bar = market.bar(symbol, date)
            px = (bar.raw_close if bar and bar.raw_close > 0 else bar.close) if bar else float(pos.get("average_cost", 0.0))
            value = px * int(pos.get("shares", 0))
            total_value += value
            rows.append(
                {
                    "symbol": symbol,
                    "shares": int(pos.get("shares", 0)),
                    "average_cost": float(pos.get("average_cost", 0.0)),
                    "price": px,
                    "market_value": value,
                    "pnl": (px - float(pos.get("average_cost", 0.0))) * int(pos.get("shares", 0)),
                }
            )
        external_rows = []
        external_value = 0.0
        for symbol, pos in payload.get("external_positions", {}).items():
            bar = market.bar(symbol, date)
            px = (bar.raw_close if bar and bar.raw_close > 0 else bar.close) if bar else float(pos.get("average_cost", 0.0))
            value = px * int(pos.get("shares", 0))
            external_value += value
            external_rows.append(
                {
                    "symbol": symbol,
                    "shares": int(pos.get("shares", 0)),
                    "average_cost": float(pos.get("average_cost", 0.0)),
                    "price": px,
                    "market_value": value,
                    "pnl": (px - float(pos.get("average_cost", 0.0))) * int(pos.get("shares", 0)),
                    "return_pct": (px / float(pos.get("average_cost", 1.0)) - 1.0) if float(pos.get("average_cost", 0.0)) > 0 else 0.0,
                }
            )
        payload["date"] = date
        payload["total_value"] = total_value
        payload["position_rows"] = rows
        payload["external_rows"] = external_rows
        payload["external_market_value"] = external_value
        return payload

    def export_csv(self, path: str) -> None:
        payload = self.load()
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["time_utc", "symbol", "side", "shares", "price", "reason"])
            writer.writeheader()
            writer.writerows(payload.get("trades", []))
