from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence


def daily_returns(values: Sequence[float]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0:
            out.append(cur / prev - 1.0)
    return out


def autocorrelation_factor(returns: Sequence[float], max_lag: Optional[int] = None) -> float:
    """Lo (2002) / Newey-West variance inflation factor for the sample mean.

    ``eta = 1 + 2 * sum_k (1 - k/(L+1)) * rho_k``  (Bartlett kernel).
    ``eta > 1`` means positive autocorrelation: the Sharpe standard error is
    understated if this correction is ignored.
    """
    vals = [float(r) for r in returns if math.isfinite(float(r))]
    n = len(vals)
    if n < 4:
        return 1.0
    if max_lag is None:
        max_lag = max(1, int(round(4.0 * (n / 100.0) ** (2.0 / 9.0))))
    max_lag = min(max_lag, n - 1)
    mean = statistics.mean(vals)
    var = sum((r - mean) ** 2 for r in vals) / n
    if var <= 1e-18:
        return 1.0
    eta = 1.0
    for k in range(1, max_lag + 1):
        cov = sum((vals[i] - mean) * (vals[i - k] - mean) for i in range(k, n)) / n
        weight = 1.0 - k / (max_lag + 1.0)
        eta += 2.0 * weight * (cov / var)
    return max(eta, 1e-6)


def sharpe_statistics(returns: Sequence[float], periods_per_year: int = 252) -> Dict[str, float]:
    """Sharpe ratio with the Lo (2002) autocorrelation-adjusted standard error.

    The point estimate is unchanged; what changes is how *significant* it is.
    Reporting a raw Sharpe t-statistic on autocorrelated (monthly-rebalanced,
    daily-valued) returns systematically overstates significance.
    """
    vals = [float(r) for r in returns if math.isfinite(float(r))]
    n = len(vals)
    out = {
        "sharpe_annual": 0.0, "sharpe_se_annual": 0.0,
        "sharpe_t_stat": 0.0, "sharpe_t_stat_iid": 0.0,
        "autocorrelation_factor": 1.0, "observations": float(n),
    }
    if n < 4:
        return out
    std = statistics.pstdev(vals)
    if std <= 1e-16:
        return out
    sr = statistics.mean(vals) / std
    eta = autocorrelation_factor(vals)
    var_iid = (1.0 + 0.5 * sr * sr) / n
    var_adj = eta * var_iid
    se_period = math.sqrt(var_adj)
    root = math.sqrt(max(1, periods_per_year))
    out.update({
        "sharpe_annual": sr * root,
        "sharpe_se_annual": se_period * root,
        "sharpe_t_stat": sr / se_period if se_period > 1e-18 else 0.0,
        "sharpe_t_stat_iid": sr / math.sqrt(var_iid) if var_iid > 1e-18 else 0.0,
        "autocorrelation_factor": eta,
    })
    return out


def max_drawdown(values: Sequence[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            mdd = min(mdd, value / peak - 1.0)
    return mdd


def _beta_alpha(port: Sequence[float], bench: Sequence[float]) -> tuple[float, float]:
    n = min(len(port), len(bench))
    if n < 3:
        return 0.0, 0.0
    p = list(port[-n:])
    b = list(bench[-n:])
    mb = sum(b) / n
    mp = sum(p) / n
    cov = sum((x - mp) * (y - mb) for x, y in zip(p, b)) / (n - 1)
    var = sum((y - mb) ** 2 for y in b) / (n - 1)
    beta = cov / var if var > 1e-16 else 0.0
    alpha_daily = mp - beta * mb
    return beta, alpha_daily * 252.0


def performance_metrics(
    equity: Sequence[float],
    benchmark: Optional[Sequence[float]] = None,
    turnover: Optional[Sequence[float]] = None,
    gross_turnover: Optional[Sequence[float]] = None,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
    trials: int = 1,
) -> Dict[str, float]:
    if not equity:
        return {}
    rets = daily_returns(equity)
    total_return = equity[-1] / equity[0] - 1.0 if equity[0] > 0 else 0.0
    years = max(len(rets) / periods_per_year, 1e-9)
    cagr = (equity[-1] / equity[0]) ** (1.0 / years) - 1.0 if equity[0] > 0 else 0.0
    if len(rets) > 1:
        vol = statistics.pstdev(rets) * math.sqrt(periods_per_year)
        mean = statistics.mean(rets)
        downside = [min(0.0, r) for r in rets]
        downside_vol = math.sqrt(sum(r * r for r in downside) / len(downside)) * math.sqrt(periods_per_year)
    else:
        vol = 0.0
        mean = rets[0] if rets else 0.0
        downside_vol = 0.0
    rf_daily = risk_free_rate / periods_per_year
    sharpe = ((mean - rf_daily) / statistics.pstdev(rets) * math.sqrt(periods_per_year)) if len(rets) > 1 and statistics.pstdev(rets) > 1e-16 else 0.0
    sortino = ((mean - rf_daily) * periods_per_year / downside_vol) if downside_vol > 1e-16 else 0.0
    mdd = max_drawdown(equity)
    calmar = cagr / abs(mdd) if mdd < -1e-12 else 0.0
    hit_rate = sum(1 for r in rets if r > 0) / len(rets) if rets else 0.0
    psr = 0.0
    dsr = 0.0
    psr_autocorr = 0.0
    dsr_autocorr = 0.0
    if len(rets) > 3 and statistics.pstdev(rets) > 1e-16:
        mu = statistics.mean(rets)
        sigma = statistics.pstdev(rets)
        sr_period = (mu - rf_daily) / sigma
        skew = sum(((r - mu) / sigma) ** 3 for r in rets) / len(rets)
        kurt = sum(((r - mu) / sigma) ** 4 for r in rets) / len(rets)
        denom = 1.0 - skew * sr_period + ((kurt - 1.0) / 4.0) * sr_period ** 2
        eta = autocorrelation_factor(rets)
        if denom > 1e-12:
            z = sr_period * math.sqrt(max(1, len(rets) - 1)) / math.sqrt(denom)
            psr = statistics.NormalDist().cdf(z)
            # Same statistic with the autocorrelation-robust denominator.
            z_ac = sr_period * math.sqrt(max(1, len(rets) - 1)) / math.sqrt(denom * eta)
            psr_autocorr = statistics.NormalDist().cdf(z_ac)
        if trials > 1:
            nd = statistics.NormalDist()
            gamma = 0.5772156649
            e = math.e
            expected_max = math.sqrt(1.0 / max(1, len(rets) - 1)) * ((1.0 - gamma) * nd.inv_cdf(1.0 - 1.0 / trials) + gamma * nd.inv_cdf(1.0 - 1.0 / (trials * e)))
            z_dsr = (sr_period - expected_max) * math.sqrt(max(1, len(rets) - 1)) / math.sqrt(denom)
            dsr = nd.cdf(z_dsr)
            z_dsr_ac = (sr_period - expected_max) * math.sqrt(max(1, len(rets) - 1)) / math.sqrt(denom * eta)
            dsr_autocorr = nd.cdf(z_dsr_ac)
        else:
            dsr = psr
            dsr_autocorr = psr_autocorr
    # Turnover observations are recorded once per rebalance, not once per day.
    # ``turnover`` is the **one-way** convention (0.5 * sum|target - current|),
    # matching the optimizer's budget; ``gross_turnover`` is the traded notional
    # (buys + sells) over opening equity.  Both are reported because the two
    # conventions differ by ~2x and mixing them silently doubled cost estimates.
    avg_turnover = sum(turnover or []) / len(turnover) if turnover else 0.0
    ann_turnover = (sum(turnover or []) / years) if turnover else 0.0
    avg_gross = sum(gross_turnover or []) / len(gross_turnover) if gross_turnover else 0.0
    ann_gross = (sum(gross_turnover or []) / years) if gross_turnover else 0.0

    out: Dict[str, float] = {
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "calmar": calmar,
        "hit_rate": hit_rate,
        "average_rebalance_turnover": avg_turnover,
        "annualized_turnover": ann_turnover,
        "turnover_basis": 1.0,  # one-way (0.5 * sum|dw|)
        "average_gross_turnover": avg_gross,
        "annualized_gross_turnover": ann_gross,
        "probabilistic_sharpe_ratio": psr,
        "deflated_sharpe_ratio": dsr,
        "psr_autocorr_adjusted": psr_autocorr,
        "dsr_autocorr_adjusted": dsr_autocorr,
        "autocorrelation_factor": autocorrelation_factor(rets) if len(rets) > 4 else 1.0,
        "trials": float(trials),
    }
    out.update({k: v for k, v in sharpe_statistics(rets, periods_per_year).items() if v})

    if benchmark and len(benchmark) >= 2:
        b_rets = daily_returns(benchmark)
        n = min(len(rets), len(b_rets))
        active = [p - b for p, b in zip(rets[-n:], b_rets[-n:])]
        beta, alpha = _beta_alpha(rets, b_rets)
        b_years = max(len(b_rets) / periods_per_year, 1e-9)
        out["benchmark_total_return"] = benchmark[-1] / benchmark[0] - 1.0
        out["benchmark_cagr"] = (benchmark[-1] / benchmark[0]) ** (1.0 / b_years) - 1.0 if benchmark[0] > 0 else 0.0
        out["benchmark_volatility"] = statistics.pstdev(b_rets) * math.sqrt(periods_per_year) if len(b_rets) > 1 else 0.0
        out["benchmark_sharpe"] = (statistics.mean(b_rets) * periods_per_year / out["benchmark_volatility"]) if out["benchmark_volatility"] > 1e-16 else 0.0
        out["benchmark_max_drawdown"] = max_drawdown(benchmark)
        out["excess_total_return"] = total_return - out["benchmark_total_return"]
        out["excess_cagr"] = cagr - out["benchmark_cagr"]
        out["beta"] = beta
        out["alpha"] = alpha
        out["information_ratio"] = (
            statistics.mean(active) / statistics.pstdev(active) * math.sqrt(periods_per_year)
            if len(active) > 1 and statistics.pstdev(active) > 1e-16
            else 0.0
        )
        out["correlation"] = (
            sum((p - statistics.mean(rets[-n:])) * (b - statistics.mean(b_rets[-n:])) for p, b in zip(rets[-n:], b_rets[-n:]))
            / max(1e-12, math.sqrt(sum((p - statistics.mean(rets[-n:])) ** 2 for p in rets[-n:]) * sum((b - statistics.mean(b_rets[-n:])) ** 2 for b in b_rets[-n:])))
        )
    return out
