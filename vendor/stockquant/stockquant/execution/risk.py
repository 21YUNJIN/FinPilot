from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .types import AccountSnapshot, OrderRequest, Quote, RiskDecision


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 20_000.0
    max_position_weight: float = 0.10
    max_gross_exposure: float = 1.00
    max_daily_loss: float = 0.02
    max_drawdown: float = 0.10
    max_quote_age_seconds: int = 30
    max_spread_bps: float = 50.0
    max_orders_per_minute: int = 10
    min_quote_volume: float = 0.0


class QuoteValidator:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def validate(self, quote: Quote) -> RiskDecision:
        reasons = []
        if quote.last <= 0 or quote.bid <= 0 or quote.ask <= 0:
            reasons.append("quote price invalid")
        if quote.ask < quote.bid:
            reasons.append("crossed quote")
        try:
            quote_time = datetime.fromisoformat(quote.timestamp_utc.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - quote_time).total_seconds()
            if age > self.limits.max_quote_age_seconds:
                reasons.append(f"quote stale: {age:.0f}s")
        except Exception:
            reasons.append("quote timestamp invalid")
        if quote.spread_bps > self.limits.max_spread_bps:
            reasons.append(f"spread too wide: {quote.spread_bps:.1f} bps")
        if quote.volume < self.limits.min_quote_volume:
            reasons.append("quote volume below minimum")
        return RiskDecision(allowed=not reasons, reason="; ".join(reasons), adjusted_quantity=0.0)


class RiskManager:
    """Independent risk gate. Strategies cannot bypass this module."""

    @staticmethod
    def _floor_quantity(value: float, step: float) -> float:
        step = float(step or 0.01)
        if step <= 0 or value <= 0:
            return 0.0
        # Avoid binary-float artifacts while keeping the requested exchange step.
        return max(0.0, math.floor((value + step * 1e-9) / step) * step)

    def __init__(self, limits: RiskLimits, halt_file: str, state_path: Optional[str] = None):
        self.limits = limits
        self.halt_file = Path(halt_file)
        self.state_path = Path(state_path) if state_path else None
        self.quote_validator = QuoteValidator(limits)
        self.order_times: List[float] = []

    def _risk_equity_state(self, equity: float) -> Dict[str, float]:
        today = datetime.now(timezone.utc).date().isoformat()
        state: Dict[str, float] = {"day": today, "day_start_equity": equity, "peak_equity": equity}
        if self.state_path and self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
            except Exception:
                pass
        if state.get("day") != today:
            state["day"] = today
            state["day_start_equity"] = equity
            state["peak_equity"] = equity
        state["peak_equity"] = max(float(state.get("peak_equity", equity) or equity), equity)
        state["equity"] = equity
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    def halted(self) -> bool:
        return self.halt_file.exists()

    def halt(self, reason: str = "manual halt") -> None:
        """Create the persistent kill-switch file; no new order may pass."""
        self.halt_file.parent.mkdir(parents=True, exist_ok=True)
        self.halt_file.write_text(str(reason), encoding="utf-8")

    def resume(self) -> None:
        """Remove the kill switch; callers should require explicit human action."""
        try:
            self.halt_file.unlink()
        except FileNotFoundError:
            pass

    def check_order(
        self,
        order: OrderRequest,
        account: AccountSnapshot,
        quote: Quote,
        prices: Optional[Dict[str, float]] = None,
    ) -> RiskDecision:
        if self.halted():
            return RiskDecision(False, "trading halt switch is active")
        quote_check = self.quote_validator.validate(quote)
        if not quote_check.allowed:
            return quote_check
        now = time.time()
        self.order_times = [t for t in self.order_times if now - t <= 60.0]
        if len(self.order_times) >= self.limits.max_orders_per_minute:
            return RiskDecision(False, "order rate limit reached")
        if order.quantity <= 0:
            return RiskDecision(False, "order quantity must be positive")
        if account.equity <= 0:
            return RiskDecision(False, "account equity invalid")
        equity_state = self._risk_equity_state(account.equity)
        daily_start = float(equity_state.get("day_start_equity", account.equity) or account.equity)
        daily_pnl = account.equity - daily_start
        peak = max(float(equity_state.get("peak_equity", account.equity) or account.equity), account.equity)
        if daily_pnl <= -self.limits.max_daily_loss * max(daily_start, 1e-12):
            return RiskDecision(False, "daily loss limit reached")
        drawdown = (peak - account.equity) / peak if peak > 0 else 0.0
        if drawdown >= self.limits.max_drawdown:
            return RiskDecision(False, f"drawdown limit reached: {drawdown:.2%}")
        price = quote.ask if order.side.lower() == "buy" else quote.bid
        if price <= 0:
            return RiskDecision(False, "execution price invalid")
        step = float(getattr(quote, "amount_step", 0.01) or 0.01)
        notional = price * order.quantity
        if notional > self.limits.max_order_notional:
            adjusted = self._floor_quantity(self.limits.max_order_notional / price, step)
            return RiskDecision(True, "order reduced to max notional", adjusted)
        if order.side.lower() == "buy":
            current_value = 0.0
            if prices and order.symbol in account.positions:
                current_value = account.positions[order.symbol].quantity * prices.get(order.symbol, price)
            projected = current_value + notional
            if projected / account.equity > self.limits.max_position_weight:
                room = max(0.0, self.limits.max_position_weight * account.equity - current_value)
                adjusted = self._floor_quantity(room / price, step)
                return RiskDecision(True, "order reduced to position limit", adjusted)
        self.order_times.append(now)
        return RiskDecision(True, "approved", order.quantity)
