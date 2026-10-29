from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date as Date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec
from stockquant.analysis.trackrecord import multiple_testing_pvalues


def _rank(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 5:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((x - mx) * (y - my) for x, y in zip(rx, ry))
    vx = sum((x - mx) ** 2 for x in rx)
    vy = sum((y - my) ** 2 for y in ry)
    if vx <= 1e-16 or vy <= 1e-16:
        return None
    return cov / math.sqrt(vx * vy)


def newey_west_t_stat(values: Sequence[float]) -> float:
    """Newey-West (HAC) t-statistic for the mean of a possibly autocorrelated
    series (e.g. a time series of factor ICs).  Uses the Bartlett kernel with
    the standard lag choice ``4 * (T/100)^(2/9)``."""
    vals = [float(v) for v in values if math.isfinite(float(v))]
    n = len(vals)
    if n < 2:
        return 0.0
    mean = statistics.mean(vals)
    e = [v - mean for v in vals]
    lag = max(1, int(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lag = min(lag, n - 1)
    gamma0 = sum(x * x for x in e) / n
    hac = gamma0
    for l in range(1, lag + 1):
        gamma_l = sum(e[t] * e[t - l] for t in range(l, n)) / n
        weight = 1.0 - l / (lag + 1.0)
        hac += 2.0 * weight * gamma_l
    var_mean = hac / n
    if var_mean <= 1e-16:
        return 0.0
    return mean / math.sqrt(var_mean)


def two_sided_pvalue(t_stat: float) -> float:
    """Two-sided normal p-value from a t-statistic."""
    return 2.0 * (1.0 - statistics.NormalDist().cdf(abs(float(t_stat))))


def _rebalance_due(cur: str, nxt: str, mode: str) -> bool:
    if mode == "daily":
        return True
    if mode == "weekly":
        return Date.fromisoformat(cur).isocalendar()[:2] != Date.fromisoformat(nxt).isocalendar()[:2]
    if mode == "quarterly":
        return (int(cur[5:7]) - 1) // 3 != (int(nxt[5:7]) - 1) // 3 or cur[:4] != nxt[:4]
    if mode == "semiannual":
        return (int(cur[5:7]) - 1) // 6 != (int(nxt[5:7]) - 1) // 6 or cur[:4] != nxt[:4]
    if mode == "annual":
        return cur[:4] != nxt[:4]
    return cur[:7] != nxt[:7]


def _forward_return(market: MarketData, symbol: str, start: str, end: str) -> Optional[float]:
    a = market.bar(symbol, start)
    b = market.bar(symbol, end)
    if a is None or b is None or a.close <= 0 or b.close <= 0 or a.suspended or b.suspended:
        return None
    return b.close / a.close - 1.0


@dataclass
class FactorICResult:
    horizon: int
    rebalance: str
    observations: int
    summary: Dict[str, Dict[str, float]]
    by_date: List[Dict[str, object]]


def analyze_factor_ic(
    market: MarketData,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    factors: Optional[Sequence[FactorSpec]] = None,
    min_market_cap_quantile: float = 0.0,
) -> FactorICResult:
    fset = tuple(factors) if factors is not None else DEFAULT_FACTORS
    engine = FactorEngine(
        fset,
        min_history=min_history,
        sector_neutral=sector_neutral,
        min_market_cap_quantile=min_market_cap_quantile,
    )
    dates = [
        d for d in market.dates
        if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)
    ]
    by_date: List[Dict[str, object]] = []
    factor_values: Dict[str, List[float]] = {f.name: [] for f in fset}
    long_short_values: Dict[str, List[float]] = {f.name: [] for f in fset}
    for i in range(0, max(0, len(dates) - horizon)):
        cur = dates[i]
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or not _rebalance_due(cur, nxt, rebalance):
            continue
        signal = dates[i]
        future = dates[i + horizon]
        components, diag = engine.components(market, signal)
        if diag.scored_count < 5:
            continue
        row: Dict[str, object] = {"date": signal, "future_date": future, "count": diag.scored_count, "ic": {}}
        for f in fset:
            xs, ys = [], []
            pairs = []
            for symbol, z in components.get(f.name, {}).items():
                fr = _forward_return(market, symbol, signal, future)
                if fr is None:
                    continue
                xs.append(z)
                ys.append(fr)
                pairs.append((z, fr))
            ic = spearman(xs, ys)
            if ic is not None:
                row["ic"][f.name] = ic  # type: ignore[index]
                factor_values[f.name].append(ic)
            if len(pairs) >= 10:
                pairs.sort(key=lambda x: x[0])
                k = max(1, len(pairs) // 5)
                bottom = sum(v for _, v in pairs[:k]) / k
                top = sum(v for _, v in pairs[-k:]) / k
                long_short_values[f.name].append(top - bottom)
        if row["ic"]:  # type: ignore[truthy-function]
            by_date.append(row)
    summary: Dict[str, Dict[str, float]] = {}
    raw_pvalues: Dict[str, float] = {}
    for factor, values in factor_values.items():
        if not values:
            summary[factor] = {"mean_ic": 0.0, "ic_std": 0.0, "ic_ir": 0.0, "t_stat": 0.0, "t_stat_nw": 0.0, "p_value": 1.0, "p_value_bonf": 1.0, "p_value_holm": 1.0, "p_value_bhy": 1.0, "positive_rate": 0.0, "mean_long_short": 0.0, "long_short_t_stat": 0.0, "positive_long_short": 0.0, "observations": 0.0}
            continue
        mean = statistics.mean(values)
        std = statistics.pstdev(values)
        periods = 52 if rebalance == "weekly" else 252 if rebalance == "daily" else 12
        ls_values = long_short_values.get(factor, [])
        ls_mean = statistics.mean(ls_values) if ls_values else 0.0
        ls_std = statistics.pstdev(ls_values) if len(ls_values) > 1 else 0.0
        t_stat_nw = newey_west_t_stat(values)
        p_value = two_sided_pvalue(t_stat_nw)
        raw_pvalues[factor] = p_value
        summary[factor] = {
            "mean_ic": mean,
            "ic_std": std,
            "ic_ir": mean / std * math.sqrt(periods) if std > 1e-16 else 0.0,
            "t_stat": mean / std * math.sqrt(len(values)) if std > 1e-16 else 0.0,
            "t_stat_nw": t_stat_nw,
            "p_value": p_value,
            "p_value_bonf": 1.0,
            "p_value_holm": 1.0,
            "p_value_bhy": 1.0,
            "positive_rate": sum(1 for v in values if v > 0) / len(values),
            "mean_long_short": ls_mean,
            "long_short_t_stat": ls_mean / ls_std * math.sqrt(len(ls_values)) if ls_std > 1e-16 else 0.0,
            "positive_long_short": sum(1 for v in ls_values if v > 0) / len(ls_values) if ls_values else 0.0,
            "observations": float(len(values)),
        }
    if raw_pvalues:
        order = [f.name for f in fset if f.name in raw_pvalues]
        raw = [raw_pvalues[name] for name in order]
        bonf = multiple_testing_pvalues(raw, method="bonferroni")
        holm = multiple_testing_pvalues(raw, method="holm")
        bhy = multiple_testing_pvalues(raw, method="bhy")
        for name, b, h, y in zip(order, bonf, holm, bhy):
            summary[name]["p_value_bonf"] = b
            summary[name]["p_value_holm"] = h
            summary[name]["p_value_bhy"] = y
    return FactorICResult(horizon=horizon, rebalance=rebalance, observations=len(by_date), summary=summary, by_date=by_date)


def analyze_ic_decay(
    market: MarketData,
    horizons: Sequence[int] = (5, 10, 20, 60),
    min_history: int = 80,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    factors: Optional[Sequence[FactorSpec]] = None,
    min_market_cap_quantile: float = 0.0,
) -> Dict[str, Dict[int, float]]:
    """Mean IC by forward horizon (IC decay) for each factor.

    A factor with durable predictive power shows a slowly-decaying positive IC;
    a lucky factor spikes at one horizon and vanishes.  Returns
    ``{factor_name: {horizon_days: mean_ic}}``.
    """
    out: Dict[str, Dict[int, float]] = {}
    for horizon in horizons:
        result = analyze_factor_ic(
            market,
            min_history=min_history,
            horizon=horizon,
            rebalance=rebalance,
            sector_neutral=sector_neutral,
            start_date=start_date,
            end_date=end_date,
            factors=factors,
            min_market_cap_quantile=min_market_cap_quantile,
        )
        for factor, metrics in result.summary.items():
            out.setdefault(factor, {})[horizon] = metrics["mean_ic"]
    return out


def write_ic_report(result: FactorICResult, output_path: str, json_path: Optional[str] = None) -> None:
    import html
    import json

    rows = []
    for factor, m in sorted(result.summary.items(), key=lambda kv: kv[1]["mean_ic"], reverse=True):
        rows.append(
            "<tr>"
            f"<td>{html.escape(factor)}</td>"
            f"<td>{m['mean_ic']:.4f}</td>"
            f"<td>{m['ic_std']:.4f}</td>"
            f"<td>{m['ic_ir']:.3f}</td>"
            f"<td>{m['t_stat']:.3f}</td>"
            f"<td>{100*m['positive_rate']:.1f}%</td>"
            f"<td>{m['mean_long_short']:.4f}</td>"
            f"<td>{m['long_short_t_stat']:.3f}</td>"
            f"<td>{int(m['observations'])}</td>"
            "</tr>"
        )
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Factor IC Report</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f7f9fc;color:#17212b;margin:0}}main{{max-width:1000px;margin:0 auto;padding:40px 24px}}
.card{{background:white;border:1px solid #d8dee9;border-radius:12px;padding:16px;margin:14px 0}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{border-bottom:1px solid #d8dee9;padding:9px;text-align:left;font-size:13px}}th{{color:#627084}}
code{{background:#edf2f7;padding:2px 5px;border-radius:4px}}</style></head><body><main>
<h1>Factor IC / ICIR Report</h1>
<div class="card">Rebalance: <code>{html.escape(result.rebalance)}</code> · Forward horizon: <code>{result.horizon} trading days</code> · Valid periods: <code>{result.observations}</code></div>
<table><thead><tr><th>Factor</th><th>Mean IC</th><th>IC Std</th><th>ICIR</th><th>t-stat</th><th>Positive Rate</th><th>Mean Long-Short</th><th>LS t-stat</th><th>Obs.</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="card"><strong>Interpretation:</strong> positive mean IC means higher factor scores tended to be followed by higher relative returns in this sample. t-stat is descriptive, not a multiple-testing-corrected discovery.</div>
</main></body></html>"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    if json_path:
        Path(json_path).write_text(json.dumps({"summary": result.summary, "by_date": result.by_date}, ensure_ascii=False, indent=2), encoding="utf-8")
