"""Almgren-Chriss optimal execution.

The current backtest charges a *static* square-root impact per order, which is a
reasonable cost estimate but says nothing about **how to slice** a large order.
Almgren & Chriss (2000) "Optimal execution of portfolio transactions" solves for
the trajectory that minimises

    E[cost] + lambda * Var[cost]

with linear temporary impact ``eta`` and permanent impact ``gamma``.  For a
constant volatility ``sigma`` and risk aversion ``lambda`` the optimal holdings
path is

    x_j = X * sinh(kappa (T - t_j)) / sinh(kappa T),   kappa = sqrt(lambda sigma^2 / eta)

``kappa -> 0`` recovers a uniform TWAP; larger ``kappa`` front-loads the order
(trade faster to cut timing risk).

This module gives the schedule and its expected cost so the execution layer can
compare AC against TWAP.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List

__all__ = ["optimal_execution_schedule", "expected_cost"]


@dataclass
class ExecutionSchedule:
    total_shares: float
    horizon: float
    n_steps: int
    kappa: float
    holdings: List[float]      # shares still to execute at the start of each step
    trades: List[float]        # shares executed in each step


def optimal_execution_schedule(
    total_shares: float,
    horizon: float,
    n_steps: int,
    sigma: float,
    eta: float,
    risk_aversion: float = 1e-6,
) -> ExecutionSchedule:
    """AC optimal holdings/trade trajectory.

    ``sigma`` is per-period volatility of the price (currency/√period),
    ``eta`` the temporary-impact coefficient (currency per share per unit rate),
    ``risk_aversion`` lambda.
    """
    if n_steps <= 0:
        raise ValueError("n_steps must be positive")
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    tau = horizon / n_steps
    kappa = math.sqrt(max(risk_aversion, 0.0) * sigma * sigma / eta) if eta > 0 else 0.0
    holdings: List[float] = []
    for j in range(n_steps + 1):
        t_j = j * tau
        if kappa < 1e-9:
            x = total_shares * (1.0 - j / n_steps)
        else:
            x = total_shares * math.sinh(kappa * (horizon - t_j)) / math.sinh(kappa * horizon)
        holdings.append(x)
    trades = [holdings[j] - holdings[j + 1] for j in range(n_steps)]
    return ExecutionSchedule(
        total_shares=total_shares, horizon=horizon, n_steps=n_steps,
        kappa=kappa, holdings=holdings, trades=trades,
    )


def expected_cost(
    schedule: ExecutionSchedule,
    temporary_impact: float,
    permanent_impact: float = 0.0,
    price: float = 1.0,
) -> Dict[str, float]:
    """Expected AC cost split into temporary and permanent components.

    Temporary cost ``sum n_j^2 * temporary_impact`` (linear in the rate, so the
    total scales with the slice size), permanent cost
    ``0.5 * permanent_impact * total_shares^2`` (Almgren-Chriss, 2000).
    """
    temp = sum(n * n for n in schedule.trades) * temporary_impact
    perm = 0.5 * permanent_impact * schedule.total_shares ** 2
    return {
        "temporary_cost": temp,
        "permanent_cost": perm,
        "total_cost": temp + perm,
        "total_cost_bps": (temp + perm) / (price * schedule.total_shares) * 1e4 if schedule.total_shares > 0 else 0.0,
    }
