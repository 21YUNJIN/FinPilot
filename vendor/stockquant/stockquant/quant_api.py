"""StockQuant-native, read-only quant service functions.

These functions are the contract layer for the future LLM query interface and
the web workbench.  They never place orders, mutate strategy state, or read
secrets.  All calculations are deterministic and testable.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, Iterable, Mapping, Optional

from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec
from stockquant.portfolio.risk import portfolio_volatility


def resolve_symbol(market: MarketData, symbol: str) -> str:
    text = str(symbol).strip().upper()
    if text in market.bars_by_symbol:
        return text
    if "." not in text:
        candidates = []
        if text.startswith(("6", "9")):
            candidates.append(text + ".SH")
        elif text.startswith(("0", "3")):
            candidates.append(text + ".SZ")
        candidates.append(text)
        for candidate in candidates:
            if candidate in market.bars_by_symbol:
                return candidate
    raise ValueError(f"unknown StockQuant symbol: {symbol}")


def last_available_date(market: MarketData, as_of_date: str) -> str:
    requested = str(as_of_date or "").strip()
    if not requested:
        requested = market.dates[-1]
    available = [d for d in market.dates if d <= requested]
    if not available:
        raise ValueError(f"no market data on or before {requested}")
    return available[-1]


def factor_snapshot(
    market: MarketData,
    symbol: str,
    as_of_date: str,
    *,
    factors: Optional[Iterable[FactorSpec]] = None,
    min_history: int = 80,
    neutralize_by: str = "cap_decile",
) -> Dict[str, Any]:
    """Return factor z-scores and composite score for one symbol."""
    resolved = resolve_symbol(market, symbol)
    actual = last_available_date(market, as_of_date)
    fset = tuple(factors or DEFAULT_FACTORS)
    engine = FactorEngine(
        fset,
        min_history=min_history,
        sector_neutral=True,
        neutralize_by=neutralize_by,
    )
    components, diagnostics = engine.components(market, actual)
    values = {spec.name: components.get(spec.name, {}).get(resolved) for spec in fset}
    if not any(v is not None for v in values.values()):
        raise ValueError(f"no factor values available for {resolved} on {actual}")
    total_weight = sum(abs(spec.weight) for spec in fset) or 1.0
    composite = sum(spec.weight * float(values.get(spec.name) or 0.0) for spec in fset) / total_weight
    return {
        "mode": "read_only",
        "symbol": resolved,
        "requested_as_of": str(as_of_date or ""),
        "as_of": actual,
        "composite_score": composite,
        "factor_z": values,
        "diagnostics": dataclasses.asdict(diagnostics),
    }


def _position_values(market: MarketData, positions: Mapping[str, float], as_of_date: str):
    actual = last_available_date(market, as_of_date)
    snapshot = market.snapshot(actual, apply_membership=False)
    resolved: Dict[str, float] = {}
    values: Dict[str, float] = {}
    prices: Dict[str, float] = {}
    warnings = []
    for raw_symbol, shares in positions.items():
        symbol = resolve_symbol(market, raw_symbol)
        qty = float(shares)
        if abs(qty) <= 1e-12:
            continue
        bar = snapshot.get(symbol)
        if bar is None or bar.close <= 0 or not bar.quote_fresh():
            warnings.append(f"{symbol}: no fresh price on {actual}")
            continue
        resolved[symbol] = qty
        prices[symbol] = bar.close
        values[symbol] = qty * bar.close
    return actual, resolved, values, prices, warnings


def portfolio_risk(
    market: MarketData,
    positions: Mapping[str, float],
    as_of_date: str,
    *,
    lookback: int = 60,
) -> Dict[str, Any]:
    """Return market values, weights and annualized portfolio volatility."""
    if not positions:
        raise ValueError("positions cannot be empty")
    actual, resolved, values, prices, warnings = _position_values(market, positions, as_of_date)
    total = sum(values.values())
    if total <= 0:
        raise ValueError("portfolio has no positive market value")
    weights = {s: v / total for s, v in values.items()}
    vol = portfolio_volatility(market, actual, weights, lookback=lookback)
    if vol is None:
        warnings.append("portfolio volatility unavailable (insufficient covariance history)")
    return {
        "mode": "read_only",
        "requested_as_of": str(as_of_date or ""),
        "as_of": actual,
        "positions": resolved,
        "prices": prices,
        "position_values": values,
        "total_value": total,
        "weights": weights,
        "annualized_volatility": vol,
        "cash_weight": max(0.0, 1.0 - sum(weights.values())),
        "warnings": warnings,
    }


def stress_test(
    market: MarketData,
    positions: Mapping[str, float],
    as_of_date: str,
    scenarios: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Simple deterministic scenario shocks.

    Supported scenario fields:
    - ``market_shock``: decimal shock applied to every priced position;
    - ``symbol_shocks``: decimal shocks for named symbols;
    - ``vol_multiplier``: multiply current annualized volatility.

    This is a transparent first-order stress test, not a full factor/beta model.
    """
    baseline = portfolio_risk(market, positions, as_of_date)
    scenarios = scenarios or {
        "market_down_10": {"market_shock": -0.10},
        "market_down_20": {"market_shock": -0.20},
        "vol_double": {"vol_multiplier": 2.0},
    }
    results = []
    total = float(baseline["total_value"])
    for name, spec in scenarios.items():
        values = dict(baseline["position_values"])
        market_shock = float(spec.get("market_shock", 0.0) or 0.0)
        if market_shock:
            values = {s: v * (1.0 + market_shock) for s, v in values.items()}
        symbol_shocks = spec.get("symbol_shocks") or {}
        for raw_symbol, shock in symbol_shocks.items():
            symbol = resolve_symbol(market, raw_symbol)
            if symbol in values:
                values[symbol] *= 1.0 + float(shock)
        scenario_value = sum(values.values())
        vol = baseline.get("annualized_volatility")
        multiplier = float(spec.get("vol_multiplier", 1.0) or 1.0)
        results.append({
            "name": str(name),
            "scenario_value": scenario_value,
            "pnl": scenario_value - total,
            "pnl_pct": (scenario_value / total - 1.0) if total else None,
            "annualized_volatility": (float(vol) * multiplier) if vol is not None else None,
            "assumptions": {
                "market_shock": market_shock,
                "symbol_shocks": symbol_shocks,
                "vol_multiplier": multiplier,
            },
        })
    return {
        "mode": "read_only",
        "as_of": baseline["as_of"],
        "baseline": baseline,
        "scenarios": results,
    }


__all__ = ["resolve_symbol", "last_available_date", "factor_snapshot",
           "portfolio_risk", "stress_test"]
