"""ETF pilot: a low-turnover, tradable book whose *execution* can be measured.

This is deliberately **not** an alpha model.  The stock line's edge is not
statistically significant and the convertible line needs an account permission
the user cannot obtain yet, so neither can be validated with real fills.  ETFs
need no permission, start at ~100 CNY, trade with a tight spread and pay no stamp
duty -- which makes them the only instrument where the assumptions that matter
(slippage, fill rate, reconciliation) can be tested with real money.

Construction (monthly, no stock selection):

1. inverse-volatility risk parity across a few broad sleeves
   (``w_i proportional to 1 / sigma_i``), so no single sleeve dominates the risk;
2. each sleeve is scaled by a trend filter (``close < MA(trend_lookback)`` ->
   ``risk_off_multiplier``), which is what actually caps drawdowns in A-shares;
3. a per-sleeve weight cap plus a cash buffer;
4. a no-trade band so monthly rebalances do not churn (target turnover is a few
   times a year, not a few times a month).

Signal timing matches the rest of the project: weights are computed from data up
to and including ``date`` (the close) and executed at the **next** session's open.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.backtest.costs import total_slippage_bps
from stockquant.backtest.metrics import performance_metrics
from stockquant.domain import Bar, MarketData

__all__ = ["EtfConfig", "EtfCostConfig", "sleeve_targets", "apply_lot_constraints",
           "lot_rounded_weights", "run_etf_backtest", "plan_orders"]


@dataclass(frozen=True)
class EtfConfig:
    symbols: Tuple[str, ...] = ("510300", "510500", "510880", "518880", "511010")
    vol_lookback: int = 60
    trend_lookback: int = 200
    risk_off_multiplier: float = 0.5
    max_weight: float = 0.35
    cash_buffer: float = 0.02
    vol_floor: float = 0.02


@dataclass(frozen=True)
class EtfCostConfig:
    initial_cash: float = 100_000.0
    commission_bps: float = 1.0        # ETF 佣金约万 0.5~万 2.5
    stamp_duty_bps: float = 0.0        # ETF 无印花税
    slippage_bps: float = 2.0
    impact_coefficient: float = 25.0
    participation_cap: float = 0.10
    min_commission: float = 5.0
    lot_size: int = 100                # ETF 最小 100 份
    rebalance_band: float = 0.01       # 权重偏离 <1% 就不动
    min_trade_weight: float = 0.005
    max_lot_fraction: float = 0.5      # 一个最小交易单位 > 本金的这个比例 → 该袖套无法持有
    start: Optional[str] = None
    end: Optional[str] = None
    warmup_bars: Optional[int] = None  # default: trend_lookback


@dataclass
class EtfTrade:
    date: str
    signal_date: str
    symbol: str
    side: str
    shares: int
    price: float
    gross_amount: float
    commission: float
    slippage_cost: float
    cash_after: float


@dataclass
class EtfBacktestResult:
    dates: List[str]
    equity_curve: List[float]
    benchmark_curve: List[float]
    trades: List[EtfTrade]
    turnover: List[float]
    gross_turnover: List[float]
    target_history: Dict[str, Dict[str, float]]
    metrics: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def _annualized_vol(bars: Sequence[Bar], lookback: int, floor: float) -> Optional[float]:
    window = bars[-(lookback + 1):]
    returns = [cur.close / prev.close - 1.0
               for prev, cur in zip(window, window[1:])
               if prev.close > 0 and cur.close > 0 and prev.quote_fresh() and cur.quote_fresh()]
    if len(returns) < max(10, lookback // 2):
        return None
    vol = statistics.pstdev(returns) * math.sqrt(252.0)
    return max(vol, floor)


def sleeve_targets(market: MarketData, date: str, config: EtfConfig) -> Dict[str, float]:
    """Inverse-vol risk parity + trend filter, capped and cash-buffered.

    A sleeve with insufficient history (fewer than ``trend_lookback`` bars) is
    excluded rather than given a default weight: "no data" must not look like
    "no risk".
    """
    raw: Dict[str, float] = {}
    for symbol in config.symbols:
        bars = market.history(symbol, date, lookback=config.trend_lookback + 1)
        if len(bars) < config.trend_lookback:
            continue
        vol = _annualized_vol(bars, config.vol_lookback, config.vol_floor)
        if not vol:
            continue
        value = 1.0 / vol
        closes = [b.close for b in bars[-config.trend_lookback:] if b.close > 0]
        if closes and bars[-1].close < sum(closes) / len(closes):
            value *= config.risk_off_multiplier
        raw[symbol] = value
    if not raw:
        return {}

    investable = max(0.0, 1.0 - config.cash_buffer)
    total = sum(raw.values())
    if total <= 0.0:
        # Every sleeve is risk-off (e.g. risk_off_multiplier=0 and the whole
        # universe is below its trend): hold cash instead of dividing by zero.
        return {}
    weights = {s: v / total * investable for s, v in raw.items()}

    # Per-sleeve cap via water-filling (keep the freed budget in the book).
    for _ in range(20):
        over = {s: w for s, w in weights.items() if w > config.max_weight + 1e-12}
        if not over:
            break
        freed = 0.0
        for s in over:
            freed += weights[s] - config.max_weight
            weights[s] = config.max_weight
        room = {s: max(0.0, config.max_weight - w) for s, w in weights.items()
                if w < config.max_weight - 1e-12}
        room_total = sum(room.values())
        if room_total <= 1e-12:
            break
        for s, r in room.items():
            weights[s] += freed * r / room_total
    return weights


def apply_lot_constraints(
    equity: float,
    targets: Dict[str, float],
    prices: Dict[str, float],
    cost: EtfCostConfig,
    max_lot_fraction: float = 0.5,
    max_weight: Optional[float] = None,
) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Drop sleeves whose **single lot** is too large a share of the book.

    A-share ETFs trade in lots of 100 units, and bond/money-market ETFs are
    quoted near 100 CNY -- one lot of 511010 (国债ETF, ~141 CNY) is ~14,000 CNY.
    On a 20,000 CNY account that sleeve simply cannot be held, so the *design*
    portfolio (which includes a bond anchor) is not implementable and its real
    risk profile is closer to an equity+gold book.  Silence here would make the
    plan look diversified while the account is not.

    The dropped weight is redistributed across the remaining sleeves (re-capped),
    never into cash, so the deviation from the design shows up as higher
    volatility rather than as an unexplained cash drag.
    """
    keep: Dict[str, float] = {}
    dropped: Dict[str, str] = {}
    for symbol, weight in targets.items():
        price = prices.get(symbol) or 0.0
        lot_value = price * cost.lot_size
        if price <= 0:
            dropped[symbol] = "no price"
        elif lot_value > max_lot_fraction * equity:
            dropped[symbol] = (f"one lot = {lot_value:,.0f} CNY > {max_lot_fraction:.0%} of "
                               f"{equity:,.0f} CNY")
        else:
            keep[symbol] = weight
    if not keep:
        return {}, dropped
    scale = sum(targets.values()) / sum(keep.values())
    out = {s: w * scale for s, w in keep.items()}
    if max_weight:
        for _ in range(20):
            over = {s: w for s, w in out.items() if w > max_weight + 1e-12}
            if not over:
                break
            freed = sum(w - max_weight for w in over.values())
            for s in over:
                out[s] = max_weight
            room = {s: max(0.0, max_weight - w) for s, w in out.items() if w < max_weight - 1e-12}
            total_room = sum(room.values())
            if total_room <= 1e-12:
                break
            for s, r in room.items():
                out[s] += freed * r / total_room
    return out, dropped


def lot_rounded_weights(
    equity: float,
    targets: Dict[str, float],
    prices: Dict[str, float],
    cost: EtfCostConfig,
    max_lot_fraction: float = 0.5,
    max_weight: Optional[float] = None,
) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Return *actual* post-lot target weights, not just dropped-sleeve weights.

    ``apply_lot_constraints`` only removes sleeves whose single lot is too
    large.  It leaves the remaining targets continuous.  A real account buys
    whole lots, so the achievable weight is ``floor(target_value/lot_value) *
    lot_value / equity``.  This helper makes that difference explicit and is
    used by the audit report and the order plan.
    """
    effective, dropped = apply_lot_constraints(
        equity, targets, prices, cost,
        max_lot_fraction=max_lot_fraction, max_weight=max_weight,
    )
    out: Dict[str, float] = {}
    if equity <= 0:
        return out, dropped
    for symbol, weight in effective.items():
        price = prices.get(symbol) or 0.0
        lot_value = price * cost.lot_size
        if price <= 0 or lot_value <= 0:
            dropped[symbol] = "no price"
            continue
        lots = int(max(0.0, equity * float(weight)) // lot_value)
        actual = lots * lot_value / equity
        if actual > 0:
            out[symbol] = actual
    return out, dropped


def plan_orders(
    equity: float,
    targets: Dict[str, float],
    current_value: Dict[str, float],
    prices: Dict[str, float],
    cost: EtfCostConfig,
    band: Optional[float] = None,
) -> List[dict]:
    """Order list for one rebalance (sells first, then buys).

    ``current_value`` is the market value held per symbol, so this works both for
    the backtest and for "what do I actually key in today".
    """
    band = cost.rebalance_band if band is None else band
    orders: List[dict] = []
    symbols = sorted(set(targets) | set(current_value))
    for symbol in symbols:
        price = prices.get(symbol)
        if not price or price <= 0:
            continue
        target_value = equity * float(targets.get(symbol, 0.0))
        current = float(current_value.get(symbol, 0.0))
        delta = target_value - current
        if abs(delta) < max(cost.min_trade_weight * equity, band * equity):
            continue
        shares = int(abs(delta) // (price * cost.lot_size) * cost.lot_size)
        if shares <= 0:
            continue
        orders.append({
            "symbol": symbol,
            "side": "SELL" if delta < 0 else "BUY",
            "shares": shares,
            "ref_price": price,
            "amount": shares * price,
            "target_weight": float(targets.get(symbol, 0.0)),
        })
    orders.sort(key=lambda o: (o["side"] != "SELL", o["symbol"]))
    return orders


def run_etf_backtest(
    market: MarketData,
    etf: EtfConfig = EtfConfig(),
    cost: EtfCostConfig = EtfCostConfig(),
    benchmark_symbol: str = "510300",
) -> EtfBacktestResult:
    """Monthly rebalanced backtest with next-open execution and explicit costs."""
    warmup = cost.warmup_bars if cost.warmup_bars is not None else etf.trend_lookback
    all_dates = market.dates
    dates = [d for d in all_dates
             if (cost.start is None or d >= cost.start) and (cost.end is None or d <= cost.end)]
    if len(dates) < warmup + 20:
        raise ValueError("not enough dates for the ETF backtest (check --start / warmup)")

    cash = cost.initial_cash
    positions: Dict[str, int] = {}
    trades: List[EtfTrade] = []
    equity_curve: List[float] = []
    benchmark_curve: List[float] = []
    turnover: List[float] = []
    gross_turnover: List[float] = []
    target_history: Dict[str, Dict[str, float]] = {}
    warnings: List[str] = []
    benchmark_value = cost.initial_cash
    pending: Optional[Tuple[str, Dict[str, float]]] = None
    dropped_sleeves: Dict[str, str] = {}

    def snapshot(date: str) -> Dict[str, Bar]:
        return market.snapshot(date, apply_membership=False)

    for idx, cur in enumerate(dates):
        bars = snapshot(cur)
        # ---- 1) execute yesterday's signal at today's open -------------------
        if pending is not None:
            signal_date, targets = pending
            open_prices = {s: b.open for s, b in bars.items()
                           if s in set(list(positions) + list(targets)) and b.open > 0 and b.quote_fresh()}
            equity_open = cash + sum(sh * open_prices.get(s, 0.0)
                                     for s, sh in positions.items() if s in open_prices)
            if equity_open > 0 and open_prices:
                current_value = {s: sh * open_prices[s] for s, sh in positions.items() if s in open_prices}
                effective, dropped = apply_lot_constraints(
                    equity_open, targets, open_prices, cost, max_lot_fraction=cost.max_lot_fraction,
                    max_weight=etf.max_weight)
                dropped_sleeves.update(dropped)
                orders = plan_orders(equity_open, effective, current_value, open_prices, cost)
                traded = 0.0
                for order in orders:
                    symbol = order["symbol"]
                    bar = bars.get(symbol)
                    if bar is None:
                        continue
                    px = open_prices[symbol]
                    if order["side"] == "SELL":
                        if not bar.can_sell():
                            continue
                        qty = min(order["shares"], positions.get(symbol, 0))
                        if qty <= 0:
                            continue
                        gross = qty * px
                        bps = total_slippage_bps(gross, bar.close * bar.volume, cost.slippage_bps,
                                                 cost.impact_coefficient, cost.participation_cap)
                        slip = gross * bps / 10000.0
                        commission = max(cost.min_commission, gross * cost.commission_bps / 10000.0)
                        stamp = gross * cost.stamp_duty_bps / 10000.0
                        cash += gross - slip - commission - stamp
                        positions[symbol] = positions.get(symbol, 0) - qty
                        if positions[symbol] <= 0:
                            positions.pop(symbol, None)
                        traded += gross
                        trades.append(EtfTrade(cur, signal_date, symbol, "sell", qty, px, gross,
                                               commission, slip, cash))
                    else:
                        if not bar.can_buy():
                            continue
                        # cash-constrained sizing, lot by lot
                        qty = order["shares"]
                        bps = total_slippage_bps(qty * px, bar.close * bar.volume, cost.slippage_bps,
                                                 cost.impact_coefficient, cost.participation_cap)
                        trade_px = px * (1.0 + bps / 10000.0)
                        while qty > 0:
                            gross = qty * trade_px
                            commission = max(cost.min_commission, gross * cost.commission_bps / 10000.0)
                            if gross + commission <= cash:
                                break
                            qty -= cost.lot_size
                        if qty <= 0:
                            continue
                        gross = qty * trade_px
                        commission = max(cost.min_commission, gross * cost.commission_bps / 10000.0)
                        slip = qty * px * bps / 10000.0
                        cash -= gross + commission
                        positions[symbol] = positions.get(symbol, 0) + qty
                        traded += gross
                        trades.append(EtfTrade(cur, signal_date, symbol, "buy", qty, trade_px, gross,
                                               commission, slip, cash))
                turnover.append(0.5 * traded / equity_open)
                gross_turnover.append(traded / equity_open)
            pending = None

        # ---- 2) mark to market ---------------------------------------------
        close_equity = cash
        for symbol, shares in positions.items():
            bar = bars.get(symbol)
            px = bar.close if bar is not None and bar.close > 0 else 0.0
            close_equity += shares * px
        equity_curve.append(close_equity)

        if idx > 0 and benchmark_symbol:
            prev_bars, cur_bars = snapshot(dates[idx - 1]), bars
            a, b = prev_bars.get(benchmark_symbol), cur_bars.get(benchmark_symbol)
            if a and b and a.close > 0 and b.close > 0 and a.quote_fresh() and b.quote_fresh():
                benchmark_value *= 1.0 + (b.close / a.close - 1.0)
        benchmark_curve.append(benchmark_value)

        # ---- 3) signal on the close, execute next session ------------------
        nxt = dates[idx + 1] if idx + 1 < len(dates) else None
        if nxt is not None and (cur[:7] != nxt[:7]):
            if idx + 1 >= warmup:
                targets = sleeve_targets(market, cur, etf)
                if targets:
                    pending = (cur, targets)
                    target_history[cur] = dict(targets)

    metrics = performance_metrics(equity_curve, benchmark=benchmark_curve,
                                  turnover=turnover, gross_turnover=gross_turnover)
    metrics["years"] = len(dates) / 252.0
    if dropped_sleeves:
        warnings.append(
            "sleeves skipped for lot size: " +
            "; ".join(f"{s} ({why})" for s, why in sorted(dropped_sleeves.items()))
        )
    if not any(turnover):
        warnings.append("no rebalance executed; check the warmup window and start date")
    return EtfBacktestResult(
        dates=dates, equity_curve=equity_curve, benchmark_curve=benchmark_curve, trades=trades,
        turnover=turnover, gross_turnover=gross_turnover, target_history=target_history,
        metrics=metrics, warnings=warnings,
    )
