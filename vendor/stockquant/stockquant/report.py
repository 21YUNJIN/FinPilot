from __future__ import annotations

import csv
import html
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from stockquant.backtest.engine import BacktestResult
from stockquant.walkforward import WalkForwardResult


def fmt_pct(value: float, digits: int = 2) -> str:
    return f"{100.0 * value:.{digits}f}%"


def fmt_num(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}"


def _svg_chart(
    equity: Sequence[float],
    benchmark: Sequence[float],
    width: int = 960,
    height: int = 320,
) -> str:
    if not equity:
        return "<p>No equity data.</p>"
    vals = list(equity) + list(benchmark or [])
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        hi = lo + 1.0
    pad_l, pad_r, pad_t, pad_b = 52, 16, 18, 34
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b

    def points(series: Sequence[float]) -> str:
        if not series:
            return ""
        n = max(1, len(series) - 1)
        out = []
        for i, v in enumerate(series):
            x = pad_l + inner_w * i / n
            y = pad_t + inner_h * (1.0 - (v - lo) / (hi - lo))
            out.append(f"{x:.1f},{y:.1f}")
        return " ".join(out)

    grid = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + inner_h * frac
        value = hi - (hi - lo) * frac
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="8" y="{y+4:.1f}" class="axis">{value:,.0f}</text>')
    return f"""
<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Equity curve">
  <style>
    .grid {{ stroke: #d8dee9; stroke-width: 1; }}
    .axis {{ fill: #627084; font-size: 11px; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
    .portfolio {{ fill: none; stroke: #1769aa; stroke-width: 3; }}
    .benchmark {{ fill: none; stroke: #d97706; stroke-width: 2; stroke-dasharray: 5 4; }}
  </style>
  {''.join(grid)}
  <polyline class="benchmark" points="{points(benchmark)}"/>
  <polyline class="portfolio" points="{points(equity)}"/>
</svg>
"""


def _metric_cards(metrics: Dict[str, float]) -> str:
    cards = [
        ("CAGR", fmt_pct(metrics.get("cagr", 0.0))),
        ("Sharpe", fmt_num(metrics.get("sharpe", 0.0))),
        ("Max Drawdown", fmt_pct(metrics.get("max_drawdown", 0.0))),
        ("Excess Return", fmt_pct(metrics.get("excess_total_return", 0.0))),
        ("Annual Turnover (one-way)", fmt_pct(metrics.get("annualized_turnover", 0.0))),
        ("Annual Gross Turnover", fmt_pct(metrics.get("annualized_gross_turnover", 0.0))),
        ("Hit Rate", fmt_pct(metrics.get("hit_rate", 0.0))),
    ]
    return "".join(
        f'<div class="card"><div class="card-label">{html.escape(k)}</div><div class="card-value">{html.escape(v)}</div></div>'
        for k, v in cards
    )


def _metrics_table(metrics: Dict[str, float]) -> str:
    rows = []
    for key in sorted(metrics):
        val = metrics[key]
        if any(token in key for token in ("return", "drawdown", "turnover", "hit_rate")):
            shown = fmt_pct(val)
        else:
            shown = fmt_num(val, 4)
        rows.append(f"<tr><td>{html.escape(key)}</td><td>{html.escape(shown)}</td></tr>")
    return "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _targets_table(result: BacktestResult) -> str:
    if not result.target_history:
        return "<p>No target weights recorded.</p>"
    last_date = max(result.target_history)
    weights = result.target_history[last_date]
    if not weights:
        return f"<p>No positions at {html.escape(last_date)}.</p>"
    rows = "".join(
        f"<tr><td>{html.escape(symbol)}</td><td>{fmt_pct(weight)}</td></tr>"
        for symbol, weight in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    )
    return f"<p>Signal date: <code>{html.escape(last_date)}</code>; execution occurs on the next trading session.</p><table><thead><tr><th>Symbol</th><th>Target Weight</th></tr></thead><tbody>{rows}</tbody></table>"


def _diagnostics_table(result: BacktestResult) -> str:
    if not result.diagnostics:
        return "<p>No diagnostics available.</p>"
    rows = []
    for diag in result.diagnostics[-12:]:
        missing = ", ".join(f"{k}:{v}" for k, v in diag.missing_by_factor.items())
        sectors = ", ".join(f"{k}:{v}" for k, v in sorted(diag.sector_counts.items()))
        rows.append(
            f"<tr><td>{html.escape(diag.date)}</td><td>{diag.universe_count}</td><td>{diag.scored_count}</td><td>{html.escape(sectors)}</td><td>{html.escape(missing)}</td></tr>"
        )
    return "<table><thead><tr><th>Date</th><th>Universe</th><th>Scored</th><th>Sectors</th><th>Missing factor inputs</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def render_html(result: BacktestResult, output_path: str) -> None:
    metrics = result.metrics
    warning_html = ""
    if result.warnings:
        warning_html = "<ul>" + "".join(f"<li>{html.escape(w)}</li>" for w in result.warnings[:20]) + "</ul>"
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StockQuant Research Report</title>
<style>
  :root {{ --ink:#17212b; --muted:#627084; --line:#d8dee9; --bg:#f7f9fc; --card:#ffffff; --blue:#1769aa; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.55; }}
  main {{ max-width:1180px; margin:0 auto; padding:40px 24px 72px; }}
  h1 {{ font-size:32px; margin-bottom:4px; }}
  h2 {{ margin-top:34px; border-bottom:1px solid var(--line); padding-bottom:8px; }}
  .sub {{ color:var(--muted); margin-bottom:24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:12px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }}
  .card-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .card-value {{ font-size:23px; font-weight:700; margin-top:6px; }}
  .panel {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; margin-top:16px; }}
  .chart {{ width:100%; height:auto; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); }}
  th, td {{ border-bottom:1px solid var(--line); padding:8px 10px; text-align:left; font-size:13px; }}
  th {{ color:var(--muted); font-weight:600; }}
  code {{ background:#edf2f7; padding:2px 5px; border-radius:4px; }}
  .warning {{ background:#fff7e6; border:1px solid #f2c879; border-radius:10px; padding:12px 16px; }}
  .footer {{ color:var(--muted); font-size:12px; margin-top:34px; }}
  @media (max-width:900px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} }}
</style>
</head>
<body>
<main>
  <h1>StockQuant Research Report</h1>
  <div class="sub">A-share style multi-factor research engine · Walk-forward-ready · Costs included · Research only</div>
  <div class="cards">{_metric_cards(metrics)}</div>
  <div class="panel">{_svg_chart(result.equity_curve, result.benchmark_curve)}</div>
  <div class="panel">
    <strong>Method</strong>
    <p>Factors are computed only from data available on or before the signal date; announcement-derived fields are gated by <code>fundamentals_available_at</code> (a date-only stamp means &laquo;published after the close&raquo;, so it is usable from the next session). Orders are scheduled after the close and executed at the next trading day's open, for names with a traded print (zero-volume / suspended / limit-locked bars are treated as unfillable). The backtest includes commission, stamp duty, slippage, lot size and a T+1 sell guard. Turnover is reported one-way (0.5&middot;&Sigma;|&Delta;w|) and as gross traded notional.</p>
    <p><strong>Important:</strong> the default dataset is synthetic. This report validates the engine, not a live strategy.</p>
  </div>
  <h2>Performance Metrics</h2>
  <div class="panel">{_metrics_table(metrics)}</div>
  <h2>Latest Targets</h2>
  <div class="panel">{_targets_table(result)}</div>
  <h2>Factor Diagnostics</h2>
  <div class="panel">{_diagnostics_table(result)}</div>
  <h2>Warnings</h2>
  <div class="panel warning">{warning_html or '<p>No warnings.</p>'}</div>
  <div class="footer">Generated by stockquant-mvp. Not investment advice. Past performance and synthetic backtests do not predict future returns.</div>
</main>
</body>
</html>
"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


def write_metrics_json(result: BacktestResult, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metrics": result.metrics,
        "config": {
            "initial_cash": result.config.initial_cash,
            "commission_bps": result.config.commission_bps,
            "stamp_duty_bps": result.config.stamp_duty_bps,
            "slippage_bps": result.config.slippage_bps,
            "rebalance": result.config.rebalance,
            "start": result.config.start,
            "end": result.config.end,
        },
        "trade_count": len(result.trades),
        "warnings": result.warnings,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_trades_csv(result: BacktestResult, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "date", "signal_date", "symbol", "side", "shares", "price",
        "gross_amount", "commission", "stamp_duty", "slippage_cost", "cash_after",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for trade in result.trades:
            writer.writerow(trade.__dict__)


def render_walkforward_html(wf: WalkForwardResult, output_path: str) -> None:
    rows = []
    for w in wf.windows:
        rows.append(
            "<tr>"
            f"<td>{html.escape(w.train_start)} → {html.escape(w.train_end)}</td>"
            f"<td>{html.escape(w.test_start)} → {html.escape(w.test_end)}</td>"
            f"<td>{html.escape(w.selected)}</td>"
            f"<td>{fmt_num(w.train_sharpe)}</td>"
            f"<td>{fmt_num(w.test_metrics.get('sharpe', 0.0))}</td>"
            f"<td>{fmt_pct(w.test_metrics.get('cagr', 0.0))}</td>"
            f"<td>{w.trades}</td>"
            "</tr>"
        )
    metrics = wf.oos_metrics
    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Walk-forward Report</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f7f9fc;color:#17212b;margin:0}}
main{{max-width:1180px;margin:0 auto;padding:40px 24px}} .card{{background:white;border:1px solid #d8dee9;border-radius:12px;padding:16px;margin:12px 0}}
table{{width:100%;border-collapse:collapse;background:white}}th,td{{border-bottom:1px solid #d8dee9;padding:8px;text-align:left;font-size:13px}}th{{color:#627084}}
</style></head><body><main>
<h1>Walk-forward Out-of-Sample Report</h1>
<div class="card"><strong>OOS CAGR:</strong> {fmt_pct(metrics.get('cagr',0))} · <strong>Sharpe:</strong> {fmt_num(metrics.get('sharpe',0))} · <strong>Deflated Sharpe:</strong> {fmt_num(metrics.get('deflated_sharpe_ratio',0))} · <strong>PBO:</strong> {fmt_num(metrics.get('pbo', float('nan')))} · <strong>Max Drawdown:</strong> {fmt_pct(metrics.get('max_drawdown',0))} · <strong>IR:</strong> {fmt_num(metrics.get('information_ratio',0))}</div>
<div class="card">{_svg_chart(wf.oos_equity, wf.oos_benchmark)}</div>
<table><thead><tr><th>Train</th><th>Test</th><th>Selected</th><th>Train Sharpe</th><th>Test Sharpe</th><th>Test CAGR</th><th>Trades</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>Each window selects factor weights only from the preceding train window. The test window is not used for selection. A label-horizon purge and embargo separate train labels from the test period.</p>
<p><strong>Deflated Sharpe</strong> adjusts for the number of candidate evaluations. <strong>PBO</strong> estimates the probability that the in-sample winner underperforms the median candidate out of sample.</p>
<p><strong>Warning:</strong> synthetic datasets and repeated candidate selection can still create optimistic results. Validate with PIT real data, multiple markets, and independent cost assumptions.</p>
</main></body></html>"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


def render_comparison_html(experiments: List[Dict[str, object]], output_path: str) -> None:
    rows = []
    cards = []
    for exp in experiments:
        metrics = exp.get("metrics", {})  # type: ignore[assignment]
        name = str(exp.get("name", "experiment"))
        report = str(exp.get("report", ""))
        rows.append(
            "<tr>"
            f"<td><a href=\"{html.escape(report)}\">{html.escape(name)}</a></td>"
            f"<td>{fmt_pct(float(metrics.get('cagr', 0.0)))}</td>"
            f"<td>{fmt_num(float(metrics.get('sharpe', 0.0)))}</td>"
            f"<td>{fmt_pct(float(metrics.get('max_drawdown', 0.0)))}</td>"
            f"<td>{fmt_pct(float(metrics.get('excess_total_return', 0.0)))}</td>"
            f"<td>{fmt_pct(float(metrics.get('annualized_turnover', 0.0)))}</td>"
            "</tr>"
        )
        cards.append(f'<a class="card" href="{html.escape(report)}"><strong>{html.escape(name)}</strong><span>CAGR {fmt_pct(float(metrics.get("cagr", 0.0)))} · Sharpe {fmt_num(float(metrics.get("sharpe", 0.0)))}</span></a>')
    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Real Data Experiment Summary</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f7f9fc;color:#17212b;margin:0}}main{{max-width:1180px;margin:0 auto;padding:40px 24px}}
.cards{{display:grid;grid-template-columns:repeat(2,minmax(240px,1fr));gap:12px}}a.card{{display:flex;flex-direction:column;gap:6px;background:white;border:1px solid #d8dee9;border-radius:12px;padding:16px;text-decoration:none;color:#17212b}}a.card span{{color:#627084;font-size:13px}}
table{{width:100%;border-collapse:collapse;background:white;margin-top:20px}}th,td{{border-bottom:1px solid #d8dee9;padding:9px;text-align:left;font-size:13px}}th{{color:#627084}}a{{color:#1769aa}}.warn{{background:#fff7e6;border:1px solid #f2c879;border-radius:10px;padding:14px 16px;margin-top:18px}}
</style></head><body><main>
<h1>真实 A 股数据实验汇总</h1>
<p>AKShare 沪深300当前成分抽样，2021–2025，后复权。当前成分和缺失 PIT 财务数据仍会产生偏差。</p>
<div class="cards">{''.join(cards)}</div>
<table><thead><tr><th>Experiment</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Excess vs EW</th><th>Annual Turnover (one-way)</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<div class="warn"><strong>Interpretation:</strong> 全周期固定 balanced 方案相对等权基准没有稳定超额；固定 balanced 在 2024–2025 基本接近基准；defensive 有正收益但明显落后基准。Walk-forward 结果会随窗口长度和候选数量变化，不能视为可实盘 alpha。</div>
</main></body></html>"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
