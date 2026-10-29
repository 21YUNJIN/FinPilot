"""Track-record diagnostics for backtest results.

Implements the statistical tools from the quantitative-finance literature that
answer the question "is this backtest credible?":

* Probabilistic Sharpe Ratio (PSR)  -- Bailey & Lopez de Prado (2012/2014)
* Deflated Sharpe Ratio (DSR)       -- Bailey & Lopez de Prado (2014)
* Minimum Track Record Length (MinTRL) -- Bailey & Lopez de Prado (2014)
* Minimum Backtest Length (MinBTL)  -- Bailey, Borwein, Lopez de Prado & Zhu (2014)
* Haircut Sharpe Ratio              -- Harvey & Liu (2015), Harvey, Liu & Zhu (2016)

Conventions (matching ``stockquant.backtest.metrics``):

* The Sharpe ratio used inside the non-normality corrections is the *per-period*
  Sharpe ratio (``mean / population_std`` of the return series).  Skewness and
  kurtosis are the raw standardized third and fourth moments (kurtosis = 3 for a
  normal distribution).  Lengths are returned in years by dividing the number of
  observations by ``periods_per_year``.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Optional, Sequence, Tuple

EULER_MASCHERONI = 0.5772156649015329


def _norm() -> statistics.NormalDist:
    return statistics.NormalDist()


def return_statistics(
    returns: Sequence[float],
    periods_per_year: int = 252,
) -> Dict[str, float]:
    """Return sample moments and Sharpe ratios for a return series."""
    vals = [float(r) for r in returns if math.isfinite(float(r))]
    out: Dict[str, float] = {
        "observations": float(len(vals)),
        "mean": 0.0,
        "std": 0.0,
        "sharpe_period": 0.0,
        "sharpe_annual": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "excess_kurtosis": 0.0,
    }
    if len(vals) < 2:
        return out
    mean = statistics.mean(vals)
    std = statistics.pstdev(vals)
    sr_period = mean / std if std > 1e-12 else 0.0
    sr_annual = sr_period * math.sqrt(max(1, periods_per_year))
    if std > 1e-12:
        skew = sum(((v - mean) / std) ** 3 for v in vals) / len(vals)
        kurt = sum(((v - mean) / std) ** 4 for v in vals) / len(vals)
    else:
        skew, kurt = 0.0, 3.0
    return {
        "observations": float(len(vals)),
        "mean": mean,
        "std": std,
        "sharpe_period": sr_period,
        "sharpe_annual": sr_annual,
        "skewness": skew,
        "kurtosis": kurt,
        "excess_kurtosis": kurt - 3.0,
    }


def expected_max_sharpe_ratio(n_trials: float) -> float:
    """Expected value of the maximum Sharpe ratio over ``n_trials`` independent
    skill-less strategies, expressed in units of the Sharpe ratio's standard
    error (a pure number, ~sqrt(2 ln N) for large N).

    Bailey, Borwein, Lopez de Prado & Zhu (2014), eq. for E[max_N].
    """
    n = float(n_trials)
    if n <= 1.0:
        return 0.0
    nd = _norm()
    # Clamp the inputs just inside (0, 1) so inv_cdf stays finite.
    p1 = max(1e-15, min(1.0 - 1e-15, 1.0 - 1.0 / n))
    p2 = max(1e-15, min(1.0 - 1e-15, 1.0 - 1.0 / (n * math.e)))
    z1 = nd.inv_cdf(p1)
    z2 = nd.inv_cdf(p2)
    return (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    benchmark_sr: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """PSR: probability that the true Sharpe exceeds ``benchmark_sr`` (per-period)."""
    s = return_statistics(returns, periods_per_year)
    sr = s["sharpe_period"]
    n = s["observations"]
    if n <= 1 or s["std"] <= 1e-12:
        return 0.5
    denom = _psr_denominator(sr, s["skewness"], s["kurtosis"])
    z = (sr - benchmark_sr) * math.sqrt(n - 1.0) / math.sqrt(denom)
    return _norm().cdf(z)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    n_trials: float,
    periods_per_year: int = 252,
) -> float:
    """DSR: PSR evaluated against the expected maximum Sharpe from ``n_trials``
    independent skill-less strategies."""
    s = return_statistics(returns, periods_per_year)
    sr = s["sharpe_period"]
    n = s["observations"]
    if n <= 1 or s["std"] <= 1e-12:
        return 0.5
    emax = expected_max_sharpe_ratio(n_trials)
    benchmark = emax * math.sqrt(1.0 / (n - 1.0)) if n > 1 else 0.0
    denom = _psr_denominator(sr, s["skewness"], s["kurtosis"])
    z = (sr - benchmark) * math.sqrt(n - 1.0) / math.sqrt(denom)
    return _norm().cdf(z)


def min_track_record_length(
    returns: Sequence[float],
    benchmark_sr: float = 0.0,
    confidence: float = 0.95,
    periods_per_year: int = 252,
) -> float:
    """MinTRL in years: how long a track record is needed to be ``confidence``
    sure that the true Sharpe exceeds ``benchmark_sr``, given the observed
    Sharpe, skewness and kurtosis."""
    s = return_statistics(returns, periods_per_year)
    sr = s["sharpe_period"]
    if s["observations"] < 2 or s["std"] <= 1e-12:
        return float("inf")
    if sr <= benchmark_sr + 1e-12:
        return float("inf")
    z = _norm().inv_cdf(max(1e-12, min(1.0 - 1e-12, confidence)))
    denom = _psr_denominator(sr, s["skewness"], s["kurtosis"])
    observations = 1.0 + denom * (z / (sr - benchmark_sr)) ** 2
    return observations / max(1, periods_per_year)


def min_backtest_length(
    returns: Sequence[float],
    n_trials: float,
    periods_per_year: int = 252,
) -> float:
    """MinBTL in years: the minimum backtest length an investor should require
    given ``n_trials`` independent strategy configurations were tried.

    Uses the standard normal/independence approximation from Bailey, Borwein,
    Lopez de Prado & Zhu (2014): ``MinBTL ~= 2 * ln(N) / SR_annual^2`` where
    ``SR_annual`` is the annualized Sharpe of the selected strategy.  Returns 0
    for a single trial (no selection bias) and ``inf`` for a non-positive
    observed Sharpe.
    """
    s = return_statistics(returns, periods_per_year)
    sr_annual = s["sharpe_annual"]
    if sr_annual <= 1e-12:
        return float("inf")
    n = float(n_trials)
    if n <= 1.0:
        return 0.0
    return 2.0 * math.log(n) / (sr_annual * sr_annual)


def _psr_denominator(sr: float, skew: float, kurt: float) -> float:
    """Mertens' small-sample variance factor: 1 - skew*SR + (kurt-1)/4 * SR^2."""
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    return max(denom, 1e-12)


def _two_sided_pvalue(t_stat: float) -> float:
    return 2.0 * (1.0 - _norm().cdf(abs(t_stat)))


def harvey_liu_haircut(
    returns: Sequence[float],
    n_trials: float,
    method: str = "holm",
    periods_per_year: int = 252,
) -> Dict[str, float]:
    """Harvey-Liu haircut Sharpe ratio.

    The multiple-testing corrected p-value for "best of N trials" is
    ``1 - (1 - p)^N`` (Holm/max-of-N) or ``min(N*p, 1)`` (Bonferroni).  That
    adjusted p-value is converted back into an equivalent t-statistic, then into
    a Sharpe ratio (``SR = t / sqrt(n)``).  BHY requires the full distribution of
    p-values across trials, which is not available for a single reported best
    strategy, so it is handled by :func:`multiple_testing_pvalues` instead.
    """
    method = method.lower()
    if method not in {"holm", "bonferroni"}:
        raise ValueError("method must be 'holm' or 'bonferroni' for a single reported best")
    s = return_statistics(returns, periods_per_year)
    n = s["observations"]
    if n < 2 or s["std"] <= 1e-12:
        return {
            "haircut_sharpe": 0.0,
            "haircut_sharpe_annual": 0.0,
            "t_stat": 0.0,
            "single_pvalue": 1.0,
            "adjusted_pvalue": 1.0,
            "n_trials": float(n_trials),
            "method": method,
        }
    t_stat = s["sharpe_period"] * math.sqrt(n)
    p_single = _two_sided_pvalue(t_stat)
    n_trials_f = float(n_trials)
    if method == "bonferroni":
        p_adj = min(1.0, n_trials_f * p_single)
    else:  # holm / max-of-N
        p_adj = 1.0 - (1.0 - p_single) ** n_trials_f if p_single < 1.0 else 1.0
    if p_adj >= 1.0:
        t_adj = 0.0
    else:
        t_adj = _norm().inv_cdf(1.0 - p_adj / 2.0)
    haircut_period = t_adj / math.sqrt(n)
    return {
        "haircut_sharpe": haircut_period,
        "haircut_sharpe_annual": haircut_period * math.sqrt(max(1, periods_per_year)),
        "t_stat": t_stat,
        "single_pvalue": p_single,
        "adjusted_pvalue": p_adj,
        "n_trials": n_trials_f,
        "method": method,
    }


def multiple_testing_pvalues(
    p_values: Sequence[float],
    method: str = "bhy",
) -> List[float]:
    """Multiple-testing adjusted p-values.

    Supports Bonferroni, Holm (FWER) and Benjamini-Hochberg-Yekutieli (FDR),
    following Harvey, Liu & Zhu (2016).  Returns adjusted p-values in the
    original input order.
    """
    method = method.lower()
    if method not in {"bonferroni", "holm", "bhy"}:
        raise ValueError("method must be 'bonferroni', 'holm' or 'bhy'")
    p = [float(x) for x in p_values]
    m = len(p)
    if m == 0:
        return []
    if m == 1:
        return [min(1.0, p[0])]
    order = sorted(range(m), key=lambda i: p[i])  # ascending
    sorted_p = [p[i] for i in order]
    adj_sorted: List[float] = [0.0] * m

    if method == "bonferroni":
        for idx, raw in enumerate(sorted_p):
            adj_sorted[idx] = min(1.0, m * raw)
    elif method == "holm":
        # adjusted_i = max_{j<=i} min(1, (m-j+1)*p_(j))
        running = 0.0
        for i, raw in enumerate(sorted_p):
            candidate = min(1.0, (m - i) * raw)
            running = max(running, candidate)
            adj_sorted[i] = running
    else:  # bhy (Benjamini-Hochberg-Yekutieli), descending pass
        harmonic = math.fsum(1.0 / j for j in range(1, m + 1))
        # sorted descending
        desc = list(reversed(sorted_p))
        adj_desc: List[float] = [0.0] * m
        adj_desc[0] = desc[0]
        for i in range(1, m):
            q = (m * harmonic / (m - i)) * desc[i]
            adj_desc[i] = min(adj_desc[i - 1], min(1.0, q))
        # map descending back to ascending
        for i in range(m):
            adj_sorted[m - 1 - i] = adj_desc[i]

    out = [0.0] * m
    for pos, i in enumerate(order):
        out[i] = adj_sorted[pos]
    return out


def track_record_report(
    returns: Sequence[float],
    n_trials: float = 1,
    benchmark_sr: float = 0.0,
    confidence: float = 0.95,
    periods_per_year: int = 252,
) -> Dict[str, float]:
    """One-shot summary of all track-record diagnostics."""
    s = return_statistics(returns, periods_per_year)
    report: Dict[str, float] = {
        "observations": s["observations"],
        "sharpe_annual": s["sharpe_annual"],
        "skewness": s["skewness"],
        "kurtosis": s["kurtosis"],
        "probabilistic_sharpe_ratio": probabilistic_sharpe_ratio(
            returns, benchmark_sr, periods_per_year
        ),
        "deflated_sharpe_ratio": deflated_sharpe_ratio(
            returns, n_trials, periods_per_year
        ),
        "min_track_record_length_years": min_track_record_length(
            returns, benchmark_sr, confidence, periods_per_year
        ),
        "min_backtest_length_years": min_backtest_length(
            returns, n_trials, periods_per_year
        ),
    }
    haircut = harvey_liu_haircut(returns, n_trials, method="holm", periods_per_year=periods_per_year)
    report["haircut_sharpe_annual"] = haircut["haircut_sharpe_annual"]
    report["haircut_sharpe_period"] = haircut["haircut_sharpe"]
    return report
