from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.domain import Bar, MarketData, Position, Trade
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec
from stockquant.factors.engine import FactorDiagnostics
from stockquant.portfolio import PortfolioConstraints, build_target_weights, target_volatility_scale
from stockquant.portfolio.risk import market_trend_scale
from stockquant.backtest.costs import total_slippage_bps
from stockquant.backtest.metrics import performance_metrics


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    commission_bps: float = 3.0
    stamp_duty_bps: float = 5.0
    slippage_bps: float = 5.0
    impact_coefficient: float = 25.0
    participation_cap: float = 0.10
    use_volume_impact: bool = True
    min_commission: float = 5.0
    lot_size: int = 100
    rebalance: str = "monthly"  # monthly, weekly, daily
    start: Optional[str] = None
    end: Optional[str] = None
    min_history: int = 80
    sector_neutral: bool = True
    target_volatility: Optional[float] = 0.18
    vol_method: str = "realized"  # realized | garch
    market_filter: str = "none"  # none or ma
    market_filter_lookback: int = 200
    risk_off_exposure: float = 0.5
    alpha_weight: float = 1.0
    rebalance_band: float = 0.02
    min_trade_weight: float = 0.005
    constraints: PortfolioConstraints = field(default_factory=PortfolioConstraints)
    factors: Tuple[FactorSpec, ...] = DEFAULT_FACTORS
    min_market_cap_quantile: float = 0.0
    orthogonalize: bool = False
    impute_missing: bool = False
    # Factor timing: scale exposure by the *realized* recent cross-sectional IC
    # of the composite score (factor momentum, no look-ahead). Off by default
    # so every existing result is unchanged unless explicitly enabled.
    factor_timing: bool = False
    factor_timing_lookback: int = 3
    factor_timing_risk_off: float = 0.5
    # Portfolio-level drawdown circuit breaker: once the running drawdown
    # exceeds ``portfolio_drawdown_limit`` (e.g. 0.12 = 12%), scale new
    # exposure by ``portfolio_drawdown_exposure``.
    portfolio_drawdown_limit: Optional[float] = None
    portfolio_drawdown_exposure: float = 0.5
    # Index-enhancement beta control: pull the portfolio beta toward
    # ``target_beta`` (e.g. 1.0) so a defensive factor tilt does not
    # become a low-beta, index-lagging bet.
    target_beta: Optional[float] = None
    beta_lambda: float = 0.0
    # Benchmark weighting for the 'excess return' yardstick.
    # 'equal' = equal-weight members; 'cap' = free-float/market-cap
    # weighted (the correct yardstick for index enhancement).
    benchmark_method: str = "cap"
    benchmark_weight_cap: float = 0.02  # cap a single name's benchmark weight
    # PIT policy for announcement-derived factors ("off" | "session" | "strict").
    # See stockquant.factors.engine.pit_usable: a date-only availability stamp is
    # treated as "published after the close" and therefore usable from the *next*
    # session; an explicit timestamp is compared against the session close.
    pit_policy: str = "session"
    #: Neutralisation grouping: "sector" (current vendor labels, back-filled),
    #: "cap_decile" (point-in-time clean), "random:N" (placebo), "none".
    neutralize_by: str = "sector"
    # The risk model sees this multiple of ``max_positions`` before the final
    # position count is enforced, so a lower-score but diversifying name can win.
    # Default 1.0 preserves the published optimizer behaviour: widening the pool
    # to 3x *lowered* CAGR on the CSI300-30 sample (2.9% vs 7.0%), so it stays an
    # explicit, trial-registered research switch rather than a silent default.
    optimizer_candidate_multiplier: float = 1.0
    # Kelly sizing: share of investable capital deployed (the score->return map is
    # arbitrary, so this is the risk-budget dial rather than a textbook f_kelly).
    kelly_fraction: float = 0.5
    # Turnover control: reject a target list whose one-way turnover exceeds this
    # (relative to current weights). 1.0 = unlimited.
    max_one_way_turnover: float = 1.0
    # Fail loudly when execution prices are corporate-action adjusted: cash
    # dividends / splits are not credited in this engine yet.
    warn_on_adjusted_execution: bool = True


@dataclass
class BacktestResult:
    config: BacktestConfig
    dates: List[str]
    equity_curve: List[float]
    benchmark_curve: List[float]
    trades: List[Trade]
    #: One-way turnover per rebalance (0.5 * sum|target - current|).
    turnover: List[float]
    #: Gross traded notional over opening equity (buys + sells), same order.
    gross_turnover: List[float]
    target_history: Dict[str, Dict[str, float]]
    signal_history: Dict[str, Dict[str, float]]
    diagnostics: List[FactorDiagnostics]
    metrics: Dict[str, float]
    warnings: List[str] = field(default_factory=list)
    timing: Dict[str, float] = field(default_factory=dict)
    #: Risk-overlay diagnostics per rebalance: realized-vol estimates and the
    #: source used to scale exposure ("portfolio" vs "universe").
    risk: Dict[str, float] = field(default_factory=dict)


class BacktestEngine:
    def __init__(self, market: MarketData, config: Optional[BacktestConfig] = None):
        self.market = market
        self.config = config or BacktestConfig()
        self.factor_engine = FactorEngine(
            factors=self.config.factors,
            min_history=self.config.min_history,
            sector_neutral=self.config.sector_neutral,
            min_market_cap_quantile=self.config.min_market_cap_quantile,
            orthogonalize=self.config.orthogonalize,
            impute_missing=self.config.impute_missing,
            pit_policy=self.config.pit_policy,
            neutralize_by=self.config.neutralize_by,
        )

    def _rebalance_due(self, cur: str, nxt: Optional[str]) -> bool:
        if nxt is None:
            return False
        mode = self.config.rebalance.lower()
        if mode == "daily":
            return True
        if mode == "weekly":
            a = Date.fromisoformat(cur).isocalendar()[:2]
            b = Date.fromisoformat(nxt).isocalendar()[:2]
            return a != b
        if mode == "monthly":
            return cur[:7] != nxt[:7]
        if mode == "quarterly":
            return (int(cur[5:7]) - 1) // 3 != (int(nxt[5:7]) - 1) // 3 or cur[:4] != nxt[:4]
        if mode == "semiannual":
            return (int(cur[5:7]) - 1) // 6 != (int(nxt[5:7]) - 1) // 6 or cur[:4] != nxt[:4]
        if mode == "annual":
            return cur[:4] != nxt[:4]
        raise ValueError(f"Unknown rebalance mode: {self.config.rebalance}")

    def _price(self, bar: Optional[Bar], use_open: bool) -> Optional[float]:
        if bar is None or bar.suspended:
            return None
        px = bar.open if use_open else bar.close
        if px is None or px <= 0:
            return None
        return px

    def _total_slippage_bps(self, order_notional: float, bar: Bar, px: float) -> float:
        daily_notional = bar.volume * px if px > 0 else 0.0
        return total_slippage_bps(
            order_notional=order_notional,
            daily_notional=daily_notional,
            slippage_bps=self.config.slippage_bps,
            impact_coefficient=self.config.impact_coefficient,
            participation_cap=self.config.participation_cap,
            use_volume_impact=self.config.use_volume_impact,
        )

    def _execute_targets(
        self,
        exec_date: str,
        signal_date: str,
        targets: Dict[str, float],
        cash: float,
        positions: Dict[str, Position],
        trades: List[Trade],
    ) -> Tuple[float, float, float]:
        snapshot = self.market.snapshot(exec_date)
        open_prices: Dict[str, float] = {}
        for symbol in set(list(positions) + list(targets)):
            bar = snapshot.get(symbol)
            px = self._price(bar, use_open=True)
            if px is not None:
                open_prices[symbol] = px
        if not open_prices:
            return cash, 0.0, 0.0

        equity_open = cash + sum(
            pos.shares * open_prices.get(symbol, pos.avg_cost)
            for symbol, pos in positions.items()
        )
        if equity_open <= 0:
            return cash, 0.0, 0.0

        # Apply a no-trade band to existing positions. This reduces noise and costs.
        effective_targets = dict(targets)
        for symbol, pos in positions.items():
            px = open_prices.get(symbol)
            if px is None or pos.shares <= 0 or equity_open <= 0:
                continue
            current_weight = pos.shares * px / equity_open
            target_weight = effective_targets.get(symbol, 0.0)
            if symbol in effective_targets and abs(current_weight - target_weight) < self.config.rebalance_band:
                effective_targets[symbol] = current_weight
        targets = {
            s: w for s, w in effective_targets.items()
            if w >= self.config.min_trade_weight or s in positions
        }

        desired_shares: Dict[str, int] = {}
        for symbol, weight in targets.items():
            px = open_prices.get(symbol)
            if px is None:
                continue
            raw_shares = int((equity_open * max(0.0, weight)) // (px * self.config.lot_size) * self.config.lot_size)
            desired_shares[symbol] = max(0, raw_shares)

        turnover_amount = 0.0

        # Sell first. This reduces cash constraints and approximates normal rebalancing.
        for symbol in list(positions):
            pos = positions[symbol]
            bar = snapshot.get(symbol)
            px = open_prices.get(symbol)
            target_qty = desired_shares.get(symbol, 0)
            sell_qty = pos.shares - target_qty
            if sell_qty <= 0 or px is None or bar is None:
                continue
            # Fail closed: limit-down, suspended and zero-volume bars cannot be
            # assumed to fill.  (ST names stay sellable on purpose.)
            if not bar.can_sell():
                continue
            if pos.last_buy_date >= exec_date:  # A-share T+1 guard.
                continue
            gross = sell_qty * px
            slippage_bps = self._total_slippage_bps(gross, bar, px)
            slippage_cost = gross * slippage_bps / 10000.0
            commission = max(self.config.min_commission, gross * self.config.commission_bps / 10000.0)
            stamp = gross * self.config.stamp_duty_bps / 10000.0
            cash += gross - slippage_cost - commission - stamp
            turnover_amount += gross
            trades.append(
                Trade(
                    date=exec_date,
                    signal_date=signal_date,
                    symbol=symbol,
                    side="sell",
                    shares=sell_qty,
                    price=px,
                    gross_amount=gross,
                    commission=commission,
                    stamp_duty=stamp,
                    slippage_cost=slippage_cost,
                    cash_after=cash,
                    reason="rebalance",
                )
            )
            pos.shares -= sell_qty
            if pos.shares <= 0:
                del positions[symbol]

        # Buy targets, highest target weight first.
        for symbol, target_weight in sorted(targets.items(), key=lambda kv: kv[1], reverse=True):
            bar = snapshot.get(symbol)
            px = open_prices.get(symbol)
            if bar is None or px is None or not bar.can_buy():
                continue
            target_qty = desired_shares.get(symbol, 0)
            current_qty = positions.get(symbol).shares if symbol in positions else 0
            buy_qty = target_qty - current_qty
            if buy_qty <= 0:
                continue
            slippage_bps = self._total_slippage_bps(buy_qty * px, bar, px)
            trade_px = px * (1.0 + slippage_bps / 10000.0)
            # Respect available cash including commission. Iterate down by lots if needed.
            while buy_qty > 0:
                gross = buy_qty * trade_px
                commission = max(self.config.min_commission, gross * self.config.commission_bps / 10000.0)
                if gross + commission <= cash:
                    break
                buy_qty -= self.config.lot_size
            if buy_qty <= 0:
                continue
            slippage_bps = self._total_slippage_bps(buy_qty * px, bar, px)
            trade_px = px * (1.0 + slippage_bps / 10000.0)
            gross = buy_qty * trade_px
            commission = max(self.config.min_commission, gross * self.config.commission_bps / 10000.0)
            slippage_cost = buy_qty * px * slippage_bps / 10000.0
            cash -= gross + commission
            turnover_amount += gross
            if symbol in positions:
                pos = positions[symbol]
                total_cost = pos.avg_cost * pos.shares + gross
                pos.shares += buy_qty
                pos.avg_cost = total_cost / pos.shares if pos.shares else 0.0
                pos.last_buy_date = exec_date
            else:
                positions[symbol] = Position(
                    symbol=symbol,
                    shares=buy_qty,
                    avg_cost=trade_px,
                    entry_date=exec_date,
                    last_buy_date=exec_date,
                )
            trades.append(
                Trade(
                    date=exec_date,
                    signal_date=signal_date,
                    symbol=symbol,
                    side="buy",
                    shares=buy_qty,
                    price=trade_px,
                    gross_amount=gross,
                    commission=commission,
                    stamp_duty=0.0,
                    slippage_cost=slippage_cost,
                    cash_after=cash,
                    reason="rebalance",
                )
            )

        gross_turnover = turnover_amount / equity_open if equity_open > 0 else 0.0
        # One-way turnover mirrors the optimizer's budget definition
        # (0.5 * sum|target - current|); the gross traded notional is reported
        # separately so the two conventions can never be confused again.
        return cash, 0.5 * gross_turnover, gross_turnover

    def _realized_ic(self, signal_date: str, cur_date: str, scores: Dict[str, float]) -> Optional[float]:
        """Cross-sectional IC between a *past* signal and the return realized up
        to ``cur_date``. Uses only information available at ``cur_date`` (no
        look-ahead), so it is safe to drive factor timing."""
        from stockquant.analysis.ic import spearman
        xs: List[float] = []
        ys: List[float] = []
        for symbol, score in scores.items():
            a = self.market.bar(symbol, signal_date)
            b = self.market.bar(symbol, cur_date)
            if a is None or b is None or a.close <= 0 or b.close <= 0 or a.suspended or b.suspended:
                continue
            xs.append(float(score))
            ys.append(b.close / a.close - 1.0)
        return spearman(xs, ys)

    def _factor_timing_scale(
        self,
        cur_date: str,
        signal_history: Dict[str, Dict[str, float]],
        timing_history: List[float],
    ) -> float:
        """1.0 when recent realized IC is positive, else the risk-off fraction."""
        if not self.config.factor_timing:
            return 1.0
        prev_dates = sorted(signal_history)
        if prev_dates:
            s_prev = prev_dates[-1]
            ic = self._realized_ic(s_prev, cur_date, signal_history[s_prev])
            if ic is not None and math.isfinite(ic):
                timing_history.append(float(ic))
        lookback = max(1, int(self.config.factor_timing_lookback))
        recent = timing_history[-lookback:]
        if not recent:
            return 1.0
        mean_ic = statistics.mean(recent)
        return 1.0 if mean_ic > 0 else float(self.config.factor_timing_risk_off)

    @staticmethod
    def _cap_weights(weights: Dict[str, float], cap: float, iters: int = 25) -> Dict[str, float]:
        """Cap a single name's weight and redistribute.

        Contract: ``weights`` must be **normalised** (sum ~ 1).  Passing raw
        market caps silently degenerates into equal weight, because every name
        then exceeds the cap and gets clipped to the same value.
        """
        w = dict(weights)
        if cap <= 0 or not w:
            return w
        for _ in range(iters):
            over = {k: v for k, v in w.items() if v > cap + 1e-15}
            if not over:
                break
            excess = sum(v - cap for v in over.values())
            for k in over:
                w[k] = cap
            room = {k: v for k, v in w.items() if v < cap - 1e-15}
            rt = sum(cap - v for v in room.values())
            if rt <= 1e-15:
                break
            for k, v in room.items():
                w[k] = v + excess * (cap - v) / rt
        tot = sum(w.values())
        return {k: v / tot for k, v in w.items()} if tot > 0 else w

    def _benchmark_return(self, prev_date: str, cur_date: str) -> float:
        """Benchmark return of the (membership-filtered) universe.

        ``cap`` weights by market cap so the yardstick matches a real index; the
        old equal-weight benchmark embeds a size tilt + daily-rebalancing premium
        that a low-beta enhancement portfolio can never beat.
        """
        prev = self.market.snapshot(prev_date)
        cur = self.market.snapshot(cur_date)
        cap_mode = self.config.benchmark_method == "cap"
        pairs: List[Tuple[float, float]] = []
        for symbol, a in prev.items():
            b = cur.get(symbol)
            if not (b and a.close > 0 and b.close > 0 and a.quote_fresh() and b.quote_fresh()):
                continue
            if cap_mode:
                if a.market_cap <= 0:
                    # A market-cap benchmark must not silently substitute traded
                    # notional (close * volume) for market cap: that is a
                    # liquidity index, not a cap index, and it changes the
                    # excess-return yardstick.  Drop the name and report it.
                    self._benchmark_missing_cap += 1
                    continue
                pairs.append((b.close / a.close - 1.0, a.market_cap))
            else:
                pairs.append((b.close / a.close - 1.0, 1.0))
        if not pairs:
            return 0.0
        rets = [r for r, _ in pairs]
        weights = [w for _, w in pairs]
        total = sum(weights)
        if total <= 0:
            return 0.0
        if cap_mode:
            # Normalise *before* applying the single-name cap: ``_cap_weights``
            # expects weights in (0, 1].  Feeding it raw market caps (yuan) made
            # every name look "over the cap", so each one was clipped to the same
            # 2% and renormalised -- i.e. benchmark_method="cap" silently produced
            # an **equal-weight** benchmark all along, which is a different (and
            # in A-shares a materially higher) yardstick.
            weight_sum = sum(weights) or 1.0
            wmap = {str(i): w / weight_sum for i, w in enumerate(weights)}
            capped = self._cap_weights(wmap, self.config.benchmark_weight_cap)
            weights = [capped[str(i)] for i in range(len(weights))]
            total = sum(weights)
        return sum(r * w for r, w in zip(rets, weights)) / total

    def run(self) -> BacktestResult:
        all_dates = self.market.dates
        dates = [
            d for d in all_dates
            if (self.config.start is None or d >= self.config.start)
            and (self.config.end is None or d <= self.config.end)
        ]
        if not dates:
            raise ValueError("No dates in the requested backtest range")


        cash = self.config.initial_cash
        positions: Dict[str, Position] = {}
        trades: List[Trade] = []
        equity_curve: List[float] = []
        benchmark_curve: List[float] = []
        # ``turnover`` is one-way (the optimizer's convention); the gross traded
        # notional is tracked separately so cost models and reports agree.
        turnover: List[float] = []
        gross_turnover: List[float] = []
        risk_log: Dict[str, float] = {}
        vol_ests: List[float] = []
        vol_scales: List[float] = []
        vol_portfolio_source = 0
        vol_observations = 0
        self._benchmark_missing_cap = 0
        adjusted_execution_dates = 0
        gated_unverified = 0
        target_history: Dict[str, Dict[str, float]] = {}
        signal_history: Dict[str, Dict[str, float]] = {}
        diagnostics: List[FactorDiagnostics] = []
        warnings: List[str] = []
        pending: Optional[Tuple[str, Dict[str, float]]] = None
        benchmark_value = self.config.initial_cash
        timing_history: List[float] = []
        timing_by_date: Dict[str, float] = {}
        # Index membership must cover the requested window: before the first
        # snapshot the constituent list is unknown, and the old loader back-filled
        # the first future snapshot into the past.
        uncovered = self.market.uncovered_membership_dates(dates)
        if uncovered:
            warnings.append(
                f"{len(uncovered)} of {len(dates)} sessions precede the first index-membership "
                f"snapshot ({uncovered[0]} -> {uncovered[-1]}); those sessions have an empty "
                "universe (fail-closed) instead of the first future constituent list"
            )

        for idx, cur in enumerate(dates):
            if pending is not None:
                signal_date, targets = pending
                cash, turn, gross = self._execute_targets(
                    exec_date=cur,
                    signal_date=signal_date,
                    targets=targets,
                    cash=cash,
                    positions=positions,
                    trades=trades,
                )
                turnover.append(turn)
                gross_turnover.append(gross)
                pending = None

            snapshot = self.market.snapshot(cur)
            close_equity = cash
            for symbol, pos in positions.items():
                bar = snapshot.get(symbol)
                px = bar.close if bar and bar.close > 0 else pos.avg_cost
                close_equity += pos.shares * px
            equity_curve.append(close_equity)

            if idx > 0:
                benchmark_value *= 1.0 + self._benchmark_return(dates[idx - 1], cur)
            benchmark_curve.append(benchmark_value)

            nxt = dates[idx + 1] if idx + 1 < len(dates) else None
            if self._rebalance_due(cur, nxt):
                scores, diag = self.factor_engine.score(self.market, cur)
                diagnostics.append(diag)
                if scores:
                    # Current weights (used for the turnover cap).
                    total_equity = cash + sum(
                        pos.shares * (snapshot.get(sym).close if snapshot.get(sym) and snapshot.get(sym).close > 0 else pos.avg_cost)
                        for sym, pos in positions.items()
                    )
                    current_weights = {}
                    if total_equity > 0:
                        for sym, pos in positions.items():
                            px = snapshot.get(sym).close if snapshot.get(sym) and snapshot.get(sym).close > 0 else pos.avg_cost
                            current_weights[sym] = pos.shares * px / total_equity
                    scheme = self.config.constraints.weight_scheme
                    if scheme == "optimize":
                        from stockquant.portfolio.optimizer import optimize_target_weights
                        betas = None
                        if self.config.target_beta is not None and self.config.beta_lambda > 0:
                            from stockquant.portfolio.optimizer import estimate_betas
                            betas = estimate_betas(self.market, list(snapshot), cur)
                        targets = optimize_target_weights(
                            self.market, scores, snapshot, cur,
                            self.config.constraints, current_weights=current_weights,
                            cvar_alpha=self.config.constraints.cvar_alpha,
                            cvar_lambda=self.config.constraints.cvar_lambda,
                            betas=betas,
                            target_beta=self.config.target_beta,
                            beta_lambda=self.config.beta_lambda,
                            candidate_multiplier=self.config.optimizer_candidate_multiplier,
                        )
                    elif scheme == "relative":
                        from stockquant.portfolio.constructor import build_relative_weights
                        if self.config.benchmark_method == "cap":
                            cap_tot = sum((snapshot[s].market_cap for s in snapshot if snapshot[s].market_cap > 0), 0.0) or 1.0
                            raw_bw = {s: snapshot[s].market_cap / cap_tot for s in snapshot if snapshot[s].market_cap > 0}
                            bench_w = self._cap_weights(raw_bw, self.config.benchmark_weight_cap)
                        else:
                            bench_w = {s: 1.0 / len(snapshot) for s in snapshot} if snapshot else {}
                        rel_betas = None
                        if self.config.target_beta is not None:
                            from stockquant.portfolio.optimizer import estimate_betas
                            rel_betas = estimate_betas(self.market, list(bench_w), cur)
                        targets = build_relative_weights(
                            scores, snapshot, self.config.constraints, bench_w,
                            betas=rel_betas,
                            target_active_beta=(0.0 if self.config.target_beta is not None else None),
                        )
                    elif scheme == "enhanced":
                        from stockquant.portfolio.constructor import build_enhanced_weights
                        targets = build_enhanced_weights(
                            scores, snapshot, self.config.constraints,
                        )
                    elif scheme == "kelly":
                        from stockquant.portfolio.sizing import kelly_weights
                        kelly = kelly_weights(
                            self.market, scores, list(snapshot), cur,
                            fraction=self.config.kelly_fraction,
                            cap=self.config.constraints.max_weight,
                            investable=max(0.0, 1.0 - self.config.constraints.cash_buffer),
                        )
                        targets = kelly or build_target_weights(
                            scores, snapshot, self.config.constraints, current_weights=current_weights
                        )
                    else:
                        targets = build_target_weights(
                            scores, snapshot, self.config.constraints, current_weights=current_weights
                        )
                    if self.config.alpha_weight < 1.0 and snapshot:
                        index_constraints = PortfolioConstraints(
                            max_positions=len(snapshot),
                            max_weight=0.08,
                            sector_max_weight=1.0,
                            cash_buffer=self.config.constraints.cash_buffer,
                            min_score=-1e9,
                            weight_scheme="equal",
                        )
                        base = build_target_weights({s: 1.0 for s in snapshot}, snapshot, index_constraints)
                        alpha_w = max(0.0, min(1.0, self.config.alpha_weight))
                        symbols = set(targets) | set(base)
                        targets = {
                            s: alpha_w * targets.get(s, 0.0) + (1.0 - alpha_w) * base.get(s, 0.0)
                            for s in symbols
                        }
                    scale, est_vol, vol_source = target_volatility_scale(
                        self.market,
                        cur,
                        self.config.target_volatility,
                        weights=targets,
                        lookback=20,
                        method=self.config.vol_method,
                    )
                    vol_observations += 1
                    if vol_source == "portfolio":
                        vol_portfolio_source += 1
                    if est_vol:
                        vol_ests.append(est_vol)
                    vol_scales.append(scale)
                    if self.config.market_filter == "ma":
                        scale *= market_trend_scale(
                            self.market,
                            cur,
                            lookback=self.config.market_filter_lookback,
                            risk_off_exposure=self.config.risk_off_exposure,
                        )
                    timing_scale = self._factor_timing_scale(cur, signal_history, timing_history)
                    scale *= timing_scale
                    timing_by_date[cur] = timing_scale
                    if self.config.portfolio_drawdown_limit:
                        peak = max(equity_curve) if equity_curve else self.config.initial_cash
                        dd = (equity_curve[-1] / peak - 1.0) if peak > 0 else 0.0
                        if dd <= -abs(self.config.portfolio_drawdown_limit):
                            scale *= float(self.config.portfolio_drawdown_exposure)
                    targets = {s: w * scale for s, w in targets.items()}
                    diag = diagnostics[-1] if diagnostics else None
                    if diag is not None:
                        gated_unverified += diag.pit_unverified
                    if self.config.warn_on_adjusted_execution:
                        for symbol in targets:
                            bar = snapshot.get(symbol)
                            if bar is not None and bar.raw_close > 0 and bar.close > 0:
                                if abs(bar.raw_close / bar.close - 1.0) > 0.02:
                                    adjusted_execution_dates += 1
                                    break
                    pending = (cur, targets)
                    target_history[cur] = dict(targets)
                    signal_history[cur] = dict(scores)
                elif idx >= self.config.min_history + 5:
                    warnings.append(f"No factor scores available on {cur}")

        if self._benchmark_missing_cap:
            warnings.append(
                f"benchmark_method='cap' dropped {self._benchmark_missing_cap} rows without a "
                "market cap; a liquidity proxy is never substituted for market cap"
            )
        if adjusted_execution_dates:
            warnings.append(
                f"{adjusted_execution_dates} rebalances executed on corporate-action adjusted "
                "prices that differ from raw_close by >2%: cash dividends / splits are not "
                "credited to the cash ledger, so treat those results as approximate"
            )
        if gated_unverified:
            warnings.append(
                f"{gated_unverified} bars carried announcement fields without an availability "
                "stamp; they were used as-is (pit_policy='session'). Use --pit-policy strict "
                "to refuse such datasets, or re-fetch with fundamentals_available_at"
            )
        if vol_observations:
            risk_log["volatility_observations"] = float(vol_observations)
            risk_log["volatility_source_portfolio_share"] = vol_portfolio_source / vol_observations
            if vol_ests:
                risk_log["estimated_volatility_mean"] = statistics.mean(vol_ests)
                risk_log["estimated_volatility_last"] = vol_ests[-1]
            risk_log["volatility_scale_mean"] = statistics.mean(vol_scales)
            risk_log["volatility_scale_min"] = min(vol_scales)

        factor_errors = sum(sum((d.error_by_factor or {}).values()) for d in diagnostics)
        if factor_errors:
            warnings.append(f"{factor_errors} factor evaluations raised; see FactorDiagnostics.error_by_factor")

        metrics = performance_metrics(
            equity_curve,
            benchmark=benchmark_curve,
            turnover=turnover,
            gross_turnover=gross_turnover,
            risk_free_rate=0.0,
        )
        return BacktestResult(
            config=self.config,
            dates=dates,
            equity_curve=equity_curve,
            benchmark_curve=benchmark_curve,
            trades=trades,
            turnover=turnover,
            gross_turnover=gross_turnover,
            target_history=target_history,
            signal_history=signal_history,
            diagnostics=diagnostics,
            metrics=metrics,
            warnings=warnings,
            timing=timing_by_date,
            risk=risk_log,
        )
