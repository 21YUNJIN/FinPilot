"""Deterministic exit policies: stop-loss, take-profit and trailing stop.

These policies turn a position and a current price into an exit order.  They are
kept pure and separate from the broker so the risk-critical decision can be unit
tested without any exchange or market simulation.

Order type: a market order by default, or a protective limit order a small
offset below the trigger price when ``use_limit_order`` is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .types import OrderRequest


@dataclass(frozen=True)
class ExitPolicy:
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20
    trailing_stop_pct: float = 0.0  # 0 disables the trailing stop
    use_limit_order: bool = False
    limit_offset_bps: float = 10.0

    def limit_price(self, side: str, price: float) -> Optional[float]:
        if not self.use_limit_order:
            return None
        offset = price * self.limit_offset_bps / 10000.0
        # Sell slightly below, buy slightly above, to keep the order aggressive.
        return price - offset if side == "sell" else price + offset


@dataclass
class PositionView:
    symbol: str
    quantity: float
    average_cost: float
    peak_price: float = 0.0

    def update_peak(self, price: float) -> None:
        self.peak_price = max(self.peak_price, price)


def evaluate_exit(position: PositionView, price: float, policy: ExitPolicy) -> Optional[OrderRequest]:
    """Return an exit order for one position, or None if no rule triggers."""
    if position.quantity <= 0 or price <= 0 or position.average_cost <= 0:
        return None
    position.update_peak(price)
    reason = ""
    if policy.stop_loss_pct > 0 and price <= position.average_cost * (1.0 - policy.stop_loss_pct):
        reason = f"stop_loss({(price / position.average_cost - 1.0):.2%})"
    elif policy.take_profit_pct > 0 and price >= position.average_cost * (1.0 + policy.take_profit_pct):
        reason = f"take_profit({(price / position.average_cost - 1.0):.2%})"
    elif (
        policy.trailing_stop_pct > 0
        and position.peak_price > 0
        and price <= position.peak_price * (1.0 - policy.trailing_stop_pct)
    ):
        reason = f"trailing_stop(peak {position.peak_price:.4f})"
    if not reason:
        return None
    return OrderRequest(
        symbol=position.symbol,
        side="sell",
        quantity=position.quantity,
        order_type="limit" if policy.use_limit_order else "market",
        limit_price=policy.limit_price("sell", price),
        reason=reason,
        strategy_id="exit_policy",
    )


def evaluate_exits(
    positions: Dict[str, PositionView],
    prices: Dict[str, float],
    policy: ExitPolicy,
    peaks: Optional[Dict[str, float]] = None,
) -> List[OrderRequest]:
    """Evaluate an exit policy across a portfolio, returning triggered orders."""
    orders: List[OrderRequest] = []
    for symbol, position in positions.items():
        price = prices.get(symbol)
        if price is None:
            continue
        if peaks and symbol in peaks:
            position.peak_price = max(position.peak_price, peaks[symbol])
        order = evaluate_exit(position, price, policy)
        if order is not None:
            orders.append(order)
    return orders
