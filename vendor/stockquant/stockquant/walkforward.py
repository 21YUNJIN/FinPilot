from __future__ import annotations

import dataclasses
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from stockquant.analysis import analyze_factor_ic
from stockquant.analysis.linear import ridge_factor_weights
from stockquant.analysis.pbo import probability_of_backtest_overfitting
from stockquant.analysis.trackrecord import track_record_report
from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.backtest.metrics import daily_returns, performance_metrics
from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorSpec

FACTOR_NAMES = ("value", "quality", "momentum", "low_vol", "reversal", "liquidity")


def with_weights(factors: Sequence[FactorSpec], weights: Dict[str, float]) -> Tuple[FactorSpec, ...]:
    """Apply a candidate weight vector to the factor set.

    Factors absent from ``weights`` are zeroed out (not left at their default
    weight).  Without this, a 6-factor preset such as ``balanced`` would
    silently retain the default weights of ``growth``/``forecast``/``holder``/
    ``gross_margin`` and become an undocumented 10-factor blend.
    """
    return tuple(
        FactorSpec(f.name, weights.get(f.name, 0.0), f.fn, f.description)
        for f in factors
    )


#: Pre-registered *frozen* weight vectors: no per-window selection at all.
#: ``default`` is the shipped DEFAULT_FACTORS weighting (i.e. "freeze what we
#: already have"), which is the honest control for the selection machinery: the
#: 2026-10-16 walk-forward showed that selecting on training-window Sharpe cost
#: ~5pp/yr versus simply freezing these weights.
FROZEN_MODELS: Dict[str, Dict[str, float]] = {
    "default": {f.name: f.weight for f in DEFAULT_FACTORS},
}

CANDIDATES: Dict[str, Dict[str, float]] = {
    "equal": {f.name: 1.0 / len(DEFAULT_FACTORS) for f in DEFAULT_FACTORS},
    "balanced": {
        "value": 0.20,
        "quality": 0.20,
        "momentum": 0.30,
        "low_vol": 0.15,
        "reversal": 0.05,
        "liquidity": 0.10,
    },
    "momentum": {
        "value": 0.10,
        "quality": 0.15,
        "momentum": 0.45,
        "low_vol": 0.15,
        "reversal": 0.05,
        "liquidity": 0.10,
    },
    "defensive": {
        "value": 0.25,
        "quality": 0.30,
        "momentum": 0.10,
        "low_vol": 0.25,
        "reversal": 0.00,
        "liquidity": 0.10,
    },
    "value_quality": {
        "value": 0.35,
        "quality": 0.35,
        "momentum": 0.10,
        "low_vol": 0.10,
        "reversal": 0.00,
        "liquidity": 0.10,
    },

    # 价值 + 低波动 Smart Beta：国内券商与 MSCI China A 因子指数实证显示，
    # 价值/红利/低波风格在沪深300上有较强超额确定性与防御性。
    "value_low_vol": {
        "value": 0.35,
        "quality": 0.15,
        "momentum": 0.10,
        "low_vol": 0.35,
        "reversal": 0.05,
        "liquidity": 0.00,
    },


    "forecast_quality": {
        "value": 0.00,
        "quality": 0.20,
        "momentum": 0.10,
        "low_vol": 0.10,
        "reversal": 0.00,
        "liquidity": 0.10,
        "growth": 0.20,
        "gross_margin": 0.00,
        "forecast": 0.30,
        "holder": 0.00,
    },
    "quality_growth": {
        "value": 0.15,
        "quality": 0.25,
        "momentum": 0.00,
        "low_vol": 0.15,
        "reversal": 0.15,
        "liquidity": 0.00,
        "growth": 0.25,
        "gross_margin": 0.05,
    },
}


@dataclass
class WindowResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    selected: str
    train_sharpe: float
    test_metrics: Dict[str, float]
    test_dates: List[str]
    test_equity: List[float]
    test_benchmark: List[float]
    trades: int
    test_candidate_sharpes: Dict[str, float] = field(default_factory=dict)
    test_candidate_returns: Dict[str, List[float]] = field(default_factory=dict)


@dataclass
class WalkForwardResult:
    windows: List[WindowResult]
    oos_metrics: Dict[str, float]
    oos_dates: List[str]
    oos_equity: List[float]
    oos_benchmark: List[float]
    validation: Dict[str, object] = field(default_factory=dict)


class WalkForwardRunner:
    def __init__(
        self,
        market: MarketData,
        base_config: Optional[BacktestConfig] = None,
        train_days: int = 252,
        test_days: int = 63,
        step_days: int = 63,
        model: str = "select",
        ridge_lambda: float = 50.0,
        embargo_days: int = 25,
        purge_days: int = 0,
        label_horizon_days: int = 20,
        pbo_splits: int = 10,
        declared_trials: int = 0,
        selection_penalty: float = 0.0,
        shrink_to_equal: float = 0.0,
    ):
        # Extra trials the researcher already ran *outside* this walk-forward
        # (factor-weight searches, interval sweeps, earlier model variants).
        # The Deflated Sharpe needs the FULL search space, not just the
        # candidates this run happened to evaluate.
        self.declared_trials = max(0, int(declared_trials))
        self.trial_ledger: List[Dict[str, object]] = []
        self.market = market
        self.base_config = base_config or BacktestConfig()
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.model = model
        self.ridge_lambda = ridge_lambda
        self.embargo_days = max(0, embargo_days)
        self.purge_days = max(0, purge_days)
        self.label_horizon_days = max(0, label_horizon_days)
        self.pbo_splits = max(4, pbo_splits)
        # Selection criterion knobs (both pre-registered, both counted as trials):
        #   selection_penalty: score = train_sharpe - penalty * train_annual_turnover
        #   shrink_to_equal:   final = (1-f) * selected + f * equal weight
        # The default (0/0) reproduces the historical "pick the best train Sharpe"
        # rule so older results stay reproducible.
        self.selection_penalty = max(0.0, float(selection_penalty))
        self.warnings: List[str] = []
        self.shrink_to_equal = min(1.0, max(0.0, float(shrink_to_equal)))

    def _window_config(
        self,
        start: str,
        end: str,
        weights: Dict[str, float],
        alpha_weight: Optional[float] = None,
    ) -> BacktestConfig:
        """Per-window config = base config with only the window fields replaced.

        The old hand-written field list silently dropped every setting that was
        added later (``benchmark_method``, ``pit_policy``, ``kelly_fraction``, ...),
        so a walk-forward "run with --pit-policy strict" actually ran in the
        default mode.  ``dataclasses.replace`` keeps every field in sync by
        construction.
        """
        return dataclasses.replace(
            self.base_config,
            start=start,
            end=end,
            factors=with_weights(DEFAULT_FACTORS, weights),
            alpha_weight=self.base_config.alpha_weight if alpha_weight is None else alpha_weight,
        )

    def run(self) -> WalkForwardResult:
        all_dates = [
            d for d in self.market.dates
            if (self.base_config.start is None or d >= self.base_config.start)
            and (self.base_config.end is None or d <= self.base_config.end)
        ]
        warmup = self.base_config.min_history + 5
        if len(all_dates) < warmup + 80:
            raise ValueError("Not enough dates for walk-forward evaluation")
        isolation_days = max(self.embargo_days, self.label_horizon_days + self.purge_days)
        start_idx = warmup
        windows: List[WindowResult] = []
        trial_count = 0
        if self.model == "select":
            # Measured on CSI300 2018-06..2026-09: re-selecting weights every
            # window cost ~4.7pp/yr of OOS excess versus freezing the shipped
            # weights (see docs/STOCK_LINE_REVIEW_2026-10-16.md).  Keep the rule
            # available for research, but never let it run silently.
            self.warnings.append(
                "wf-model=select re-picks weights on every training window; on the CSI300 sample this "
                "cost ~4.7pp/yr of out-of-sample excess versus --wf-model default (frozen weights)."
            )
        while start_idx + self.train_days + isolation_days + self.test_days <= len(all_dates):
            train = all_dates[start_idx:start_idx + self.train_days]
            test_start_idx = start_idx + self.train_days + isolation_days
            test = all_dates[test_start_idx:test_start_idx + self.test_days]
            if not train or not test:
                break
            best_name = "balanced"
            best_sharpe = -math.inf
            best_score = -math.inf
            best_result = None
            best_weights = CANDIDATES["balanced"]
            candidates = dict(CANDIDATES)
            needs_ic = self.model in ("select", "ic", "ic_blend")
            needs_ridge = self.model in ("select", "ridge")
            ic_result = analyze_factor_ic(
                self.market,
                min_history=self.base_config.min_history,
                horizon=20,
                rebalance=self.base_config.rebalance,
                sector_neutral=self.base_config.sector_neutral,
                start_date=train[0],
                end_date=train[-1],
            )
            positive_ic = ({name: max(0.0, float(m.get("mean_ic", 0.0))) for name, m in ic_result.summary.items()}
                           if needs_ic else {})
            top_ic = sorted(positive_ic.values(), reverse=True)[:4]
            mean_top_ic = statistics.mean(top_ic) if top_ic else 0.0
            dynamic_alpha = (self.base_config.alpha_weight * min(1.0, max(0.0, mean_top_ic / 0.03))
                             if needs_ic else self.base_config.alpha_weight)
            test_alpha = dynamic_alpha if self.model == "ic_blend" else self.base_config.alpha_weight
            total_positive = sum(positive_ic.values())
            if total_positive > 0:
                positive_ic = {k: min(0.50, v / total_positive) for k, v in positive_ic.items()}
                total_capped = sum(positive_ic.values()) or 1.0
                candidates["ic_weighted"] = {k: v / total_capped for k, v in positive_ic.items()}
            ridge_weights = ridge_factor_weights(
                self.market,
                start=train[0],
                end=train[-1],
                min_history=self.base_config.min_history,
                horizon=20,
                rebalance=self.base_config.rebalance,
                sector_neutral=self.base_config.sector_neutral,
                ridge_lambda=self.ridge_lambda,
            ) if needs_ridge else {}
            if ridge_weights:
                candidates["ridge"] = ridge_weights
            if self.model in FROZEN_MODELS:
                best_name = self.model
                best_weights = FROZEN_MODELS[self.model]
                best_sharpe = 0.0
            elif self.model == "ic_blend":
                best_name = "balanced"
                best_weights = CANDIDATES["balanced"]
                best_sharpe = 0.0
            elif self.model == "ic" and "ic_weighted" in candidates:
                best_name = "ic_weighted"
                best_weights = candidates[best_name]
                best_sharpe = 0.0
            elif self.model == "ridge" and "ridge" in candidates:
                best_name = "ridge"
                best_weights = candidates[best_name]
                best_sharpe = 0.0
            elif self.model in candidates and self.model != "select":
                best_name = self.model
                best_weights = candidates[best_name]
                best_sharpe = 0.0
            else:
                trial_count += len(candidates)
                for name, weights in candidates.items():
                    self.trial_ledger.append({
                        "window": train[0],
                        "candidate": name,
                        "weights": dict(weights),
                        "signature": name + "|" + ",".join(f"{k}={round(v,4)}" for k, v in sorted(weights.items())),
                    })
                    cfg = self._window_config(train[0], train[-1], weights)
                    result = BacktestEngine(self.market, cfg).run()
                    sharpe = result.metrics.get("sharpe", -math.inf)
                    score = sharpe - self.selection_penalty * result.metrics.get("annualized_turnover", 0.0)
                    if score > best_score and result.trades:
                        best_name, best_score, best_result, best_weights = name, score, result, weights
                        best_sharpe = sharpe
            if not best_weights:
                best_weights = CANDIDATES["balanced"]
                best_name = "balanced"
                best_sharpe = 0.0
            if self.shrink_to_equal > 0.0 and best_name not in FROZEN_MODELS:
                f = self.shrink_to_equal
                prior = CANDIDATES["equal"]
                blended = {}
                for key in set(best_weights) | set(prior):
                    blended[key] = (1.0 - f) * float(best_weights.get(key, 0.0)) + f * float(prior.get(key, 0.0))
                total = sum(blended.values()) or 1.0
                best_weights = {k: v / total for k, v in blended.items()}
                best_name = f"{best_name}+shrink{int(round(f * 100))}"
            weights = best_weights

            def test_config(weights: Dict[str, float]) -> BacktestConfig:
                return self._window_config(test[0], test[-1], weights, alpha_weight=test_alpha)

            # PBO needs every candidate's genuinely out-of-sample returns. For
            # fixed models there is no selection decision, so avoid the extra work.
            candidate_results = {}
            candidate_sharpes = {}
            candidate_returns = {}
            if self.model == "select":
                for candidate_name, weights in candidates.items():
                    candidate_result = BacktestEngine(self.market, test_config(weights)).run()
                    candidate_results[candidate_name] = candidate_result
                    candidate_sharpes[candidate_name] = float(candidate_result.metrics.get("sharpe", 0.0))
                    candidate_returns[candidate_name] = daily_returns(candidate_result.equity_curve)
            if best_name not in candidate_results:
                candidate_result = BacktestEngine(self.market, test_config(best_weights)).run()
                candidate_results[best_name] = candidate_result
                candidate_sharpes[best_name] = float(candidate_result.metrics.get("sharpe", 0.0))
                candidate_returns[best_name] = daily_returns(candidate_result.equity_curve)
            test_result = candidate_results[best_name]
            windows.append(
                WindowResult(
                    train_start=train[0],
                    train_end=train[-1],
                    test_start=test[0],
                    test_end=test[-1],
                    selected=best_name,
                    train_sharpe=best_sharpe,
                    test_metrics=test_result.metrics,
                    test_dates=test_result.dates,
                    test_equity=test_result.equity_curve,
                    test_benchmark=test_result.benchmark_curve,
                    trades=len(test_result.trades),
                    test_candidate_sharpes=candidate_sharpes,
                    test_candidate_returns=candidate_returns,
                )
            )
            start_idx += self.step_days

        if not windows:
            raise ValueError("Walk-forward produced no windows")
        oos_dates: List[str] = []
        oos_equity: List[float] = []
        oos_benchmark: List[float] = []
        port_value = 1.0
        bench_value = 1.0
        for w in windows:
            p_rets = daily_returns(w.test_equity)
            b_rets = daily_returns(w.test_benchmark)
            # Align to actual trading dates after the first observation in each test window.
            dates = w.test_dates[1:]
            for d, pr, br in zip(dates, p_rets, b_rets):
                port_value *= 1.0 + pr
                bench_value *= 1.0 + br
                oos_dates.append(d)
                oos_equity.append(port_value)
                oos_benchmark.append(bench_value)
        unique_signatures = {str(t["signature"]) for t in self.trial_ledger}
        search_space = max(trial_count, len(unique_signatures), self.declared_trials, 1)
        effective_trials = search_space
        oos_metrics = performance_metrics(oos_equity, benchmark=oos_benchmark, trials=effective_trials)
        turnover_avgs = [w.test_metrics.get("average_rebalance_turnover", 0.0) for w in windows]
        gross_avgs = [w.test_metrics.get("average_gross_turnover", 0.0) for w in windows]
        years = max(1.0, len(oos_dates) / 252.0)
        if turnover_avgs:
            oos_metrics["average_rebalance_turnover"] = statistics.mean(turnover_avgs)
            oos_metrics["annualized_turnover"] = sum(turnover_avgs) / years
        # The chained OOS equity curve is built window by window, so its own
        # turnover series is empty; aggregate the gross (round-trip) basis from the
        # windows too.  Reporting one basis as 0.0 while the other is non-zero made
        # the pair unusable.
        if gross_avgs and any(gross_avgs):
            oos_metrics["average_gross_turnover"] = statistics.mean(gross_avgs)
            oos_metrics["annualized_gross_turnover"] = sum(gross_avgs) / years
        elif turnover_avgs:
            oos_metrics["average_gross_turnover"] = 2.0 * statistics.mean(turnover_avgs)
            oos_metrics["annualized_gross_turnover"] = 2.0 * sum(turnover_avgs) / years
        oos_metrics.setdefault("turnover_basis", 1.0)
        validation: Dict[str, object] = {
            "warnings": list(self.warnings),
            "label_horizon_days": float(self.label_horizon_days),
            "purge_days": float(self.purge_days),
            "embargo_days": float(self.embargo_days),
            "effective_isolation_days": float(isolation_days),
            "candidate_trials": float(effective_trials),
            "trial_accounting": {
                "evaluated_in_walkforward": float(trial_count),
                "unique_candidate_signatures": float(len(unique_signatures)),
                "declared_external_trials": float(self.declared_trials),
                "used_for_deflation": float(effective_trials),
            },
        }
        common_candidates = set(windows[0].test_candidate_returns)
        for window in windows[1:]:
            common_candidates &= set(window.test_candidate_returns)
        candidate_names = sorted(common_candidates)
        if len(candidate_names) >= 2:
            matrix = []
            for name in candidate_names:
                row: List[float] = []
                for window in windows:
                    row.extend(window.test_candidate_returns[name])
                matrix.append(row)
            try:
                pbo = probability_of_backtest_overfitting(
                    matrix,
                    n_splits=self.pbo_splits,
                    strategy_names=candidate_names,
                )
            except ValueError as exc:
                validation["pbo_error"] = str(exc)
            else:
                validation["pbo"] = pbo
                oos_metrics["pbo"] = float(pbo["pbo"])
                oos_metrics["pbo_combinations"] = float(pbo["combinations"])
        else:
            validation["pbo_error"] = "fewer than two common candidates"
        oos_metrics["candidate_trials"] = float(effective_trials)
        oos_metrics["label_horizon_days"] = float(self.label_horizon_days)
        if len(oos_equity) >= 3:
            oos_returns = daily_returns(oos_equity)
            track = track_record_report(oos_returns, n_trials=effective_trials)
            validation["track_record"] = track
            oos_metrics["min_track_record_length_years"] = track["min_track_record_length_years"]
            oos_metrics["min_backtest_length_years"] = track["min_backtest_length_years"]
            oos_metrics["haircut_sharpe_annual"] = track["haircut_sharpe_annual"]
            oos_metrics["oos_skewness"] = track["skewness"]
            oos_metrics["oos_kurtosis"] = track["kurtosis"]
        oos_metrics["purge_days"] = float(self.purge_days)
        oos_metrics["embargo_days"] = float(self.embargo_days)
        oos_metrics["effective_isolation_days"] = float(isolation_days)
        return WalkForwardResult(
            windows=windows,
            oos_metrics=oos_metrics,
            oos_dates=oos_dates,
            oos_equity=oos_equity,
            oos_benchmark=oos_benchmark,
            validation=validation,
        )
