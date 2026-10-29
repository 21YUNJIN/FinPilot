"""Transaction-cost model for backtests.

A-share backtests that assume a fixed basis-point slippage understate the cost
of trading in size.  This module adds a simple square-root market-impact term
(Almgren-Chriss / square-root law) on top of the fixed spread slippage, driven
by the order's participation rate in the day's traded notional.

All functions are pure so they can be unit-tested independently of the engine.
"""

from __future__ import annotations

import math


def participation_rate(
    order_notional: float,
    daily_notional: float,
    cap: float = 0.10,
) -> float:
    """Fraction of the day's traded notional consumed by an order, capped."""
    if daily_notional <= 0.0:
        return 0.0
    raw = max(0.0, order_notional) / daily_notional
    return min(raw, max(0.0, cap))


def square_root_impact_bps(participation: float, coefficient: float = 25.0) -> float:
    """Square-root temporary-impact cost in basis points.

    ``coefficient`` is the impact in bps at 100% participation.  For a 1%
    participation order with ``coefficient=25`` the impact is 2.5 bps.
    """
    return coefficient * math.sqrt(max(0.0, participation))


def total_slippage_bps(
    order_notional: float,
    daily_notional: float,
    slippage_bps: float,
    impact_coefficient: float = 25.0,
    participation_cap: float = 0.10,
    use_volume_impact: bool = True,
) -> float:
    """Total one-sided slippage in bps: fixed spread plus size-dependent impact."""
    if not use_volume_impact or impact_coefficient <= 0.0:
        return slippage_bps
    participation = participation_rate(order_notional, daily_notional, participation_cap)
    impact = square_root_impact_bps(participation, impact_coefficient)
    return slippage_bps + impact
