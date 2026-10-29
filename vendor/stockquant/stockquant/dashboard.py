from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request

from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.data import generate_demo_market, load_csv, load_membership_csv
from stockquant.data.realtime import fetch_realtime_quotes
from stockquant.domain import MarketData
from stockquant.etf_plan import evaluate_plan_gate
from stockquant.factors import DEFAULT_FACTORS
from stockquant.portfolio import PortfolioConstraints
from stockquant.quant_api import factor_snapshot, portfolio_risk, stress_test
from stockquant.assistant.llm import build_llm_client
from stockquant.assistant.quant_query import QuantAssistant
from stockquant.recommendation import build_recommendations
from stockquant.assistant.alerts import AlertStore, build_alerts
from stockquant.assistant.notify import dispatch as notify_dispatch, load_notify_config
from stockquant.assistant.monitor import HoldingsMonitor, MonitorStore
from stockquant.assistant.position_gap import compute_position_gap
from stockquant.assistant.portfolio import PortfolioLedger
from stockquant.assistant.watchlist import WatchlistStore
from stockquant.walkforward import CANDIDATES, with_weights


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent.parent
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
if not OUTPUTS_ROOT.exists():
    OUTPUTS_ROOT = WORKSPACE_ROOT / "outputs"

DATASETS = {
    "csi300_full": {
        # 564 只历史成分（含补齐的 15 只退市股），市值已按 raw_close×股本 修正，
        # fundamentals_available_at 已补戳 —— 旧的 549 只文件市值虚高 100~200 倍。
        "label": "历史沪深300 · 564只 · 市值已修 + PIT 可得时间 · 含退市股",
        "data": "data/real/csi300_hist_full_2018_2026.recovered.csv",
        "membership": "data/real/dolt_csi300_membership_2018_2025.csv",
        "start": "2018-07-01",
        "end": "2026-09-30",
    },
    "csi300_akshare": {
        "label": "历史沪深300 · 549只 · 行情+行业(无基本面)",
        "data": "data/real/csi300_hist_akshare_2018_2026.csv",
        "membership": "data/real/dolt_csi300_membership_2018_2025.csv",
        "start": "2018-07-01",
        "end": "2026-09-30",
    },
    "real_v3": {
        "label": "历史沪深300成分 · 80只 · PIT财务/预告/股东",
        "data": "data/real/qlib_csi300_hist80_v3_clean_2018_2025.csv",
        "membership": "data/real/dolt_csi300_membership_2018_2025.csv",
        "start": "2018-07-01",
        "end": "2025-12-31",
    },
    "real_v2": {
        "label": "历史沪深300成分 · 80只 · PIT财务",
        "data": "data/real/qlib_csi300_hist80_v2_2018_2025.csv",
        "membership": "data/real/dolt_csi300_membership_2018_2025.csv",
        "start": "2018-07-01",
        "end": "2025-12-31",
    },
    "csi300_30": {
        "label": "当前沪深300 · 30只 · 真实行情",
        "data": "data/real/csi300_30_hfq_pit_v2.csv",
        "membership": None,
        "start": "2021-06-01",
        "end": "2025-12-31",
    },
    "demo": {
        "label": "合成演示数据",
        "data": None,
        "membership": None,
        "start": "2024-01-01",
        "end": "2024-12-31",
    },
}

app = Flask(__name__)
_data_cache: Dict[str, MarketData] = {}
_result_cache: Dict[str, Dict[str, Any]] = {}
_last_result: Dict[str, Any] = {}
_run_lock = threading.Lock()
_quant_sessions: Dict[str, Dict[str, Any]] = {}
_quant_session_lock = threading.Lock()

STATE_DIR = PROJECT_ROOT / "data" / "state"
_monitor_store = MonitorStore(str(STATE_DIR))
_alert_store = AlertStore(str(STATE_DIR))
_monitor_lock = threading.Lock()
_monitor_state: Dict[str, Any] = {"running": False, "last_error": None}
_monitor_thread_started = False
_monitor_wake = threading.Event()
_started_at = time.time()

LAST_BACKTEST_PATH = STATE_DIR / "last_backtest.json"


def _load_last_result() -> Dict[str, Any]:
    try:
        return json.loads(LAST_BACKTEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_result(result: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_BACKTEST_PATH.write_text(
            json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception:
        pass


_last_result = _load_last_result()


ACCESS_TOKEN_PATH = STATE_DIR / "access_token.txt"


def _access_token() -> str:
    """Local access token for non-localhost requests (family / LAN access)."""
    try:
        tok = ACCESS_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except Exception:
        pass
    import secrets
    tok = secrets.token_urlsafe(9)
    try:
        ACCESS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACCESS_TOKEN_PATH.write_text(tok, encoding="utf-8")
    except Exception:
        pass
    return tok


def _is_local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1", "localhost", None)


@app.before_request
def _gate_nonlocal() -> Any:
    """Localhost is open; any other client must supply the access token."""
    if _is_local():
        return None
    tok = _access_token()
    if request.cookies.get("sq_token") == tok:
        return None
    if request.args.get("k") == tok:
        return None
    return Response(
        "<meta charset='utf-8'><h2>需要访问口令</h2><p>请在网址后加 <code>?k=口令</code>（口令在运行服务的电脑上）。</p>",
        status=401, mimetype="text/html")


@app.after_request
def _persist_token_cookie(resp):
    try:
        if request.args.get("k") and request.args.get("k") == _access_token() and not _is_local():
            resp.set_cookie("sq_token", _access_token(), max_age=60 * 60 * 24 * 365, samesite="Lax")
    except Exception:
        pass
    return resp


def _html_safe(value: Any) -> Any:
    if isinstance(value, str):
        return html.escape(value)
    if isinstance(value, list):
        return [_html_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _html_safe(v) for k, v in value.items()}
    return value


def _json_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _sample(values: List[float], limit: int = 600) -> List[float]:
    if len(values) <= limit:
        return [_json_float(v) for v in values]
    step = (len(values) - 1) / (limit - 1)
    return [_json_float(values[round(i * step)]) for i in range(limit)]


def _get_market(key: str) -> MarketData:
    if key in _data_cache:
        return _data_cache[key]
    spec = DATASETS.get(key)
    if not spec:
        raise ValueError("unknown dataset")
    if key == "demo":
        market = MarketData.from_rows(generate_demo_market(start=spec["start"], end=spec["end"]))
    else:
        path = PROJECT_ROOT / str(spec["data"])
        market = load_csv(str(path))
        membership = spec.get("membership")
        if membership:
            market.membership_by_date = load_membership_csv(str(PROJECT_ROOT / str(membership)))
    _data_cache[key] = market
    return market


def _config_from_payload(payload: Dict[str, Any]) -> BacktestConfig:
    preset = str(payload.get("preset", "balanced"))
    if preset not in CANDIDATES:
        preset = "balanced"
    return BacktestConfig(
        initial_cash=_json_float(payload.get("initial_cash"), 1_000_000.0),
        commission_bps=_json_float(payload.get("commission_bps"), 3.0),
        stamp_duty_bps=_json_float(payload.get("stamp_duty_bps"), 5.0),
        slippage_bps=_json_float(payload.get("slippage_bps"), 5.0),
        lot_size=int(payload.get("lot_size", 100)),
        rebalance=str(payload.get("rebalance", "quarterly")),
        start=str(payload.get("start") or DATASETS[next(iter(DATASETS))]["start"]),
        end=str(payload.get("end") or DATASETS[next(iter(DATASETS))]["end"]),
        min_history=int(payload.get("min_history", 100)),
        sector_neutral=bool(payload.get("sector_neutral", True)),
        target_volatility=None if payload.get("no_target_vol") else _json_float(payload.get("target_volatility"), 0.18),
        market_filter=str(payload.get("market_filter", "none")),
        market_filter_lookback=int(payload.get("market_filter_lookback", 200)),
        risk_off_exposure=_json_float(payload.get("risk_off_exposure"), 0.5),
        alpha_weight=_json_float(payload.get("alpha_weight"), 0.3),
        rebalance_band=_json_float(payload.get("rebalance_band"), 0.02),
        min_trade_weight=_json_float(payload.get("min_trade_weight"), 0.005),
        constraints=PortfolioConstraints(
            max_positions=int(payload.get("max_positions", 12)),
            max_weight=_json_float(payload.get("max_weight"), 0.10),
            sector_max_weight=_json_float(payload.get("sector_max_weight"), 0.30),
            cash_buffer=_json_float(payload.get("cash_buffer"), 0.03),
            min_score=_json_float(payload.get("min_score"), -9.0),
            weight_scheme=str(payload.get("weight_scheme", "score")),
            max_turnover=_json_float(payload.get("max_turnover"), 1.0),
            cvar_alpha=(None if not payload.get("cvar_alpha") else _json_float(payload.get("cvar_alpha"), 0.05)),
            cvar_lambda=_json_float(payload.get("cvar_lambda"), 0.0),
        ),
        vol_method=str(payload.get("vol_method", "realized")),
        portfolio_drawdown_limit=(
            None if not payload.get("portfolio_drawdown_limit")
            else _json_float(payload.get("portfolio_drawdown_limit"), 0.15)
        ),
        portfolio_drawdown_exposure=_json_float(payload.get("portfolio_drawdown_exposure"), 0.5),
        factors=with_weights(DEFAULT_FACTORS, CANDIDATES[preset]),
    )


def _run_backtest(payload: Dict[str, Any]) -> Dict[str, Any]:
    dataset_key = str(payload.get("dataset", "real_v3"))
    market = _get_market(dataset_key)
    config = _config_from_payload(payload)
    cache_key = hashlib.sha256(
        json.dumps({"dataset": dataset_key, "config": payload}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if cache_key in _result_cache:
        return _result_cache[cache_key]
    with _run_lock:
        if cache_key in _result_cache:
            return _result_cache[cache_key]
        result = BacktestEngine(market, config).run()
    metrics = {k: _json_float(v) for k, v in result.metrics.items()}
    latest_targets = {}
    if result.target_history:
        last_date = max(result.target_history)
        latest_targets = {s: _json_float(w) for s, w in result.target_history[last_date].items()}
    else:
        last_date = None
    payload_out = {
        "dataset": dataset_key,
        "dataset_label": DATASETS[dataset_key]["label"],
        "period": [result.dates[0], result.dates[-1]] if result.dates else [],
        "metrics": metrics,
        "equity": _sample(result.equity_curve),
        "benchmark": _sample(result.benchmark_curve),
        "dates": _sample([float(i) for i in range(len(result.equity_curve))], limit=600),
        "targets": sorted(latest_targets.items(), key=lambda x: x[1], reverse=True)[:30],
        "recommendations": build_recommendations(market, config, result, top_n=20),
        "target_date": last_date,
        "trade_count": len(result.trades),
        "warnings": result.warnings,
        "config": {
            "rebalance": config.rebalance,
            "preset": str(payload.get("preset", "balanced")),
            "alpha_weight": config.alpha_weight,
            "max_positions": config.constraints.max_positions,
            "target_volatility": config.target_volatility,
            "market_filter": config.market_filter,
            "weight_scheme": config.constraints.weight_scheme,
            "max_turnover": config.constraints.max_turnover,
        },
    }
    _result_cache[cache_key] = payload_out
    global _last_result
    _last_result = payload_out
    _save_last_result(payload_out)
    return payload_out


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _first_json(paths: List[Path], default: Any) -> tuple:
    """Return (path, data) for the first existing artifact on disk."""
    for path in paths:
        if path.exists():
            return path, _read_json(path, default)
    return None, default


IC_CANDIDATES = [
    OUTPUTS_ROOT / "stockquant_akshare_research" / "factor_ic.json",
    OUTPUTS_ROOT / "stockquant_hist80_v3_ic" / "factor_ic.json",
    OUTPUTS_ROOT / "stockquant_hist80_v3_ic" / "factor_ic.json",
]

ATTRIBUTION_CANDIDATES = [
    OUTPUTS_ROOT / "stockquant_akshare_attribution" / "style_attribution.json",
]

WALKFORWARD_CANDIDATES = [
    # 最新、最诚实的一次：strict PIT + 修好的市值加权基准 + 冻结权重对照
    OUTPUTS_ROOT / "stockquant_stock" / "wf_pit_strict" / "walkforward.json",
    OUTPUTS_ROOT / "stockquant_akshare_walkforward_v2" / "walkforward.json",
    OUTPUTS_ROOT / "stockquant_akshare_walkforward" / "walkforward.json",
    OUTPUTS_ROOT / "stockquant_hist80_v3_wf_balanced" / "walkforward.json",
    OUTPUTS_ROOT / "stockquant_hist80_v3_wf_fq" / "walkforward.json",
]

_dataset_info_cache: Dict[str, Dict[str, Any]] = {}


def _dataset_info(key: str) -> Dict[str, Any]:
    """Cheap dataset fingerprint: manifest if present, else a cached CSV scan."""
    spec = DATASETS.get(key)
    if not spec:
        return {}
    if key in _dataset_info_cache:
        return _dataset_info_cache[key]
    info: Dict[str, Any] = {"key": key, "label": spec["label"]}
    data_rel = spec.get("data")
    if not data_rel:
        info.update({"kind": "demo"})
        _dataset_info_cache[key] = info
        return info
    path = PROJECT_ROOT / str(data_rel)
    info["path"] = str(data_rel)
    manifest = _read_json(Path(str(path) + ".manifest.json"), {})
    if manifest:
        info["rows"] = manifest.get("rows")
        info["first_date"] = manifest.get("first_date")
        info["last_date"] = manifest.get("last_date")
        info["sha256"] = str(manifest.get("sha256", ""))[:12]
        info["size_mb"] = round(float(manifest.get("size_bytes", 0)) / 1024 / 1024, 1)
    _dataset_info_cache[key] = info
    return info


@app.get("/")
def index() -> str:
    return HTML_PAGE


@app.get("/m")
def mobile() -> str:
    return MOBILE_PAGE


@app.post("/api/forward-test/freeze")
def api_forward_freeze() -> Any:
    """Freeze the latest model targets for an out-of-sample forward test."""
    if not _last_result or not _last_result.get("targets"):
        return jsonify({"ok": False, "error": "先运行一次回测，再冻结目标组合"}), 400
    from stockquant.assistant.forward_test import freeze_targets
    targets = {str(s): float(w) for s, w in _last_result.get("targets") or []}
    as_of = str(_last_result.get("target_date") or (_last_result.get("period") or ["", ""])[-1])
    freeze_targets(targets, as_of)
    return jsonify({"ok": True, "as_of": as_of, "n": len(targets)})


@app.get("/api/stock/<symbol>")
def api_stock_detail(symbol: str) -> Any:
    """Per-stock factor breakdown: why the model scored this name the way it did."""
    key = str(_monitor_store.load_settings().get("dataset") or next(iter(DATASETS)))
    if key not in DATASETS:
        key = next(iter(DATASETS))
    market = _get_market(key)
    date = market.dates[-1] if market.dates else None
    if not date:
        return jsonify({"available": False})
    from stockquant.factors import FactorEngine
    config = _config_from_payload({"dataset": key, "preset": "value_low_vol"})
    engine = FactorEngine(
        factors=config.factors,
        min_history=config.min_history,
        sector_neutral=config.sector_neutral,
    )
    components, diag = engine.components(market, date)
    symbol = str(symbol).upper()
    factors = []
    for f in config.factors:
        z = components.get(f.name, {}).get(symbol)
        factors.append({"name": f.name, "z": round(float(z), 4) if z is not None else None,
                        "weight": round(float(f.weight), 3), "desc": f.description})
    scores, _ = engine.score(market, date)
    bar = market.bar(symbol, date)
    return jsonify(_html_safe({
        "available": bool(bar is not None),
        "symbol": symbol,
        "date": date,
        "score": round(float(scores.get(symbol, 0.0)), 4) if scores.get(symbol) is not None else None,
        "factors": factors,
        "close": round(float(bar.close), 2) if bar else None,
        "pe": bar.pe if bar else None,
        "pb": bar.pb if bar else None,
        "roe": bar.roe if bar else None,
        "sector": bar.sector if bar else "Unknown",
    }))


@app.get("/api/forward-test")
def api_forward_evaluate() -> Any:
    """Evaluate the frozen targets against realised forward returns."""
    from stockquant.assistant.forward_test import evaluate_frozen
    key = str(_monitor_store.load_settings().get("dataset") or next(iter(DATASETS)))
    if key not in DATASETS:
        key = next(iter(DATASETS))
    try:
        market = _get_market(key)
    except Exception as exc:
        return jsonify({"available": False, "error": f"{type(exc).__name__}: {exc}"})
    return jsonify(_html_safe(evaluate_frozen(market)))


@app.get("/api/convertible")
def api_convertible() -> Any:
    """可转债双低轮动：历史回测 + 本期买入清单。"""
    data = _read_json(OUTPUTS_ROOT / "stockquant_cb" / "double_low.json", {})
    if not data:
        return jsonify({"available": False})
    return jsonify(_html_safe({
        "available": True,
        "strategy": data.get("strategy"),
        "params": data.get("params", {}),
        "metrics": data.get("metrics", {}),
        "yearly": data.get("yearly", {}),
        "curve": data.get("curve", []),
        "picks": data.get("picks", []),
        "picks_asof": data.get("picks_asof"),
        "excluded_redeem": data.get("excluded_redeem", []),
        "excluded_st": data.get("excluded_st", []),
        "redeem_risk": (data.get("live") or {}).get("redeem_risk", {}),
        "issues": (data.get("live") or {}).get("issues", []),
        "live_asof": (data.get("live") or {}).get("asof"),
        "rules": data.get("rules", {}),
        "generated_at": data.get("generated_at"),
        "data_age_days": data.get("data_age_days"),
        "data_lag_trading_days": data.get("data_lag_trading_days"),
        "data_lag_source": data.get("data_lag_source"),
        "last_trading_day": data.get("last_trading_day"),
    }))


@app.get("/api/etf")
def api_etf() -> Any:
    """ETF 执行验证试点：本期清单 + 当前持仓 + 硬执行门控。"""
    base = OUTPUTS_ROOT / "stockquant_etf"
    settings = _read_json(base / "settings.json", {})
    plan = _read_json(base / "plan.json", {})
    plan_gate = evaluate_plan_gate(plan, settings, now=datetime.now(ZoneInfo("Asia/Shanghai")))
    if not plan:
        return jsonify(_html_safe({
            "available": False,
            "plan_gate": plan_gate,
            "capital_gate": {
                "configured_capital": settings.get("capital"),
                "min_executable": settings.get("min_executable_capital") or 50000,
                "ok": False,
                "reasons": plan_gate.get("reasons") or [],
            },
        }))
    holdings = _read_json(base / "holdings.json", {})
    report = _read_json(base / "execution_report.json", {})
    pending = filled = 0
    log_path = base / "execution_log.csv"
    if plan_gate.get("ok") and log_path.exists():
        try:
            with log_path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                fields = reader.fieldnames or []
                for row in reader:
                    if "plan_id" in fields:
                        matches = str(row.get("plan_id") or "") == str(plan.get("plan_id") or "")
                    else:
                        matches = False
                    if not matches:
                        continue
                    if str(row.get("filled_shares") or "").strip():
                        filled += 1
                    else:
                        pending += 1
        except Exception:
            pass
    executable = bool(plan_gate.get("ok"))
    paper_window_expired = any(
        reason in {"plan_expired", "execute_date_passed"} for reason in (plan_gate.get("reasons") or [])
    )
    return jsonify(_html_safe({
        "available": True,
        "asof": plan.get("asof"),
        "data_asof": plan.get("data_asof") or plan.get("asof"),
        "capital": plan.get("capital"),
        "targets": plan.get("targets") or {},
        "design_targets": plan.get("design_targets") or {},
        "prices": plan.get("prices") or {},
        # Never expose an order list as executable when the hard gate failed.
        "orders": (plan.get("orders") or []) if executable else [],
        # Paper/demo orders are kept in a separate field so they can never be
        # confused with an executable order list.
        "paper_orders": ((plan.get("orders") or [])
                         if str(plan.get("status") or "") == "demo" and not paper_window_expired
                         else []),
        "orders_count": len(plan.get("orders") or []),
        "l1_error": plan.get("l1_error"),
        "cash_residual": plan.get("cash_residual"),
        "dropped": plan.get("dropped_sleeves") or {},
        "estimated_commission": plan.get("estimated_commission"),
        "metrics": {k: v for k, v in (plan.get("metrics") or {}).items()
                    if k in ("cagr", "sharpe", "max_drawdown", "annualized_turnover")},
        "plan_gate": plan_gate,
        "executable": executable,
        "plan_status": plan.get("status"),
        "plan_id": plan.get("plan_id"),
        "generated_at": plan.get("generated_at"),
        "execute_on": plan.get("execute_on"),
        "expires_at": plan.get("expires_at"),
        "data_fresh": plan.get("data_fresh"),
        "latest_completed_trading_day": plan.get("latest_completed_trading_day"),
        "blocked_reasons": ((plan.get("blocked_reasons") or [])
                            if str(plan.get("status") or "") == "blocked"
                            else (plan_gate.get("messages") or [])),
        "capital_gate": {
            "configured_capital": settings.get("capital"),
            "min_executable": plan_gate.get("min_capital"),
            "ok": executable,
            "reasons": plan_gate.get("reasons") or [],
        },
        "holdings": holdings.get("shares") or {},
        "holdings_updated_at": holdings.get("updated_at"),
        "execution": {
            "pending": pending,
            "filled": filled,
            "fill_rate": report.get("fill_rate"),
            "slippage_median": report.get("slippage_median"),
            "slippage_p90": report.get("slippage_p90"),
            "verdict": report.get("verdict"),
        },
    }))


@app.get("/api/cb/research")
def api_cb_research() -> Any:
    """转债策略体检：钱从哪来 / 有多可信 / 参数 / 失效规则 / 打新（都是脚本产出的 JSON）。"""
    base = OUTPUTS_ROOT / "stockquant_cb"

    def _load(name):
        return _read_json(base / name, {})

    race = _load("variant_race.json")
    if isinstance(race, list):
        # 收益序列只在 DSR/PBO 脚本里用，别往浏览器塞（每条约 376 个点）
        race = [{k: v for k, v in r.items() if k not in ("returns", "dates")} for r in race]
    return jsonify(_html_safe({
        "attribution": _load("attribution.json"),
        "race": race,
        "dsr_pbo": _load("dsr_pbo.json"),
        "ensemble": _load("param_ensemble.json"),
        "failure_rules": _load("failure_rules.json"),
        "push_ipo": _load("push_ipo_stats.json"),
        "score_sensitivity": _load("worth_score_sensitivity.json"),
    }))


@app.get("/api/cb/holdings")
def api_cb_holdings() -> Any:
    """我的转债持仓：按最新价估值 + 双低排名 + 强赎/ST/急跌 预警 + 建议动作。"""
    try:
        _record_cb_nav()   # 每天记一次净值，用于回撤熔断
    except Exception:
        pass
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    ext = ledger.load().get("external_positions", {}) or {}
    rows = _cb_position_rows(ext)
    total_cost = sum(r["cost"] * r["shares"] for r in rows)
    total_value = sum(r["market_value"] for r in rows)
    return jsonify(_html_safe({"rows": rows, "total_cost": round(total_cost, 1),
                               "total_value": round(total_value, 1),
                               "pnl_pct": round(total_value / total_cost - 1.0, 4) if total_cost > 0 else 0.0,
                               "advice_asof": (_cb_report().get("picks_asof")),
                               "warnings": sum(1 for r in rows if r.get("severity") in ("critical", "warning"))}))


@app.post("/api/cb/holding")
def api_cb_holding() -> Any:
    payload = request.get_json(force=True) or {}
    code = str(payload.get("code", "")).strip()
    shares = int(payload.get("shares", 0) or 0)
    price = float(payload.get("price", 0.0) or 0.0)
    if not code or shares <= 0 or price <= 0:
        return jsonify({"ok": False, "error": "code/shares/price required"}), 400
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    ledger.add_holding(code, shares, price, note="cb_buy")
    return jsonify({"ok": True})


@app.post("/api/cb/sell")
def api_cb_sell() -> Any:
    payload = request.get_json(force=True) or {}
    code = str(payload.get("code", "")).strip()
    shares = int(payload.get("shares", 0) or 0)
    if not code or shares <= 0:
        return jsonify({"ok": False, "error": "code/shares required"}), 400
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    try:
        ledger.reduce_holding(code, shares, float(payload.get("price", 0.0) or 0.0), note="cb_sell")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


def _cb_report() -> Dict[str, Any]:
    return _read_json(OUTPUTS_ROOT / "stockquant_cb" / "double_low.json", {})


_CB_PANEL_CACHE: Dict[str, Any] = {}
_CB_PANEL_LOCK = threading.Lock()


def _cb_panel(data_csv: str, volume_csv: str):
    """转债面板只加载一次（79 万行，几十毫秒→秒级），按文件 mtime 失效。"""
    try:
        stat = Path(data_csv).stat()
        vstat = Path(volume_csv).stat() if Path(volume_csv).exists() else None
        key = (str(data_csv), stat.st_mtime, stat.st_size,
               str(volume_csv), vstat.st_mtime if vstat else None, vstat.st_size if vstat else None)
    except Exception:
        key = (str(data_csv), str(volume_csv))
    with _CB_PANEL_LOCK:
        cached = _CB_PANEL_CACHE.get("key")
        if cached != key:
            from stockquant.convertible.double_low import _load_panel
            _CB_PANEL_CACHE["data"] = _load_panel(data_csv, volume_csv)
            _CB_PANEL_CACHE["key"] = key
        return _CB_PANEL_CACHE["data"]


def _cb_params() -> Dict[str, Any]:
    """实盘清单参数（以报告为准，缺省用回测默认值）。"""
    from stockquant.convertible.positions import DEFAULTS
    params = dict(DEFAULTS)
    p = (_cb_report().get("params") or {})
    for k in DEFAULTS:
        if p.get(k) is not None:
            params[k] = p[k]
    return params


def _cb_position_rows(ext: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把持仓(含成本) → 转债盯盘建议行。数据缺失时返回空表而不报错。"""
    if not ext:
        return []
    from stockquant.convertible.positions import is_cb_code
    cb_holds = {str(c): v for c, v in ext.items() if is_cb_code(c)}
    if not cb_holds:
        return []
    try:
        from stockquant.convertible.positions import analyze_positions
        panel = _cb_panel(str(PROJECT_ROOT / "data" / "real" / "convertible_bonds_hist.csv"),
                          str(PROJECT_ROOT / "data" / "real" / "cb_volume.csv"))
        return analyze_positions(cb_holds,
                                 data_csv=str(PROJECT_ROOT / "data" / "real" / "convertible_bonds_hist.csv"),
                                 volume_csv=str(PROJECT_ROOT / "data" / "real" / "cb_volume.csv"),
                                 live_json=str(PROJECT_ROOT / "data" / "real" / "cb_live.json"),
                                 params=_cb_params(),
                                 panel=panel)
    except Exception as exc:  # 数据缺失也不能让持仓面板整体 500
        return [{"code": str(c), "name": "（行情不可用）", "shares": int(v.get("shares", 0) or 0),
                 "cost": round(float(v.get("average_cost", 0) or 0), 3),
                 "price": round(float(v.get("average_cost", 0) or 0), 3), "stale": True,
                 "pnl_pct": 0.0, "market_value": 0.0, "premium": None, "double_low": None,
                 "pure_bond_premium": None, "rating": "", "rank": None, "rank_universe": 0,
                 "in_top_n": False, "drop_20d": None, "redeem_status": None, "redeem_last_day": None,
                 "st_stock": None, "cheap_stock_price": None,
                 "action": "无数据", "severity": "warning",
                 "reasons": [f"无法读取转债行情：{type(exc).__name__}"]}
                for c, v in cb_holds.items()]


@app.get("/api/cb/risk")
def api_cb_risk() -> Any:
    """转债策略风控：回撤熔断规则 + 仓位公式。"""
    report = _cb_report()
    curve = report.get("curve") or []
    # 策略回测的当前回撤（信息用）
    peak = 0.0
    dd = 0.0
    for pt in curve:
        v = float(pt.get("equity") or 0)
        peak = max(peak, v)
        if peak > 0:
            dd = min(dd, v / peak - 1.0)
    # 用户自己的净值（≥2 天且带成本记录，才谈得上"我的回撤"）。
    # 用 市值/成本 归一化，避免"加钱/取钱"被误判成盈利/回撤。
    pts = _nav_points()
    navs = _nav_series()
    usable = [x for x in pts if x.get("nav") is not None]
    user_dd = 0.0
    have_user = len(usable) >= 2
    if have_user:
        pk = 0.0
        for e in usable:
            v = float(e["nav"])
            pk = max(pk, v)
            if pk > 0:
                user_dd = min(user_dd, v / pk - 1.0)
    # 用户持仓的总盈亏（相对成本，立刻可得）
    user_pnl = None
    try:
        ext = PortfolioLedger(str(STATE_DIR / "portfolio.json")).load().get("external_positions", {}) or {}
        rows = _cb_position_rows(ext)
        cost = sum(r["cost"] * r["shares"] for r in rows)
        val = sum(r["market_value"] for r in rows)
        if cost > 0:
            user_pnl = val / cost - 1.0
    except Exception:
        pass
    # 有自己持仓 -> 按自己的回撤判；没有 -> 按策略回测（仅参考）
    if have_user:
        basis = f"我的持仓（{len(usable)} 天记录）"
    elif user_pnl is not None:
        basis = f"策略回测（我的记录中 {len(pts)}/2 天，攒够 2 天自动切换）"
    else:
        basis = "策略回测（还没记录持仓）"
    ref_dd = user_dd if have_user else dd
    level, action = "正常", "维持满仓"
    if ref_dd <= -0.20:
        level, action = "警戒", "清仓复盘：停止交易，逐笔复盘后再决定是否重启"
    elif ref_dd <= -0.15:
        level, action = "减仓", "仓位减半：卖出一半，等回撤收窄再恢复"
    return jsonify({
        "level": level, "action": action,
        "based_on": basis,
        "user_pnl": None if user_pnl is None else round(user_pnl, 4),
        "have_holdings": user_pnl is not None,
        "strategy_drawdown": round(dd, 4),
        "user_drawdown": round(user_dd, 4),
        "user_nav_points": len(navs),
        "user_nav_points_usable": len(usable),
        "nav_basis": "市值/成本（已剔除出入金影响）" if usable else "原始市值（旧记录，下一个交易日起自动切换）",
        "rules": [
            {"threshold": "-15%", "do": "仓位减半"},
            {"threshold": "-20%", "do": "清仓 + 复盘"},
        ],
        "position_formula": "可投金额 = 你亏 20% 不影响生活的金额 ÷ 0.20",
    })


_NAV_CACHE: Dict[str, Any] = {"key": None, "data": [], "lock": threading.Lock()}


def _nav_series() -> List[Dict[str, Any]]:
    """用户自己的转债净值记录（每天一行 JSON）。

    加锁 + 按 mtime 缓存：持仓页、风控、盯盘都会读，没必要每次重读整个文件。
    """
    p = STATE_DIR / "cb_nav.jsonl"
    try:
        stat = p.stat()
        key = (stat.st_mtime, stat.st_size)
    except Exception:
        key = None
    with _NAV_CACHE["lock"]:
        if _NAV_CACHE["key"] == key and key is not None:
            return list(_NAV_CACHE["data"])
    out: List[Dict[str, Any]] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines()[-500:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    with _NAV_CACHE["lock"]:
        _NAV_CACHE["key"] = key
        _NAV_CACHE["data"] = out
        return list(out)


def _nav_points() -> List[Dict[str, Any]]:
    """把原始记录 → 归一化净值点（value/cost），出入金不会污染回撤。"""
    pts = []
    for e in _nav_series():
        try:
            value = float(e.get("value") or 0.0)
        except Exception:
            continue
        try:
            cost = float(e.get("cost") or 0.0)
        except Exception:
            cost = 0.0
        if value <= 0:
            continue
        # 老记录没有 cost 字段 -> 退化用绝对市值（并在界面标注）
        pts.append({"date": e.get("date"), "nav": (value / cost) if cost > 0 else None,
                    "value": value, "cost": cost})
    return pts


def _record_cb_nav() -> None:
    """记录当天持仓市值 + 成本（每天覆盖一次），用于回撤熔断。"""
    from stockquant.convertible.positions import is_cb_code
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    ext = ledger.load().get("external_positions", {}) or {}
    if not {str(c) for c in ext if is_cb_code(c)}:
        return
    rows = _cb_position_rows(ext)
    value = sum(r["market_value"] for r in rows)
    cost = sum(r["cost"] * r["shares"] for r in rows)
    if value <= 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    entry = {"date": today, "value": round(value, 1), "cost": round(cost, 1)}
    series = _nav_series()
    if series and series[-1].get("date") == today:
        series[-1] = entry
    else:
        series.append(entry)
    p = STATE_DIR / "cb_nav.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in series[-500:]) + "\n", encoding="utf-8")


@app.get("/api/cb/paper")
def api_cb_paper() -> Any:
    """paper 对账：本周清单的实际成交价 vs 模型价，累计滑点与执行率。"""
    report = _cb_report()
    picks = report.get("picks") or []
    week = report.get("picks_asof")
    records = []
    pth = STATE_DIR / "cb_paper.jsonl"
    if pth.exists():
        for line in pth.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    # 同一周可能被重复保存（用户修正/补填佣金）——每周只保留最后一次，
    # 否则同一个成交价会被重复计入滑点统计（2026-10-13 发现）。
    _latest: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        w = str(rec.get("week") or "")
        if w:
            _latest[w] = rec
    records = [_latest[w] for w in sorted(_latest)]
    this_week = _latest.get(str(week)) if week else None
    fills = {f["code"]: f for f in (this_week or {}).get("fills", [])}
    # aggregate slippage over all recorded weeks
    slips = []
    exec_codes = 0
    total_codes = 0
    for rec in records:
        for f in rec.get("fills", []):
            total_codes += 1
            if f.get("filled"):
                exec_codes += 1
            mp, ap = f.get("model_price"), f.get("actual_price")
            if f.get("filled") and isinstance(mp, (int, float)) and isinstance(ap, (int, float)) and mp:
                slips.append(ap / mp - 1.0)
    slips.sort()
    median_slip = slips[len(slips) // 2] if slips else 0.0
    # 佣金：券商常有"最低 5 元/笔"，小资金下真实成本可能远高于 5bps 假设 —— 逐笔记出来
    # 纯执行成本 = 半个买卖价差 + 佣金。
    # 与"滑点 vs 模型价"不同：模型价是上一交易日收盘价，两者之差里含隔夜跳空，
    # 那不是成本（跳空对你有利时"滑点"为负，把它当成本会误判，2026-10-13 踩到）。
    spreads = []
    for rec in records:
        for f in rec.get("fills", []):
            a, b = f.get("ask1"), f.get("bid1")
            if f.get("filled") and a and b and float(a) > 0 and float(b) > 0:
                mid = (float(a) + float(b)) / 2.0
                spreads.append((float(a) - float(b)) / mid)
    spreads.sort()
    median_spread = spreads[len(spreads) // 2] if spreads else 0.0
    total_fee = sum(float(f.get("fee") or 0.0) for rec in records for f in rec.get("fills", []))
    fee_notional = sum(float(f.get("model_price") or 0.0) * 10 for rec in records
                       for f in rec.get("fills", []) if f.get("filled") and f.get("model_price"))
    out_rows = []
    for p in picks:
        f = fills.get(p["bond_code"], {})
        out_rows.append({**p, "actual_price": f.get("actual_price"), "filled": f.get("filled"),
                         "fee": f.get("fee")})
    return jsonify(_html_safe({
        "week": week, "rows": out_rows,
        "weeks": len(records), "median_slippage": round(median_slip, 5),
        "total_fee": round(total_fee, 2),
        "fee_bps": round(total_fee / fee_notional * 10000, 2) if fee_notional > 0 else None,
        "median_spread_bps": round(median_spread * 10000, 2),
        "one_way_cost_bps": round(median_spread / 2 * 10000 + (total_fee / fee_notional * 10000 if fee_notional > 0 else 0), 2),
        "fill_rate": round(exec_codes / total_codes, 4) if total_codes else None,
        # 判据用"单边执行成本"（价差一半+佣金）< 30bps，而不是含隔夜跳空的"滑点"
        "gate": {"cost_ok": (median_spread / 2 * 10000 + (total_fee / fee_notional * 10000 if fee_notional > 0 else 0)) < 30,
                 "fill_ok": (exec_codes / total_codes if total_codes else 0) > 0.9,
                 "weeks_needed": 6,
                 "note": "滑点含隔夜跳空，仅供参考；判据看单边执行成本。"},
    }))


@app.get("/api/cb/paper/snapshot")
def api_cb_paper_snapshot() -> Any:
    """抓当前真实盘口（新浪），给 paper 对账一键填入「实际可成交价」= 卖一价。

    纸上对账要回答的问题是"我到底能不能按清单上的价买到"，
    所以真正有用的数字不是收盘价，而是**下单那一刻的卖一价**（买方要付的价）。
    """
    report = _cb_report()
    picks = report.get("picks") or []
    codes = [str(p.get("bond_code")) for p in picks if p.get("bond_code")]
    quotes: Dict[str, Any] = {}
    err = None
    if codes:
        try:
            quotes = fetch_realtime_quotes(codes)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

    def _pick(sym_prefix: str):
        for sym, q in quotes.items():
            if str(sym).split(".")[0] == sym_prefix:
                return q
        return None

    rows = []
    for p in picks:
        code = str(p.get("bond_code"))
        q = _pick(code) or {}
        ask1 = float(q.get("ask1") or 0.0)
        bid1 = float(q.get("bid1") or 0.0)
        last = float(q.get("price") or 0.0)
        rows.append({
            "bond_code": code,
            "bond_name": p.get("bond_name"),
            "model_price": p.get("price"),
            "ask1": ask1 or None,        # 买方的成交价（推荐用它做"实际成交价"）
            "bid1": bid1 or None,
            "last": last or None,
            "quote_time": (f"{q.get('date')} {q.get('time')}" if q else None),
            "fill_price": ask1 or last or None,   # 没盘口时退化成最新价
        })
    return jsonify(_html_safe({
        "rows": rows,
        "source": "sina",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": err,
        "no_quote": sum(1 for r in rows if not r["fill_price"]),
    }))


@app.post("/api/cb/paper/record")
def api_cb_paper_record() -> Any:
    payload = request.get_json(force=True) or {}
    week = str(payload.get("week") or "")
    fills = payload.get("fills") or []
    if not week or not fills:
        return jsonify({"ok": False, "error": "week and fills required"}), 400
    pth = STATE_DIR / "cb_paper.jsonl"
    pth.parent.mkdir(parents=True, exist_ok=True)
    with pth.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"week": week, "fills": fills, "recorded_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
    return jsonify({"ok": True})


@app.get("/api/health")
def api_health() -> Any:
    """Liveness/readiness for the watchdog and the user."""
    return jsonify({
        "ok": True,
        "uptime_seconds": round(time.time() - _started_at, 1),
        "monitor_enabled": bool(_monitor_store.load_settings().get("enabled")),
        "monitor_running": bool(_monitor_state.get("running")),
        "monitor_last_error": _monitor_state.get("last_error"),
        "cached_datasets": sorted(_data_cache.keys()),
        "has_last_backtest": bool(_last_result),
    })


@app.get("/family")
def family() -> str:
    return FAMILY_PAGE


@app.get("/api/family")
def api_family() -> Any:
    """家人版：只有清单 / 打新 / 风险提醒，无研究内容。"""
    report = _cb_report()
    live = report.get("live") or {}
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    ext = ledger.load().get("external_positions", {}) or {}
    held = {str(k) for k in ext if str(k).isdigit()}
    redeem = live.get("redeem_risk", {}) or {}
    warnings = [{"code": c, "name": v.get("name"), "last_day": v.get("last_trade_date")}
                for c, v in redeem.items() if c in held]
    return jsonify(_html_safe({
        "asof": report.get("picks_asof"),
        "metrics": {k: report.get("metrics", {}).get(k) for k in ("cagr", "sharpe", "max_drawdown")},
        "picks": report.get("picks", []),
        "issues": live.get("issues", []),
        "warnings": warnings,
        "risklvl": _cb_report().get("metrics", {}).get("max_drawdown"),
    }))


@app.get("/guide")
def guide() -> str:
    return GUIDE_PAGE


@app.get("/api/options")
def api_options() -> Any:
    return jsonify(
        {
            "datasets": [
                {"key": key, "label": value["label"], "start": value["start"], "end": value["end"]}
                for key, value in DATASETS.items()
            ],
            "presets": sorted(CANDIDATES),
        }
    )


@app.get("/api/quant/llm/status")
def api_quant_llm_status() -> Any:
    client = build_llm_client()
    return jsonify({
        "mode": "mock" if type(client).__name__ == "MockLLMClient" else "provider",
        "client": type(client).__name__,
        "read_only": True,
        "note": "Real provider needs STOCKQUANT_LLM_PROVIDER and an API key in an environment variable.",
    })


@app.post("/api/quant/ask")
def api_quant_ask() -> Any:
    payload = request.get_json(force=True) or {}
    dataset_key = str(payload.get("dataset") or "csi300_full")
    if dataset_key not in DATASETS:
        return jsonify({"ok": False, "error": f"unknown dataset: {dataset_key}"}), 400
    question = str(payload.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "question is required"}), 400
    try:
        market = _get_market(dataset_key)
        session_id = str(payload.get("session_id") or "").strip() or ("qa-" + uuid.uuid4().hex[:12])
        with _quant_session_lock:
            history = list((_quant_sessions.get(session_id) or {}).get("history") or [])
        assistant = QuantAssistant(
            market,
            dataset_key,
            str(payload.get("as_of") or DATASETS[dataset_key]["end"]),
            llm=build_llm_client(),
            log_path=str(STATE_DIR / "llm_calls.jsonl"),
        )
        result = assistant.ask(
            question,
            as_of=payload.get("as_of"),
            positions=payload.get("positions") or None,
            history=history,
        )
        with _quant_session_lock:
            _quant_sessions[session_id] = {"history": result.get("history") or [],
                                           "updated_at": datetime.now(timezone.utc).isoformat()}
        result["session_id"] = session_id
        return jsonify({"ok": True, "result": _html_safe(result)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/quant/session/clear")
def api_quant_session_clear() -> Any:
    payload = request.get_json(force=True) or {}
    session_id = str(payload.get("session_id") or "").strip()
    with _quant_session_lock:
        _quant_sessions.pop(session_id, None)
    return jsonify({"ok": True, "cleared": session_id})


@app.get("/api/quant/capabilities")
def api_quant_capabilities() -> Any:
    return jsonify({
        "mode": "read_only",
        "implemented": ["factor_snapshot", "portfolio_risk", "stress_test", "backtest_summary"],
        "datasets": sorted(DATASETS),
        "note": "No order placement, no strategy mutation, no secret access.",
    })


@app.get("/api/quant/factor/<symbol>")
def api_quant_factor(symbol: str) -> Any:
    dataset_key = str(request.args.get("dataset") or "csi300_full")
    if dataset_key not in DATASETS:
        return jsonify({"ok": False, "error": f"unknown dataset: {dataset_key}"}), 400
    as_of = str(request.args.get("as_of") or DATASETS[dataset_key]["end"])
    try:
        market = _get_market(dataset_key)
        data = factor_snapshot(market, symbol, as_of,
                               neutralize_by=str(request.args.get("neutralize_by") or "cap_decile"))
        return jsonify({"ok": True, "result": _html_safe(data)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/quant/portfolio-risk")
def api_quant_portfolio_risk() -> Any:
    payload = request.get_json(force=True) or {}
    dataset_key = str(payload.get("dataset") or "csi300_full")
    if dataset_key not in DATASETS:
        return jsonify({"ok": False, "error": f"unknown dataset: {dataset_key}"}), 400
    try:
        market = _get_market(dataset_key)
        data = portfolio_risk(market, payload.get("positions") or {}, str(payload.get("as_of") or ""))
        return jsonify({"ok": True, "result": _html_safe(data)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/quant/stress-test")
def api_quant_stress_test() -> Any:
    payload = request.get_json(force=True) or {}
    dataset_key = str(payload.get("dataset") or "csi300_full")
    if dataset_key not in DATASETS:
        return jsonify({"ok": False, "error": f"unknown dataset: {dataset_key}"}), 400
    try:
        market = _get_market(dataset_key)
        data = stress_test(
            market,
            payload.get("positions") or {},
            str(payload.get("as_of") or ""),
            payload.get("scenarios"),
        )
        return jsonify({"ok": True, "result": _html_safe(data)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/quant/backtest-summary")
def api_quant_backtest_summary() -> Any:
    """Run the existing backtest and return a compact, read-only summary."""
    try:
        payload = request.get_json(force=True) or {}
        result = _run_backtest(payload)
        return jsonify({"ok": True, "result": {
            "dataset": result.get("dataset"),
            "period": result.get("period"),
            "metrics": result.get("metrics"),
            "targets": result.get("targets"),
            "target_date": result.get("target_date"),
            "trade_count": result.get("trade_count"),
            "warnings": result.get("warnings"),
            "config": result.get("config"),
        }})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.post("/api/backtest")
def api_backtest() -> Any:
    try:
        payload = request.get_json(force=True) or {}
        return jsonify({"ok": True, "result": _html_safe(_run_backtest(payload))})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.get("/api/backtest/last")
def api_backtest_last() -> Any:
    """Return the most recent backtest without recomputing (fast page load)."""
    if not _last_result:
        return jsonify({"available": False})
    return jsonify({"available": True, "result": _html_safe(_last_result)})


@app.get("/api/ic")
def api_ic() -> Any:
    path, data = _first_json(IC_CANDIDATES, {"summary": {}})
    summary = data.get("summary", {})
    rows = []
    for name, metrics in sorted(summary.items(), key=lambda kv: -abs(kv[1].get("t_stat_nw", 0) or 0)):
        rows.append(
            {
                "factor": name,
                "mean_ic": _json_float(metrics.get("mean_ic")),
                "icir": _json_float(metrics.get("ic_ir")),
                "t_stat": _json_float(metrics.get("t_stat")),
                "t_stat_nw": _json_float(metrics.get("t_stat_nw")),
                "p_value": _json_float(metrics.get("p_value"), 1.0),
                "p_value_bhy": _json_float(metrics.get("p_value_bhy"), 1.0),
                "long_short": _json_float(metrics.get("mean_long_short")),
                "observations": _json_float(metrics.get("observations")),
            }
        )
    return jsonify({"rows": rows, "source": str(path) if path else None})


@app.get("/api/walkforward")
def api_walkforward() -> Any:
    path, data = _first_json(WALKFORWARD_CANDIDATES, {})
    if not data:
        return jsonify({"available": False})
    oos = data.get("oos_metrics", {})
    validation = data.get("validation", {}) or {}
    pbo = validation.get("pbo", {}) or {}
    track = validation.get("track_record", {}) or {}
    return jsonify(
        {
            "available": True,
            "source": str(path) if path else None,
            "windows": len(data.get("windows", [])),
            "cagr": _json_float(oos.get("cagr")),
            "sharpe": _json_float(oos.get("sharpe")),
            "max_drawdown": _json_float(oos.get("max_drawdown")),
            "excess_cagr": _json_float(oos.get("excess_cagr")),
            "beta": _json_float(oos.get("beta")),
            "alpha": _json_float(oos.get("alpha")),
            "information_ratio": _json_float(oos.get("information_ratio")),
            "deflated_sharpe": _json_float(oos.get("deflated_sharpe_ratio")),
            "probabilistic_sharpe": _json_float(oos.get("probabilistic_sharpe_ratio")),
            "trials": _json_float(oos.get("candidate_trials")),
            "pbo": _json_float(oos.get("pbo", pbo.get("pbo", 0.0))),
            "min_track_record_length_years": _json_float(oos.get("min_track_record_length_years", track.get("min_track_record_length_years"))),
            "min_backtest_length_years": _json_float(oos.get("min_backtest_length_years", track.get("min_backtest_length_years"))),
            "haircut_sharpe_annual": _json_float(oos.get("haircut_sharpe_annual", track.get("haircut_sharpe_annual"))),
            "oos_skewness": _json_float(oos.get("oos_skewness", track.get("skewness"))),
            "oos_kurtosis": _json_float(oos.get("oos_kurtosis", track.get("kurtosis"))),
            "sharpe_se_annual": _json_float(oos.get("sharpe_se_annual")),
            "sharpe_t_stat": _json_float(oos.get("sharpe_t_stat")),
            "sharpe_t_stat_iid": _json_float(oos.get("sharpe_t_stat_iid")),
            "autocorrelation_factor": _json_float(oos.get("autocorrelation_factor"), 1.0),
            "dsr_autocorr_adjusted": _json_float(oos.get("dsr_autocorr_adjusted")),
            "trial_accounting": (validation.get("trial_accounting") or {}),
        }
    )


def _market_status() -> Dict[str, Any]:
    """A-share trading-session state in Asia/Shanghai."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = None
    now = datetime.datetime.now(tz)
    hhmm = now.hour * 60 + now.minute
    is_weekday = now.weekday() < 5
    if not is_weekday:
        state = "休市（周末）"
    elif 570 <= hhmm < 690:
        state = "交易中 · 上午"
    elif 780 <= hhmm < 900:
        state = "交易中 · 下午"
    elif hhmm < 570:
        state = "未开盘"
    elif 690 <= hhmm < 780:
        state = "午间休市"
    else:
        state = "已收盘"
    return {"state": state, "time": now.strftime("%H:%M")}


def _market_is_open() -> bool:
    """True only while A-share prices can actually move (weekday sessions)."""
    st = _market_status()
    return st["state"].startswith("交易中")


@app.get("/api/market-status")
def api_market_status() -> Any:
    return jsonify(_market_status())


@app.get("/api/backtest/export")
def api_backtest_export() -> Any:
    """Download the most recent backtest as CSV (dates, equity, benchmark)."""
    result = _last_result
    if not result:
        return jsonify({"ok": False, "error": "no backtest has run yet"}), 404
    dates = result.get("dates") or []
    equity = result.get("equity") or []
    benchmark = result.get("benchmark") or []
    lines = ["date,equity,benchmark"]
    for i, d in enumerate(dates):
        eq = equity[i] if i < len(equity) else ""
        bn = benchmark[i] if i < len(benchmark) else ""
        lines.append(f"{d},{eq},{bn}")
    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=stockquant_backtest.csv"},
    )


@app.get("/api/attribution")
def api_attribution() -> Any:
    path, data = _first_json(ATTRIBUTION_CANDIDATES, {})
    if not data:
        return jsonify({"available": False})
    bn = data.get("beta_neutral", {}) or {}
    return jsonify({
        "available": True,
        "observations": int(_json_float(data.get("observations", 0))),
        "r_squared": _json_float(data.get("r_squared")),
        "residual_alpha_annual": _json_float(data.get("alpha_annual")),
        "exposures": data.get("exposures", {}),
        "beta": _json_float(bn.get("beta")),
        "hedged_alpha_annual": _json_float(bn.get("alpha_annual")),
        "hedged_sharpe_annual": _json_float(bn.get("hedged_sharpe_annual")),
        "hedged_t_stat": _json_float(bn.get("hedged_t_stat")),
    })


@app.get("/api/dataset-info")
def api_dataset_info() -> Any:
    key = str(request.args.get("key") or next(iter(DATASETS)))
    return jsonify(_dataset_info(key))


@app.get("/api/experiments")
def api_experiments() -> Any:
    candidates = [
        ("forecast/quality full", OUTPUTS_ROOT / "stockquant_hist80_v3_fq_full" / "metrics.json"),
        ("forecast/quality holdout", OUTPUTS_ROOT / "stockquant_hist80_v3_fq_holdout" / "metrics.json"),
        ("balanced blend30 full", OUTPUTS_ROOT / "stockquant_hist80_v3_blend30_full" / "metrics.json"),
        ("balanced blend30 holdout", OUTPUTS_ROOT / "stockquant_hist80_v3_blend30_holdout" / "metrics.json"),
        ("walk-forward forecast/quality", OUTPUTS_ROOT / "stockquant_hist80_v3_wf_fq" / "walkforward.json"),
        ("walk-forward balanced blend30", OUTPUTS_ROOT / "stockquant_hist80_v3_wf_balanced" / "walkforward.json"),
    ]
    rows, missing = [], []
    for name, path in candidates:
        if not path.exists():
            # A missing artifact must not render as a 0.0% experiment: that looks
            # like a real (flat) result.  Report it as missing instead.
            missing.append(name)
            continue
        data = _read_json(path, {})
        metrics = data.get("metrics") or data.get("oos_metrics") or {}
        rows.append(
            {
                "name": name,
                "cagr": _json_float(metrics.get("cagr")),
                "sharpe": _json_float(metrics.get("sharpe")),
                "max_drawdown": _json_float(metrics.get("max_drawdown")),
                "excess": _json_float(metrics.get("excess_total_return")),
                "ir": _json_float(metrics.get("information_ratio")),
            }
        )
    return jsonify({"rows": rows, "missing": missing})


@app.get("/api/daily-plan")
def api_daily_plan() -> Any:
    return jsonify(_html_safe(_read_json(PROJECT_ROOT / "data" / "state" / "daily_plan.json", {})))


@app.get("/api/news")
def api_news() -> Any:
    return jsonify(_html_safe(_read_json(PROJECT_ROOT / "data" / "state" / "news_snapshot.json", {})))


@app.get("/api/weekly-review")
def api_weekly_review() -> Any:
    return jsonify(
        _html_safe({
            "review": _read_json(PROJECT_ROOT / "data" / "state" / "weekly_review.json", {}),
            "knowledge": _read_json(PROJECT_ROOT / "data" / "state" / "knowledge_update.json", {}),
        })
    )


@app.get("/api/portfolio")
def api_portfolio() -> Any:
    ledger = PortfolioLedger(str(PROJECT_ROOT / "data" / "state" / "portfolio.json"))
    market = _get_market("real_v3")
    return jsonify(_html_safe(ledger.mark_to_market(market)))


@app.post("/api/portfolio/holding")
def api_portfolio_holding() -> Any:
    payload = request.get_json(force=True) or {}
    symbol = str(payload.get("symbol", "")).upper().strip()
    shares = int(payload.get("shares", 0) or 0)
    average_cost = float(payload.get("average_cost", 0.0) or 0.0)
    if not symbol:
        return jsonify({"ok": False, "error": "symbol is required"}), 400
    if shares < 0:
        return jsonify({"ok": False, "error": "shares must be >= 0"}), 400
    if shares > 0 and average_cost <= 0:
        return jsonify({"ok": False, "error": "average_cost must be > 0 when shares > 0"}), 400
    ledger = PortfolioLedger(str(PROJECT_ROOT / "data" / "state" / "portfolio.json"))
    result = ledger.set_holding(
        symbol=symbol,
        shares=shares,
        average_cost=average_cost,
        note=str(payload.get("note", "dashboard_actual_holding")),
    )
    return jsonify({"ok": True, "portfolio": _html_safe(result)})


@app.post("/api/portfolio/buy")
def api_portfolio_buy() -> Any:
    """One-click "I bought this" used by the recommendation tables."""
    payload = request.get_json(force=True) or {}
    symbol = str(payload.get("symbol", "")).upper().strip()
    shares = int(float(payload.get("shares", 0) or 0))
    price = float(payload.get("price", 0.0) or 0.0)
    if not symbol:
        return jsonify({"ok": False, "error": "symbol is required"}), 400
    if shares <= 0:
        return jsonify({"ok": False, "error": "shares must be > 0"}), 400
    # Fall back to the latest close / realtime quote when no price is given.
    if price <= 0:
        try:
            quote = fetch_realtime_quotes([symbol]).get(symbol) or {}
            price = float(quote.get("price") or 0.0)
        except Exception:
            price = 0.0
    if price <= 0:
        market = _get_market(str(_monitor_store.load_settings().get("dataset") or next(iter(DATASETS))))
        bar = market.bar(symbol, market.dates[-1]) if market.dates else None
        price = float((bar.raw_close if bar and bar.raw_close > 0 else (bar.close if bar else 0.0)) or 0.0)
    if price <= 0:
        return jsonify({"ok": False, "error": "cannot determine a price; pass price explicitly"}), 400
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    try:
        ledger.add_holding(symbol, shares, price)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    _alert_store.append_new([{
        "id": f"buy-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "severity": "opportunity", "kind": "marked_buy", "symbol": symbol,
        "signal_date": "", "title": f"已标记买入 {symbol}",
        "message": f"{shares} 股 @ {price:.2f}，已纳入盯盘。",
    }])
    threading.Thread(target=lambda: _safe_recompute_monitor(), daemon=True).start()
    return jsonify({"ok": True, "symbol": symbol, "shares": shares, "price": price})


def _safe_recompute_monitor() -> None:
    try:
        _compute_monitor()
    except Exception:
        pass


@app.post("/api/portfolio/sell")
def api_portfolio_sell() -> Any:
    payload = request.get_json(force=True) or {}
    symbol = str(payload.get("symbol", "")).upper().strip()
    shares = int(float(payload.get("shares", 0) or 0))
    price = float(payload.get("price", 0.0) or 0.0)
    if not symbol or shares <= 0:
        return jsonify({"ok": False, "error": "symbol and positive shares are required"}), 400
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    try:
        ledger.reduce_holding(symbol, shares, price)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    threading.Thread(target=lambda: _safe_recompute_monitor(), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/portfolio/trade")
def api_portfolio_trade() -> Any:
    payload = request.get_json(force=True) or {}
    symbol = str(payload.get("symbol", "")).upper().strip()
    side = str(payload.get("side", "")).lower().strip()
    shares = int(payload.get("shares", 0) or 0)
    price = float(payload.get("price", 0.0) or 0.0)
    if not symbol:
        return jsonify({"ok": False, "error": "symbol is required"}), 400
    if side not in {"buy", "sell"}:
        return jsonify({"ok": False, "error": "side must be buy or sell"}), 400
    if shares <= 0:
        return jsonify({"ok": False, "error": "shares must be > 0"}), 400
    if price <= 0:
        return jsonify({"ok": False, "error": "price must be > 0"}), 400
    ledger = PortfolioLedger(str(PROJECT_ROOT / "data" / "state" / "portfolio.json"))
    result = ledger.trade(symbol=symbol, side=side, shares=shares, price=price,
                          reason=str(payload.get("reason", "dashboard_paper_trade")))
    return jsonify({"ok": True, "portfolio": result})


@app.get("/api/crypto-monitor")
def api_crypto_monitor() -> Any:
    return jsonify(_html_safe(_read_json(PROJECT_ROOT / "data" / "state" / "crypto_monitor.json", {"rows": []})))


@app.get("/api/strategy-versions")
def api_strategy_versions() -> Any:
    rows = _read_json(PROJECT_ROOT / "data" / "state" / "strategy_registry.json", [])
    return jsonify(_html_safe(rows[-50:]))


@app.get("/api/paper-broker")
def api_paper_broker() -> Any:
    return jsonify(_html_safe(_read_json(PROJECT_ROOT / "data" / "state" / "paper_broker.json", {})))


@app.get("/api/execution-log")
def api_execution_log() -> Any:
    path = PROJECT_ROOT / "data" / "state" / "execution_log.jsonl"
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines()[-50:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return jsonify(_html_safe({"rows": rows}))


@app.get("/api/watchlist")
def api_watchlist() -> Any:
    return jsonify(_html_safe(WatchlistStore(str(PROJECT_ROOT / "data" / "state")).load()))


@app.post("/api/watchlist")
def api_watchlist_update() -> Any:
    payload = request.get_json(force=True) or {}
    store = WatchlistStore(str(PROJECT_ROOT / "data" / "state"))
    action = str(payload.get("action", "add"))
    symbol = str(payload.get("symbol", "")).upper()
    result = store.remove(symbol) if action == "remove" else store.add(symbol, str(payload.get("note", "")))
    return jsonify({"ok": True, "watchlist": _html_safe(result)})


@app.get("/api/entry-alerts")
def api_entry_alerts() -> Any:
    return jsonify(_html_safe(_read_json(PROJECT_ROOT / "data" / "state" / "entry_alerts.json", {"rows": []})))


def _compute_monitor(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Recompute the holdings monitor snapshot from the current ledger + market."""
    settings = settings or _monitor_store.load_settings()
    key = str(settings.get("dataset") or next(iter(DATASETS)))
    if key not in DATASETS:
        key = next(iter(DATASETS))
    market = _get_market(key)
    config = _config_from_payload({"dataset": key})
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    portfolio = ledger.mark_to_market(market)
    # Only the *real* (externally recorded) holdings feed the monitor. Falling
    # back to the paper portfolio silently would mislabel simulated positions
    # as the user's actual holdings.
    # 转债（11xxxx/12xxxx）走双低策略自己的判断链，不能拿股票因子模型去套：
    # 股票面板里根本没有转债行情，否则每只转债都会被判成"数据不足"。
    from stockquant.convertible.positions import is_cb_code
    holdings = [h for h in portfolio.get("external_rows", []) if not is_cb_code(h.get("symbol"))]
    snapshot_has_real = bool(holdings)
    quotes: Dict[str, Any] = {}
    overrides: Dict[str, float] = {}
    # When the market is closed, intraday quotes cannot move; skip the network
    # call and fall back to the last daily bar.  This is what makes a 24/7
    # monitor cheap and robust instead of hammering the quote source overnight.
    if holdings and _market_is_open():
        try:
            quotes = fetch_realtime_quotes([str(h.get("symbol")) for h in holdings])
        except Exception:
            quotes = {}
        overrides = {s: float(q.get("price") or 0.0) for s, q in quotes.items() if float(q.get("price") or 0.0) > 0}
    snapshot = HoldingsMonitor().analyze(
        market, holdings, config, settings=settings, price_overrides=overrides
    )
    if quotes:
        latest = max((str(q.get("date", "")) + " " + str(q.get("time", ""))).strip() for q in quotes.values())
        snapshot["quote_source"] = "sina_realtime"
        snapshot["quote_time"] = latest
        snapshot["quotes"] = {
            s: {"price": q.get("price"), "change_pct": q.get("change_pct"), "time": f"{q.get('date')} {q.get('time')}"}
            for s, q in quotes.items()
        }
    else:
        snapshot["quote_source"] = "daily_close"
    # ---- 转债持仓（双低策略口径）----
    cb_rows: List[Dict[str, Any]] = _cb_position_rows(portfolio.get("external_positions") or {})
    snapshot["cb_rows"] = cb_rows
    snapshot["has_cb_holdings"] = bool(cb_rows)
    snapshot["cb_warnings"] = sum(1 for r in cb_rows if r.get("severity") in ("critical", "warning"))
    snapshot["market_session"] = _market_status()["state"]
    snapshot["dataset"] = key
    snapshot["dataset_label"] = DATASETS[key]["label"]
    snapshot["enabled"] = bool(settings.get("enabled"))
    snapshot["interval_seconds"] = int(settings.get("interval_seconds", 300) or 300)
    snapshot["has_real_holdings"] = snapshot_has_real
    snapshot["portfolio_total"] = float(portfolio.get("total_value") or 0.0)
    snapshot["cash"] = float(portfolio.get("cash") or 0.0)
    _monitor_store.save_snapshot(snapshot)

    # Turn the analysis into alerts (deduplicated) and notify on new ones.
    entry_alerts = _read_json(STATE_DIR / "entry_alerts.json", {})
    from stockquant.convertible.positions import alerts_from_positions
    stock_alerts = build_alerts(snapshot, entry_alerts) if snapshot_has_real else []
    new_alerts = _alert_store.append_new(stock_alerts + alerts_from_positions(cb_rows))
    if new_alerts:
        _send_notifications(new_alerts, settings)
    snapshot["alert_count"] = len(new_alerts)
    snapshot["unread_alerts"] = _alert_store.unread_count()
    _monitor_store.save_snapshot(snapshot)
    return snapshot


def _log_notify(payload: Dict[str, Any]) -> None:
    try:
        path = STATE_DIR / "notify_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **payload}, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _send_notifications(alerts: List[Dict[str, Any]], settings: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """Fan out EVERY new alert to the configured channels (best effort)."""
    settings = settings or {}
    if not settings.get("notify", True):
        _log_notify({"skipped": "notify disabled", "count": len(alerts)})
        return {}
    results = notify_dispatch(alerts, load_notify_config())
    _log_notify({"count": len(alerts), "results": results,
                 "severities": sorted({str(a.get("severity")) for a in alerts})})
    return results


@app.get("/api/monitor")
def api_monitor() -> Any:
    return jsonify(_html_safe({
        "settings": _monitor_store.load_settings(),
        "snapshot": _monitor_store.load_snapshot(),
        "running": bool(_monitor_state.get("running")),
        "last_error": _monitor_state.get("last_error"),
    }))


@app.post("/api/monitor")
def api_monitor_update() -> Any:
    payload = request.get_json(force=True) or {}
    updates = {k: payload[k] for k in ("enabled", "interval_seconds", "dataset", "notify") if k in payload}
    settings = _monitor_store.save_settings(updates)
    _monitor_wake.set()  # re-evaluate now, don't wait for the old interval
    return jsonify({"ok": True, "settings": settings, "snapshot": _html_safe(_monitor_store.load_snapshot())})


@app.get("/api/monitor/alerts")
def api_monitor_alerts() -> Any:
    rows = list(reversed(_alert_store.load()))[:50]
    return jsonify(_html_safe({"rows": rows, "unread": _alert_store.unread_count()}))


@app.get("/api/cpcv")
def api_cpcv() -> Any:
    """Combinatorial purged CV: label-mode comparison."""
    path = OUTPUTS_ROOT / "cpcv_label_comparison.json"
    data = _read_json(path, {})
    if not data:
        return jsonify({"available": False})
    rows = []
    for mode, out in data.items():
        if not isinstance(out, dict) or not out.get("available"):
            continue
        rows.append({
            "mode": mode,
            "paths": int(_json_float(out.get("paths"))),
            "mean_ic": _json_float(out.get("mean_ic")),
            "median_ic": _json_float(out.get("median_ic")),
            "std_ic": _json_float(out.get("std_ic")),
            "min_ic": _json_float(out.get("min_ic")),
            "max_ic": _json_float(out.get("max_ic")),
            "positive_path_rate": _json_float(out.get("positive_path_rate")),
            "t_stat": _json_float(out.get("t_stat")),
        })
    return jsonify({"available": bool(rows), "rows": rows})


@app.get("/api/position-gap")
def api_position_gap() -> Any:
    """My holdings vs the model's target portfolio."""
    settings = _monitor_store.load_settings()
    key = str(settings.get("dataset") or next(iter(DATASETS)))
    if key not in DATASETS:
        key = next(iter(DATASETS))
    # Fast path: no backtest targets yet -> do not load the (large) market data.
    run = _last_result if _last_result.get("dataset") == key else {}
    if not run or not run.get("targets"):
        return jsonify({
            "available": False,
            "message": "还没有该数据集的回测目标。请先在「总览」页运行一次回测。",
        })
    market = _get_market(key)
    ledger = PortfolioLedger(str(STATE_DIR / "portfolio.json"))
    portfolio = ledger.mark_to_market(market)
    holdings = portfolio.get("external_rows", [])
    target_weights = {str(s): float(w) for s, w in (run.get("targets") or [])}
    prices = {str(h.get("symbol")): float(h.get("price") or 0.0) for h in holdings}
    date = market.dates[-1] if market.dates else None
    for symbol in target_weights:
        if prices.get(symbol):
            continue
        bar = market.bar(symbol, date) if date else None
        if bar is not None:
            prices[symbol] = float((bar.raw_close if bar.raw_close > 0 else bar.close) or 0.0)
    result = compute_position_gap(
        holdings, target_weights,
        cash=float(portfolio.get("cash") or 0.0),
        prices=prices,
    )
    result["dataset"] = key
    result["target_date"] = run.get("target_date")
    result["available"] = True
    return jsonify(_html_safe(result))


@app.post("/api/monitor/notify/test")
def api_monitor_notify_test() -> Any:
    settings = _monitor_store.load_settings()
    sample = [{
        "severity": "critical",
        "title": "StockQuant 推送测试",
        "message": "这是一条测试消息：如果你在微信/邮箱收到它，说明推送配置成功。",
    }]
    results = notify_dispatch(sample, load_notify_config())
    _log_notify({"test": True, "results": results})
    return jsonify({"ok": True, "channels": results, "configured": load_notify_config().channels()})


@app.post("/api/monitor/alerts/read")
def api_monitor_alerts_read() -> Any:
    changed = _alert_store.mark_all_read()
    return jsonify({"ok": True, "marked": changed})


@app.post("/api/monitor/refresh")
def api_monitor_refresh() -> Any:
    with _monitor_lock:
        _monitor_state["running"] = True
        try:
            snapshot = _compute_monitor()
            _monitor_state["last_error"] = None
        except Exception as exc:
            _monitor_state["last_error"] = f"{type(exc).__name__}: {exc}"
            return jsonify({"ok": False, "error": _monitor_state["last_error"]}), 500
        finally:
            _monitor_state["running"] = False
    return jsonify({"ok": True, "settings": _monitor_store.load_settings(), "snapshot": _html_safe(snapshot)})


def _start_monitor_thread() -> None:
    """Background loop: recompute the holdings monitor when enabled."""
    global _monitor_thread_started
    if _monitor_thread_started:
        return
    _monitor_thread_started = True

    def loop() -> None:
        while True:
            settings = _monitor_store.load_settings()
            try:
                interval = max(60, int(settings.get("interval_seconds", 300) or 300))
            except Exception:
                interval = 300
            # Outside trading hours there is nothing new to observe, so back off
            # to a longer idle (still 24/7, just not burning quote requests).
            idle = interval if _market_is_open() else max(interval, 1800)
            if settings.get("enabled") and not _monitor_state.get("running"):
                _monitor_state["running"] = True
                try:
                    _compute_monitor(settings)
                    _monitor_state["last_error"] = None
                except Exception as exc:
                    _monitor_state["last_error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    _monitor_state["running"] = False
            # Wait on an event so a settings change takes effect immediately
            # instead of after the old interval has elapsed.
            _monitor_wake.wait(idle)
            _monitor_wake.clear()

    threading.Thread(target=loop, name="holdings-monitor", daemon=True).start()




GUIDE_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>使用说明</title><style>
:root{--bg:#0a1220;--card:#131f31;--line:#22344d;--text:#eaf2fb;--muted:#8ea6bf;--blue:#38bdf8;--green:#34d399;--amber:#fbbf24;--red:#fb7185}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;padding:18px 16px 60px;line-height:1.75;font-size:15px}
main{max-width:820px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:26px 0 10px;color:var(--blue);border-left:3px solid var(--blue);padding-left:9px}
h3{font-size:15px;margin:16px 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 6px;border-bottom:1px solid #1c2b40;text-align:left;vertical-align:top}
th{color:var(--muted);font-size:12px}
.muted{color:var(--muted);font-size:13px}
.g{color:var(--green)}.w{color:var(--amber)}.b{color:var(--red)}
b.hl{color:var(--blue)}
ol,ul{padding-left:22px;margin:8px 0}
li{margin:5px 0}
.big{font-size:16px;background:#0d2136;border:1px solid #1c4a6b;border-radius:10px;padding:12px;margin:12px 0}
a{color:var(--blue)}
</style></head><body><main>
<h1>📖 使用说明</h1>
<div class="muted">看不懂哪个功能，就来这查。建议只看第一部分。</div>

<div class="big" style="border-color:#7f1d1d;background:#2a1116">
<b class="hl">⚠️ 先读这句</b>：本页是<b>作者个人自用流程的记录</b>，是这个研究工具的使用示例，
<b>不是给读者的投资建议，也不构成荐股</b>。系统输出的一切清单都只是研究结果，
是否采用、如何决策由你自己负责，风险自担。</div>
<div class="big"><b class="hl">一句话：这玩意儿每周告诉你买哪 15 只可转债，并提醒你风险。</b><br>
它不能自动帮你买卖，也不是必赚（预期一年 4~8%，可能亏 15~20%）。</div>

<h2>一、你实际要做的，就 3 件事</h2>
<table><thead><tr><th>频率</th><th>做什么</th><th>花多久</th></tr></thead><tbody>
<tr><td>有新债时</td><td>查看信息（是否参与自行决定）</td><td>1 分钟</td></tr>
<tr><td><b>每周一次</b></td><td>照「本周买入清单」<b>买卖</b></td><td>10 分钟</td></tr>
<tr><td>顺手</td><td>看有没有「⚠️ 强赎提醒」</td><td>几秒</td></tr>
</tbody></table>
<div class="muted">其余所有标签（盯盘/股票回测/研究/组合/助手）你现在都可以不用管。</div>

<h2>二、界面每个标签是干啥的</h2>
<table><thead><tr><th>标签</th><th>干啥的</th><th>你要不要用</th></tr></thead><tbody>
<tr><td><b>✅ 本周操作</b></td><td>清单 + 打新 + 强赎 + 持仓 + 对账</td><td class="g"><b>只用这个</b></td></tr>
<tr><td>盯盘</td><td><b>24 小时自动盯你的持仓</b>（股票 + 转债），触发风险就提醒你；开关在页面右上</td><td>建议<b>开</b>（见第八节）</td></tr>
<tr><td>股票回测</td><td>老股票模型，<b>已停用</b>（赚不到钱）</td><td class="b">别用</td></tr>
<tr><td>研究</td><td>学术诊断（IC/过拟合检验/归因）</td><td>不用</td></tr>
<tr><td>组合</td><td>股票交易用</td><td>不用</td></tr>
<tr><td>助手</td><td>资讯/复盘</td><td>可看可不看</td></tr>
</tbody></table>

<h3>「✅ 本周操作」里，从上到下每块是啥</h3>
<table><thead><tr><th>区块</th><th>作用</th><th>怎么用</th></tr></thead><tbody>
<tr><td>怎么用（3步）</td><td>使用说明</td><td>忘了点开看</td></tr>
<tr><td><b>风控纪律</b></td><td>告诉你该不该减仓</td><td>见下方第五节</td></tr>
<tr><td>状态行</td><td>数据是哪天的、剔除了哪些坏债</td><td>看「数据日」是不是最新</td></tr>
<tr><td><b>① 本周买入清单</b></td><td><b>核心</b>：按「值得买分」从高到低 15 只</td><td><b>按排序先后</b>（仅展示顺序）</td></tr>
<tr><td><b>② 我的转债持仓</b></td><td>记你买了什么 + <b>自动给你每只债一条建议</b>（持有/减仓/卖出）</td><td>买完填「代码/张数/成本」→ 记买入</td></tr>
<tr><td>📋 Paper 对账</td><td>记你<i>实际</i>成交价</td><td>填实际价格 + 勾「成交」→ 保存</td></tr>
<tr><td>⚠️ 强赎提醒</td><td>哪些债要被强制赎回</td><td>你持有它在里面 → <b class="b">最后交易日前卖掉</b></td></tr>
<tr><td>③ 打新日历</td><td>最近能打的新债</td><td>申购日当天按上限申购</td></tr>
<tr><td>分年收益 / 净值曲线</td><td>这策略每年/累计赚多少</td><td>看看心里有数</td></tr>
</tbody></table>

<h2>三、黑话翻译</h2>
<table><thead><tr><th>词</th><th>白话</th></tr></thead><tbody>
<tr><td>可转债</td><td>债券+股票的混合券：跌到 100 附近有债底保护，正股涨它能跟涨</td></tr>
<tr><td>双低</td><td>= 价格 + 转股溢价率。<b>越低越好</b>（便宜 + 跟涨强）</td></tr>
<tr><td><b>值得买分</b></td><td>综合分（0~100，<b class="g">越高越好</b>）：双低 + 评级 + 流动性算出来的</td></tr>
<tr><td>转股溢价率</td><td>转债比它能换的股票贵多少。<b>越低越划算</b></td></tr>
<tr><td>强赎</td><td>公司强制按 ~100 元赎回。你 130 买的不卖就亏 30</td></tr>
<tr><td>打新债</td><td>新债上市前免费申购，中签才交钱，上市首日通常涨 → <b>白捡</b></td></tr>
<tr><td>回撤</td><td>从最高点跌下来多少（−15% = 跌了 15%）</td></tr>
<tr><td>CAGR（年化）</td><td>平均每年赚多少</td></tr>
<tr><td>Sharpe</td><td>赚钱的性价比（越高越好，1 左右算不错）</td></tr>
</tbody></table>

<h2>四、手把手操作</h2>
<ol>
<li><b>开户（一次性）</b>：券商 App 开普通账户，转几万闲钱进去。</li>
<li><b>新债信息</b>：看「打新日历」→ 查看申购信息（该页只做信息汇总，不构成建议）。<br>
<span class="muted">值多少钱（2024-2026 实测）：146 只新债上市首日均涨 <b>+39.7%</b>、破发率 <b>0.7%</b>，每签（1000 元）期望约 <b>397 元</b>；
但中签率只有 ~0.1~0.2%，<b>每次按上限申购的期望只有 5~8 元</b>。零成本零风险，但别指望靠它赚钱。</span></li>
<li><b>候选清单的读法</b>：它是排序结果，不是推荐。作者自用口径为等权（1 手 = 10 张 ≈ 1000 元面值）。</li>
<li><b>记对账</b>：在「Paper 对账」填实际成交价 → 保存。（看系统报价和你实买差多少）</li>
<li><b>每周看一次</b>：作者自用的规则是——不在候选内的调出、新进的记入；强赎名单里的优先处理。</li>
</ol>

<h2>五、风控纪律（怎么算的 + 怎么用）</h2>
<div class="card">
<div class="warn-text"><b>它按什么算？</b>——<b class="hl">你自己记录的持仓</b>（每天记一次净值，算"从最高点跌了多少"）。<br>
如果你还没记持仓，它会临时按"策略回测的历史回撤"给你参考，并明确标出「依据：策略回测（我还没记录持仓）」。</div>
<table><thead><tr><th>触发</th><th>你要做什么</th></tr></thead><tbody>
<tr><td>回撤 &gt; <b>15%</b></td><td>仓位 <b class="w">减半</b></td></tr>
<tr><td>回撤 &gt; <b>20%</b></td><td><b class="b">清仓 + 复盘</b>，想清楚再重启</td></tr>
</tbody></table>
<div class="muted">仓位上限公式：<b>可投金额 = 你"亏 20% 不影响生活"的金额 ÷ 0.20</b>。例：能接受亏 1 万 → 最多投 5 万。</div>
<div class="muted" style="margin-top:8px"><b>为什么没有"亏 5% 就停"这种规矩？</b>我们拿 2019-2026 历史实测过：
「滚动 12 个月收益 &lt; −5% 就停手」这条规则触发过 4 次，其中 <b>2 次停手全是误报</b>（停完三个月涨了 21.9%），
一年还白少赚 0.4%，回撤一点没改善。<b>能验证的规则才写进来</b>，拍脑袋的刹车不装。</div>
</div>

<h2>六、什么情况要警惕</h2>
<ul>
<li>顶部横幅变「<b class="w">减仓</b>」→ 卖一半；变「<b class="b">清仓</b>」→ 全卖、复盘。</li>
<li>你买的债出现在「强赎提醒」→ 最后交易日前<b>必须卖</b>。</li>
<li><b>近两年为什么不赚钱？</b>我们已经查清（见「研究」页的策略体检）：2026 年转债市场其实涨了 11.8%，
是我们为了躲信用风险设的过滤（价格≥95、纯债溢价率≥0）把涨得最好的那批债挡在门外了——
<b>这是保险的保费，不是系统故障</b>。知道这一点，你就不会在亏的时候乱改规则。</li>
<li>净值从高点跌 15% → 减半仓（别慌着全清）。</li>
<li>想加钱前先问自己："这钱亏 20% 我会不会难受？"</li>
</ul>

<h2>七、千万别做的 5 件事</h2>
<ol>
<li>别按 17% 的收益率期待 → 现实是 <b>4~8%</b>。</li>
<li>别借钱 / 用急用钱投 → 只用闲钱。</li>
<li>别看到跌就割 → 回撤 15% 是正常的。</li>
<li>别用「股票回测」那个模型 → 它赚不到钱，已停用。</li>
<li>别一次投太多 → 先小资金跑 6~8 周对账，达标再加大。</li>
</ol>

<h2>八、自动盯盘（开着就行，出事会提醒你）</h2>
<div class="card">
<b class="hl">它盯什么？</b>只盯<b>你记过买入的持仓</b>（在「我的转债持仓」里录的）。没记的它不知道你买了。<br><br>
<b class="hl">怎么开？</b>「盯盘」标签右上角的开关打到开；旁边可以选多久看一次（平时 5 分钟就够）。<b>开着就 24 小时运行</b>：交易时段频繁看，收盘后自动降低频率，不折腾你。想收推送就打勾「推送提醒」。<br><br>
<b class="hl">什么情况会提醒你？</b>
<table><thead><tr><th>情况</th><th>提醒</th><th>你做什么</th></tr></thead><tbody>
<tr><td>债公告强赎</td><td class="b">红灯</td><td><b>最后交易日前卖出/转股</b></td></tr>
<tr><td>正股被 ST / 正股股价 &lt; 3 元</td><td class="b">红灯</td><td>卖出（信用风险）</td></tr>
<tr><td>转债价跌破 95（跌进债底区）</td><td class="b">红灯</td><td>卖出，别赌它翻身</td></tr>
<tr><td>近 20 日急跌超 15%</td><td class="w">黄灯</td><td>减仓/卖出（疑似暴雷前兆）</td></tr>
<tr><td>双低排名跌出前 15</td><td class="w">黄灯</td><td>下期轮动时换掉</td></tr>
<tr><td>单只浮亏超 15%</td><td class="w">黄灯</td><td>按纪律减仓</td></tr>
</tbody></table>
<div class="muted">提醒只在<b>新出现</b>时推一次，不会同一个问题反复轰炸你。红灯的当天就处理，别拖。</div>
</div>

<div class="big">✅ <b>每周打开「✅ 本周操作」→ 看有没有新债 → 按事先写死的规则处理 → 记一下对账 → 完事。</b><br>
<span class="muted">其他页面都是研究/股票用的，你现在可以完全不管。</span></div>
<div class="muted" style="margin-top:16px">本系统为研究/工具，<b>不构成投资建议</b>。历史表现不代表未来收益，请只用能承受亏损的闲钱。</div>
</main></body></html>
'''

FAMILY_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>每周转债清单</title><style>
:root{--bg:#0b1220;--card:#131f31;--line:#22344d;--text:#eaf2fb;--muted:#8ea6bf;--green:#34d399;--red:#fb7185;--amber:#fbbf24;--blue:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;padding:16px 14px 60px;line-height:1.6}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:20px 0 8px;color:var(--blue)}
.muted{color:var(--muted);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:7px 6px;border-bottom:1px solid #1c2b40;text-align:left}
th{color:var(--muted);font-size:12px;font-weight:600}
.rank{display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;background:#0e2a44;color:#7dd3fc;border-radius:999px;font-size:12px}
.warn{color:var(--amber)}.bad{color:var(--red)}
button{width:100%;padding:12px;border:0;border-radius:10px;background:linear-gradient(135deg,#0ea5e9,#2563eb);color:#fff;font-weight:700;font-size:15px;margin-top:10px}
.steps{background:#0d1a29;border:1px solid var(--line);border-radius:12px;padding:12px;font-size:14px}
.steps b{color:var(--blue)}
.foot{color:var(--muted);font-size:11px;margin-top:18px;border-top:1px solid var(--line);padding-top:10px}
</style></head><body>
<h1>📈 每周转债清单</h1>
<div class="muted" id="status">加载中…</div>
<h2>① 本周买入（按"值得买"从高到低）</h2>
<div class="card">
  <div id="picks" class="muted">加载中…</div>
  <button onclick="copyList()">📋 复制清单（发给自己/微信）</button>
</div>
<h2>② 打新债（白捡，看到就申购）</h2>
<div class="card"><div id="issues" class="muted">加载中…</div></div>
<h2>③ ⚠️ 需要你动手的</h2>
<div class="card"><div id="warnings" class="muted">加载中…</div></div>
<h2>怎么用</h2>
<div class="steps">
  <b>买</b>：清单里的每只，<b>买差不多一样的钱</b>（比如每只 1000~2000 元）。<br>
  <b>打新</b>：打新那天在券商 App <b>按上限申购</b>，中签才交钱，上市首日卖掉。<br>
  <b>每周看一次</b>：不在候选内的调出、新进的记入（作者自用规则）。<br>
  <b>强赎</b>：第③栏里出现的债，<b>必须在最后交易日之前卖掉</b>，不然会被 100 元左右赎回、亏掉溢价。
</div>
<div class="foot">这是研究/工具，<b>不是投资建议</b>。历史表现不代表未来；有回撤风险，请只用能承受亏损的闲钱。数据来自公开免费源。</div>
<script>
const $=id=>document.getElementById(id);
async function get(u){const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json()}
let lastPicks=[];
async function load(){
  try{
    const j=await get('/api/family');
    $('status').textContent='数据日 '+j.asof+' · 每周一/周四自动更新';
    const pk=j.picks||[];lastPicks=pk;
    $('picks').innerHTML=pk.length?'<table><thead><tr><th>#</th><th>名称</th><th>价格</th><th>评级</th></tr></thead><tbody>'
      +pk.map((x,i)=>`<tr><td><span class="rank">${i+1}</span></td><td>${x.bond_name}<div class="muted">${x.bond_code}</div></td><td>${Number(x.price).toFixed(2)}</td><td>${x.rating||''}</td></tr>`).join('')
      +'</tbody></table>':'<div class="muted">暂无清单</div>';
    const iss=j.issues||[];
    $('issues').innerHTML=iss.length?'<table><tbody>'+iss.slice(0,6).map(x=>`<tr><td>${x.apply_date}</td><td>${x.name}</td><td class="muted">${x.apply_code}</td></tr>`).join('')+'</tbody></table><div class="muted">申购日当天在券商 App 按上限申购</div>':'<div class="muted">近期无新债</div>';
    const wn=j.warnings||[];
    $('warnings').innerHTML=wn.length?'<table><tbody>'+wn.map(w=>`<tr><td class="bad">${w.name}</td><td>最后交易日 ${w.last_day||'—'}</td></tr>`).join('')+'</tbody></table><div class="warn">以上持有的债要卖出/转股，否则按 ~100 被赎回</div>':'<div class="muted">你持有的债里，暂时没有要强赎的 👍</div>';
  }catch(e){$('status').textContent='加载失败：'+e.message}
}
function copyList(){
  const t='【本周转债清单】\n'+lastPicks.map((x,i)=>(i+1)+'. '+x.bond_name+' ('+x.bond_code+') '+Number(x.price).toFixed(2)).join('\n')+'\n\n等权记录；不在候选内的调出。';
  navigator.clipboard.writeText(t).then(()=>alert('已复制，去微信粘贴吧')).catch(()=>prompt('手动复制：',t));
}
load();setInterval(load,600000);
</script></body></html>
'''

MOBILE_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>StockQuant 盯盘</title>
<style>
:root{--bg:#0a1220;--card:#111c2c;--line:#24354c;--text:#e8f1fa;--muted:#8fa6bd;--red:#fb7185;--green:#34d399;--amber:#fbbf24;--blue:#38bdf8}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font:-apple-system-body;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;padding:14px 14px 90px}
h1{font-size:20px;margin:0 0 2px}h2{font-size:16px;margin:18px 0 10px}
.muted{color:var(--muted);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.tag{display:inline-block;padding:3px 9px;border-radius:999px;font-size:11px;background:#102c43;color:#7dd3fc}
.sell{background:#3b1520;color:#fda4af}.buy{background:#0d2c26;color:#6ee7b7}.warn{background:#382b0d;color:#fcd34d}
.alerts .a{border-left:3px solid var(--line);padding:9px;margin-bottom:8px;background:#0c1624;border-radius:8px}
.a.critical{border-left-color:var(--red)}.a.warning{border-left-color:var(--amber)}.a.opportunity{border-left-color:var(--green)}
.bar{position:fixed;left:0;right:0;bottom:0;background:#0d1726;border-top:1px solid var(--line);padding:10px 12px;display:flex;gap:8px}
.bar input,.bar button{font-size:15px;padding:11px;border-radius:10px;border:1px solid var(--line);background:#0a1725;color:var(--text);outline:none}
.bar input{flex:1}.bar button{background:#0ea5e9;border:0;color:#fff;font-weight:700;white-space:nowrap}
.badge{background:var(--red);color:#fff;border-radius:999px;padding:1px 7px;font-size:11px;margin-left:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
</style></head><body>
<h1>StockQuant 盯盘</h1>
<div class="muted" id="status">加载中…</div>
<div class="row" style="margin-top:12px"><button class="bar-btn" style="background:#0ea5e9;border:0;color:#fff;padding:9px 14px;border-radius:10px;font-weight:700" onclick="refresh()">刷新</button><div class="muted">下拉也可刷新</div></div>
<h2>我的转债持仓 <span class="badge" id="cbbadge" style="display:none">0</span></h2>
<div id="cbholdings" class="muted">加载中…</div>
<h2>我的股票持仓 <span class="badge" id="hbadge" style="display:none">0</span></h2>
<div id="holdings" class="muted">加载中…</div>
<h2>提醒 <span class="badge" id="abadge" style="display:none">0</span></h2>
<div id="alerts" class="alerts"></div>
<div class="bar">
  <input id="sym" placeholder="代码，如 600309.SH">
  <input id="sh" type="number" inputmode="numeric" placeholder="股数" value="100" style="max-width:80px">
  <button onclick="buy()">买</button>
  <button onclick="sell()" style="background:#e11d48">卖</button>
</div>
<script>
const $=id=>document.getElementById(id);
const pct=v=>(100*v).toFixed(1)+'%';
async function get(u,o){const r=await fetch(u,o);const j=await r.json();if(!r.ok||j.ok===false)throw new Error(j.error||r.statusText);return j}
async function refresh(){
  try{
    const [m,a,cb]=await Promise.all([get('/api/monitor'),get('/api/monitor/alerts'),get('/api/cb/holdings')]);
    (function(){
      const rs=cb.rows||[];const b=$('cbbadge');const w=Number(cb.warnings||0);
      b.style.display=w>0?'inline-block':'none';b.textContent=w;
      $('cbholdings').innerHTML=rs.length?rs.map(r=>`
        <div class="card">
          <div class="row"><b>${r.name} ${r.code}</b><span class="tag ${r.severity==='critical'?'sell':(r.severity==='warning'?'warn':'buy')}">${r.action}</span></div>
          <div class="row"><span class="muted">成本 ${Number(r.cost).toFixed(2)} · 现价 ${Number(r.price).toFixed(2)}</span><span class="${r.pnl_pct>=0?'buy':'sell'}">${pct(r.pnl_pct)}</span></div>
          <div class="row"><span class="muted">双低 ${r.double_low==null?'—':Number(r.double_low).toFixed(1)}（排名 ${r.rank==null?'—':r.rank+'/'+r.rank_universe}）</span><span class="muted">${r.rating||''}</span></div>
          <div class="muted">${(r.reasons||[]).join('；')}</div>
        </div>`).join(''):'<div class="card muted">还没有记录转债持仓。在电脑端「✅ 本周操作」页用「记买入」录入。</div>';
    })();
    const s=m.snapshot||{};const rows=s.rows||[];
    $('status').textContent=rows.length?('信号日 '+s.signal_date+' · 实时价 '+(s.quote_time||'')+' · 更新 '+String(s.generated_at_utc||'').replace('T',' ').slice(5,16)):'尚无实际持仓';
    const hb=$('hbadge');const na=s.summary&&s.summary['止损/减仓']?Number(s.summary['止损/减仓']):0;
    hb.style.display=na>0?'inline-block':'none';hb.textContent=na;
    $('holdings').innerHTML=rows.length?rows.map(r=>`
      <div class="card">
        <div class="row"><b>${r.symbol}</b><span class="tag ${r.action.includes('止损')||r.action.includes('减仓')?'sell':r.action.includes('加仓')||r.action.includes('止盈')?'buy':'warn'}">${r.action}</span></div>
        <div class="row"><span class="muted">成本 ${Number(r.average_cost).toFixed(2)} · 现价 ${Number(r.price).toFixed(2)}</span><span class="${r.pnl_pct>=0?'buy':'sell'}">${pct(r.pnl_pct)}</span></div>
        <div class="row"><span class="muted">止损 ${Number(r.stop_loss).toFixed(2)} · 止盈 ${Number(r.take_profit).toFixed(2)}</span><span class="muted">模型分 ${r.score==null?'—':Number(r.score).toFixed(2)}</span></div>
        <div class="muted">${r.reason}</div>
      </div>`).join(''):'<div class="card muted">尚无实际持仓。下方输入代码即可标记买入。</div>';
    const al=a.rows||[];const ab=$('abadge');ab.style.display=a.unread>0?'inline-block':'none';ab.textContent=a.unread;
    $('alerts').innerHTML=al.length?al.slice(0,15).map(x=>`<div class="a ${x.severity}"><b>${x.title}</b><div class="muted">${x.message}</div></div>`).join(''):'<div class="muted">暂无提醒</div>';
  }catch(e){$('status').textContent='加载失败：'+e.message}
}
async function buy(){const sym=$('sym').value.trim(),sh=+$('sh').value;if(!sym)return alert('请输入代码');const px=prompt('成交价（留空=实时价）','');if(px===null)return;
  try{await get('/api/portfolio/buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,shares:sh,price:px?+px:0})});$('sym').value='';refresh()}catch(e){alert('失败：'+e.message)}}
async function sell(){const sym=$('sym').value.trim(),sh=+$('sh').value;if(!sym)return alert('请输入代码');const px=prompt('成交价（留空即可）','');if(px===null)return;
  try{await get('/api/portfolio/sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,shares:sh,price:px?+px:0})});$('sym').value='';refresh()}catch(e){alert('失败：'+e.message)}}
refresh();setInterval(refresh,60000);
</script></body></html>'''

HTML_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockQuant Control Room</title>
<style>
:root{--bg:#070d17;--panel:#0d1826;--panel2:#132237;--line:#20344d;--text:#e9f2fb;--muted:#8ba2bb;--blue:#38bdf8;--green:#34d399;--red:#fb7185;--amber:#fbbf24}
*{box-sizing:border-box}html,body{margin:0;padding:0}
body{background:radial-gradient(1200px 500px at 15% -5%,#102943 0,var(--bg) 45%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;font-size:14px}
a{color:var(--blue)}
main{max-width:1440px;margin:0 auto;padding:22px 26px 48px}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
.brand h1{font-size:24px;margin:0;letter-spacing:.2px}
.brand p{margin:4px 0 0;color:var(--muted);font-size:12px}
.spacer{flex:1}
.chip{font-size:12px;border-radius:999px;padding:6px 13px;border:1px solid var(--line);background:#0c1826;color:var(--muted);white-space:nowrap}
.chip.ok{color:var(--green);border-color:#155e45;background:#0b281f}
.chip.warn{color:var(--amber);border-color:#6b5713;background:#271f0c}
.linkbtn{font-size:12px;color:#cfe6ff;border:1px solid #284661;background:#0e1a2b;border-radius:9px;padding:7px 13px;text-decoration:none;cursor:pointer}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:20px;overflow-x:auto}
.tab{appearance:none;border:0;background:transparent;color:var(--muted);font-size:14px;font-weight:600;padding:12px 18px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:var(--text)}
.tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.page{display:none}.page.active{display:block;animation:fade .15s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
.panel{background:linear-gradient(180deg,rgba(19,34,55,.92),rgba(13,24,38,.96));border:1px solid var(--line);border-radius:16px;padding:18px 20px}
.panel h2{margin:0 0 14px;font-size:15px;font-weight:700}
.panel h3{margin:16px 0 8px;font-size:13px;color:var(--muted);font-weight:600}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.panel-head h2{margin:0}
.stack{display:grid;gap:16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:980px){.grid2{grid-template-columns:1fr}}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
select,input[type=text],input[type=number],input[type=date]{background:#081320;border:1px solid #284661;color:var(--text);border-radius:9px;padding:9px 11px;font-size:13px;outline:none;min-width:0}
select:focus,input:focus{border-color:var(--blue)}
label{display:grid;gap:5px;color:var(--muted);font-size:12px}
.form-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:980px){.form-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.form-grid{grid-template-columns:1fr}}
.btn{appearance:none;border:0;border-radius:10px;padding:10px 16px;font-size:13px;font-weight:700;cursor:pointer;color:#fff;background:linear-gradient(135deg,#0ea5e9,#2563eb)}
.btn.ghost{background:#0e1a2b;border:1px solid #284661;color:#cfe6ff;font-weight:600}
.btn.danger{background:#9f1239}
.btn:disabled{opacity:.5;cursor:wait}
.mini-btn{appearance:none;border:1px solid #284661;background:#0e1a2b;color:#cfe6ff;border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}
@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}}
@media(max-width:600px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{background:#0a1725;border:1px solid var(--line);border-radius:12px;padding:13px 15px}
.card .k{font-size:11px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase}
.card .v{font-size:21px;font-weight:800;margin-top:6px}
.positive{color:var(--green)}.negative{color:var(--red)}.neutral{color:var(--blue)}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}
.tablewrap{overflow-x:auto;margin-top:8px}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 8px;border-bottom:1px solid #1b334b;font-size:12px;text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}
tr:hover td{background:#10243a}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#102c43;color:#7dd3fc;font-size:11px;white-space:nowrap}
.tag.warning{color:#fcd34d;background:#382b0d}
.empty{color:var(--muted);font-size:13px;padding:16px 0}
.error{color:var(--red);white-space:pre-wrap;font-size:12px;margin-top:10px}
.muted{color:var(--muted)}.muted-sm{color:var(--muted);font-size:12px}
.snapshot{margin-top:12px;color:var(--muted);font-size:12px;background:#081522;border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.chart-wrap{margin-top:14px;background:#081522;border:1px solid var(--line);border-radius:12px;padding:12px}
canvas{width:100%;height:320px;display:block}
.legend{display:flex;gap:18px;color:var(--muted);font-size:12px;margin-top:8px}
.dot{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}
.dot.blue{background:var(--blue)}.dot.amber{background:var(--amber)}
.switch{position:relative;display:inline-block;width:46px;height:26px;flex:0 0 auto}
.switch input{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;cursor:pointer;z-index:2}
.slider{position:absolute;cursor:pointer;inset:0;background:#284661;border-radius:999px;transition:.2s}
.slider:before{content:"";position:absolute;height:20px;width:20px;left:3px;top:3px;background:#e8f1fa;border-radius:50%;transition:.2s}
.switch input:checked + .slider{background:#0ea5e9}
.switch input:checked + .slider:before{transform:translateX(20px)}
.switch-wrap{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.inline-check{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap}
.inline-check input{width:auto;margin:0;padding:0}
.badge{display:inline-block;margin-left:6px;background:var(--red);color:#08111d;border-radius:999px;padding:1px 7px;font-size:11px;font-weight:800;vertical-align:middle}
details.acc{background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-top:14px;overflow:hidden}
details.acc>summary{cursor:pointer;padding:15px 18px;font-size:14px;font-weight:700;list-style:none;display:flex;justify-content:space-between;align-items:center;color:var(--text)}
details.acc>summary::-webkit-details-marker{display:none}
details.acc>summary::after{content:"+";color:var(--muted);font-size:18px;transition:.2s}
details.acc[open]>summary::after{transform:rotate(45deg)}
.acc-body{padding:0 18px 18px}
.alert-list{max-height:360px;overflow-y:auto}
.alert-item{border-left:3px solid var(--line);padding:9px 12px;margin-bottom:8px;background:#0a1725;border-radius:8px}
.alert-item.critical{border-left-color:var(--red)}
.alert-item.warning{border-left-color:var(--amber)}
.alert-item.opportunity{border-left-color:var(--green)}
.alert-item b{font-size:13px}
.alert-item .msg{color:var(--muted);font-size:12px;margin-top:3px;white-space:normal}
.footer{color:var(--muted);font-size:11px;margin-top:24px;border-top:1px solid var(--line);padding-top:14px}
.modal-bg{position:fixed;inset:0;background:rgba(4,8,14,.7);display:none;align-items:center;justify-content:center;z-index:50}
.modal-bg.show{display:flex}
.modal{background:#0d1826;border:1px solid var(--line);border-radius:16px;width:min(560px,92vw);max-height:86vh;overflow:auto;padding:20px}
.modal h3{margin:0 0 12px;font-size:16px}
.modal .close{float:right;background:transparent;border:0;color:var(--muted);font-size:20px;cursor:pointer}
.fbar{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #16283c;font-size:13px}
.fbar .pos{color:var(--green)}.fbar .neg{color:var(--red)}
.symlink{color:var(--blue);cursor:pointer;text-decoration:underline dotted}
</style>
</head>
<body>
<main>
<div class="top">
  <div class="brand"><h1>StockQuant</h1><p>股票智能量化研究台 · 多因子指数增强 · 严格样本外 · 研究用途</p></div>
  <div class="spacer"></div>
  <span class="chip ok" id="status">就绪</span>
  <span class="chip" id="marketStatus">A股 —</span>
  <a href="/guide" target="_blank" class="linkbtn" style="background:#0e2a44;border-color:#1c4a6b">📖 使用说明</a>
  <a href="/api/backtest/export" class="linkbtn" id="exportBtn" style="display:none">导出 CSV</a>
</div>

<nav class="tabs" id="tabs">
  <button class="tab active" data-tab="etf">ETF 执行</button>
  <button class="tab" data-tab="convertible">✅ 本周操作</button>
  <button class="tab" data-tab="monitor">盯盘<span class="badge" id="monitorBadge" style="display:none">0</span></button>
  <button class="tab" data-tab="overview">股票回测</button>
  <button class="tab" data-tab="quantask">量化问答</button>
  <button class="tab" data-tab="research">研究</button>
  <button class="tab" data-tab="portfolio">组合</button>
  <button class="tab" data-tab="assistant">助手</button>
</nav>

<!-- ============ 总览 ============ -->
<section class="page" id="page-overview">
  <div class="panel">
    <div class="panel-head">
      <h2>策略回测</h2>
      <div class="toolbar">
        <select id="dataset" style="max-width:320px"></select>
        <select id="preset"></select>
        <button class="btn primary" id="run">运行回测</button>
      </div>
    </div>
    <details class="acc">
      <summary>高级设置（数据区间 / 成本 / 风控）</summary>
      <div class="acc-body">
        <div class="form-grid">
          <label>开始日期<input id="start" type="date"></label>
          <label>结束日期<input id="end" type="date"></label>
          <label>调仓频率<select id="rebalance"><option value="quarterly">季度</option><option value="semiannual">半年</option><option value="annual">年度</option><option value="monthly">月度</option></select></label>
          <label>主动权重<input id="alpha_weight" type="number" min="0" max="1" step="0.05" value="0.3"></label>
          <label>目标波动<input id="target_volatility" type="number" min="0.05" max="0.5" step="0.01" value="0.18"></label>
          <label>最大持仓数<input id="max_positions" type="number" value="12"></label>
          <label>单票上限<input id="max_weight" type="number" step="0.01" value="0.10"></label>
          <label>行业上限<input id="sector_max_weight" type="number" step="0.01" value="0.30"></label>
          <label>现金缓冲<input id="cash_buffer" type="number" step="0.01" value="0.03"></label>
          <label>佣金 bps<input id="commission_bps" type="number" value="3"></label>
          <label>印花税 bps<input id="stamp_duty_bps" type="number" value="5"></label>
          <label>滑点 bps<input id="slippage_bps" type="number" value="5"></label>
          <label>交易带宽<input id="rebalance_band" type="number" step="0.01" value="0.02"></label>
          <label>趋势过滤<select id="market_filter"><option value="none">关闭</option><option value="ma">200日均线</option></select></label>
          <label>风险降仓<input id="risk_off_exposure" type="number" step="0.1" value="0.5"></label>
          <label>组合构建<select id="weight_scheme"><option value="score">分数加权</option><option value="equal">等权</option><option value="optimize">协方差优化</option><option value="kelly">Kelly 分数</option></select></label>
          <label>CVaR 尾部(如0.05)<input id="cvar_alpha" type="number" step="0.01" placeholder="关闭"></label>
          <label>CVaR 权重<input id="cvar_lambda" type="number" step="1" value="0"></label>
          <label>波动估计<select id="vol_method"><option value="realized">已实现波动</option><option value="garch">GARCH(1,1)</option></select></label>
          <label>单期换手上限<input id="max_turnover" type="number" step="0.1" value="1.0" min="0.05" max="1"></label>
          <label>回撤熔断(如0.15)<input id="portfolio_drawdown_limit" type="number" step="0.01" placeholder="关闭"></label>
          <label>熔断后仓位<input id="portfolio_drawdown_exposure" type="number" step="0.1" value="0.5"></label>
        </div>
      </div>
    </details>
    <div class="error" id="error"></div>
  </div>

  <div class="panel" style="margin-top:16px">
    <h2 id="resultTitle">等待运行</h2>
    <div class="cards">
      <div class="card"><div class="k">CAGR</div><div class="v" id="mCagr">—</div></div>
      <div class="card"><div class="k">Sharpe</div><div class="v" id="mSharpe">—</div></div>
      <div class="card"><div class="k">最大回撤</div><div class="v" id="mDd">—</div></div>
      <div class="card"><div class="k">超额(对市值加权基准)</div><div class="v" id="mExcess">—</div></div>
      <div class="card"><div class="k">年化换手(单边)</div><div class="v" id="mTurnover">—</div></div>
      <div class="card"><div class="k">年化双边成交额</div><div class="v" id="mTurnoverGross">—</div></div>
      <div class="card"><div class="k">成交笔数</div><div class="v" id="mTrades">—</div></div>
    </div>
    <div class="snapshot" id="snapshot">数据快照加载中…</div>
    <div class="chart-wrap"><canvas id="chart"></canvas><div class="legend"><span><i class="dot blue"></i>策略</span><span><i class="dot amber"></i>等权基准</span></div></div>
  </div>
</section>

<!-- ============ 盯盘 ============ -->
<section class="page" id="page-monitor">
  <div class="stack">
    <div class="panel">
      <div class="panel-head">
        <h2>持股自动盯盘</h2>
        <div class="switch-wrap">
          <label class="switch"><input type="checkbox" id="monitorEnabled"><span class="slider"></span></label>
          <span id="monitorSwitchLabel" class="muted-sm">已关闭</span>
          <label class="inline-check"><input type="checkbox" id="monitorNotify" checked>推送提醒</label>
          <select id="monitorInterval" style="width:auto">
            <option value="60">每 1 分钟</option>
            <option value="300" selected>每 5 分钟</option>
            <option value="900">每 15 分钟</option>
            <option value="1800">每 30 分钟</option>
            <option value="3600">每 1 小时</option>
            <option value="7200">每 2 小时</option>
            <option value="14400">每 4 小时</option>
            <option value="28800">每 8 小时</option>
            <option value="86400">每 24 小时</option>
          </select>
          <button class="mini-btn" id="monitorRefresh">立即刷新</button>
          <button class="mini-btn" id="monitorNotifyTest">测试推送</button>
        </div>
      </div>
      <div class="snapshot" id="monitorStatus">盯盘未开启。开启后将 24 小时自动盯盘，并在交易时段结合因子模型分析你的持仓。</div>
      <div class="cards" id="monitorSummary" style="margin-top:12px"></div>
      <div id="monitorRows" class="empty">暂无实际持仓。请先录入持仓。</div>
      <h3>我的转债持仓（双低策略口径）</h3>
      <div id="monitorCbRows" class="empty">暂无转债持仓。在「✅ 本周操作」页用「记买入」录入后会在这里自动盯盘。</div>
      <div class="toolbar" style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
        <input id="quickBuySymbol" placeholder="我买了…（代码 600309.SH）" style="width:200px">
        <input id="quickBuyShares" type="number" value="100" placeholder="股数" style="width:80px">
        <input id="quickBuyPrice" type="number" step="0.01" placeholder="买入价(留空=实时)" style="width:150px">
        <button class="mini-btn" id="quickBuyBtn">标记买入</button>
      </div>
      <h3>提醒</h3>
      <div id="monitorAlerts" style="margin-top:8px"></div>
    </div>

    <details class="acc">
      <summary>潜力股入场提醒 与 观察池</summary>
      <div class="acc-body">
        <div class="grid2">
          <div><h3>潜力股入场提醒</h3><div id="entryAlerts" class="empty">加载中</div></div>
          <div><h3>观察池</h3><div id="watchlist" class="empty">加载中</div>
            <div class="toolbar" style="margin-top:10px">
              <input id="watchSymbol" placeholder="观察代码"><input id="watchNote" placeholder="备注">
              <button class="mini-btn" id="watchAdd">加入观察</button>
            </div>
          </div>
        </div>
      </div>
    </details>
  </div>
</section>

<!-- ============ 量化问答 ============ -->
<section class="page" id="page-quantask">
  <div class="panel">
    <div class="panel-head">
      <h2>量化问答（只读）</h2>
      <span class="muted-sm" id="qaMode">加载中…</span>
    </div>
    <p class="empty">LLM 只负责选择工具和解释结果；财务/量化数字全部来自 Quant API。当前不会下单，也不会修改策略。</p>
    <div class="toolbar">
      <select id="qaDataset" style="min-width:220px"></select>
      <label style="display:flex;align-items:center;gap:6px">分析日<input id="qaAsOf" type="date" style="width:150px"></label>
      <input id="qaPositions" placeholder="可选持仓：000001.SZ=1000,600000.SH=1000" style="min-width:300px;flex:1">
    </div>
    <textarea id="qaQuestion" rows="3" placeholder="例如：000001.SZ 的因子分怎么样？或者：这个组合的风险和下跌压力怎么样？" style="width:100%;margin-top:12px;background:#081320;border:1px solid #284661;color:var(--text);border-radius:9px;padding:11px;font-size:13px;resize:vertical"></textarea>
    <div class="toolbar" style="margin-top:10px">
      <button class="btn primary" id="qaAsk">提问</button>
      <button class="mini-btn" id="qaExampleFactor">示例：单股因子</button>
      <button class="mini-btn" id="qaExampleRisk">示例：组合风险</button>
      <button class="mini-btn" id="qaClear">新对话</button>
      <span class="muted-sm" id="qaStatus"></span>
    </div>
    <div id="qaAnswer" class="snapshot" style="white-space:pre-wrap;display:none"></div>
    <details class="acc" style="margin-top:12px">
      <summary>查看工具调用与原始结果</summary>
      <div class="acc-body"><pre id="qaTools" class="muted-sm" style="white-space:pre-wrap;overflow:auto;max-height:420px"></pre></div>
    </details>
  </div>
</section>

<!-- ============ 研究 ============ -->
<section class="page" id="page-research">
  <div class="stack">
    <details class="acc" open>
      <summary>🔍 转债策略体检：钱从哪来 / 有多可信 / 什么时候该停</summary>
      <div class="acc-body"><div id="cbResearch" class="empty">加载中</div></div>
    </details>
    <details class="acc">
      <summary>因子 IC 诊断（Newey-West + BHY 多重检验校正）</summary>
      <div class="acc-body"><div id="ic" class="empty">加载中</div><p class="empty" id="icSource"></p></div>
    </details>
    <details class="acc">
      <summary>严格样本外 · 过拟合检验（Deflated Sharpe / PBO / MinTRL）</summary>
      <div class="acc-body"><div id="walkforward" class="empty">加载中</div></div>
    </details>
    <details class="acc">
      <summary>CPCV 组合 Purged 交叉验证（标签口径对比）</summary>
      <div class="acc-body"><div id="cpcv" class="empty">加载中</div></div>
    </details>
    <details class="acc">
      <summary>风格归因（beta 中性）</summary>
      <div class="acc-body"><div id="attributionPanel" class="empty">加载中</div></div>
    </details>
    <details class="acc">
      <summary>实验记录</summary>
      <div class="acc-body"><div id="experiments" class="empty">加载中</div></div>
    </details>
    <details class="acc">
      <summary>前向测试（真样本外）</summary>
      <div class="acc-body">
        <div class="toolbar" style="margin-bottom:10px">
          <button class="mini-btn" id="freezeTargets">冻结当前目标组合</button>
          <button class="mini-btn" id="refreshForward">刷新</button>
        </div>
        <div id="forwardTest" class="empty">加载中</div>
      </div>
    </details>
  </div>
</section>

<!-- ============ 组合 ============ -->
<section class="page" id="page-portfolio">
  <div class="stack">
    <div class="grid2">
      <div class="panel"><h2>最新目标组合</h2><div id="targets" class="empty">暂无（先运行回测）</div></div>
      <div class="panel"><h2>量化推荐 · 风险调整</h2><div id="recommendations" class="empty">暂无（先运行回测）</div></div>
    </div>
    <div class="panel">
      <h2>持仓 vs 模型目标</h2>
      <div id="positionGap" class="empty">加载中</div>
    </div>
    <div class="grid2">
      <div class="panel"><h2>实际持仓</h2><div id="portfolio" class="empty">加载中</div>
        <div class="toolbar" style="margin-top:12px">
          <input id="holdingSymbol" placeholder="代码"><input id="holdingShares" type="number" placeholder="股数" value="100" style="width:80px"><input id="holdingCost" type="number" step="0.01" placeholder="成本">
          <button class="mini-btn" id="markHolding">标记/更新实际持仓</button>
        </div>
      </div>
      <div class="panel"><h2>纸面交易</h2><div id="paperBroker" class="empty">加载中</div>
        <div class="toolbar" style="margin-top:12px">
          <input id="tradeSymbol" placeholder="代码"><select id="tradeSide" style="width:auto"><option value="buy">买入</option><option value="sell">卖出</option></select>
          <input id="tradeShares" type="number" placeholder="股数" value="100" style="width:80px"><input id="tradePrice" type="number" step="0.01" placeholder="价格">
          <button class="mini-btn" id="paperTrade">纸面交易</button>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- ============ 助手 ============ -->
<section class="page" id="page-assistant">
  <div class="stack">
    <div class="panel"><h2>每日量化方案</h2><div id="dailyPlan" class="empty">加载中</div></div>
    <div class="grid2">
      <div class="panel"><h2>新闻与风险事件</h2><div id="news" class="empty">加载中</div></div>
      <div class="panel"><h2>每周复盘与知识候选</h2><div id="weekly" class="empty">加载中</div></div>
    </div>
    <div class="panel"><h2>策略版本</h2><div id="strategyVersions" class="empty">加载中</div></div>
    <details class="acc">
      <summary>24 小时加密盯盘（数字资产）</summary>
      <div class="acc-body"><div id="cryptoMonitor" class="empty">加载中</div></div>
    </details>
  </div>
</section>

<section class="page active" id="page-etf">
  <div class="panel">
    <div class="panel-head">
      <h2>ETF 执行验证（试点）</h2>
      <span class="muted-sm" id="etfMeta">加载中…</span>
    </div>
    <div class="snapshot" id="etfStatus">加载中…</div>
    <div class="cards" id="etfCards"></div>
    <p class="empty">这条线的目的不是赚超额，而是验证执行层：<b>滑点中位 &lt; 0.30%</b>、<b>执行率 &gt; 90%</b>、账实一致。
      判据未过之前不要加资金；详细协议见 docs/ETF_EXECUTION_VALIDATION.md。</p>
  </div>
  <div class="panel">
    <h2>本期下单清单<span class="muted-sm" id="etfAsof"></span></h2>
    <div id="etfOrders" class="empty">加载中…</div>
    <p class="empty">信号用当日收盘数据计算，<b>下一交易日开盘执行</b>；成交后把券商成交明细截图/CSV 给我，我录入并算滑点。</p>
  </div>
  <div class="panel">
    <h2>当前持仓<span class="muted-sm" id="etfHoldMeta"></span></h2>
    <div id="etfHoldings" class="empty">加载中…</div>
  </div>
</section>

<section class="page" id="page-convertible">
  <div class="stack">
    <div class="panel">
      <div class="panel-head">
        <h2>可转债「双低」轮动</h2>
        <span class="muted-sm" id="cbParams">加载中…</span>
      </div>
      <details class="acc" open style="margin-bottom:10px">
        <summary>❓ 怎么用？（点开看 3 步，30 秒读完）</summary>
        <div class="acc-body">
          <p style="line-height:1.9;margin:4px 0">
            <b>① 新债申购信息</b>：下面「打新日历」汇总近期新债的申购日/代码/评级（信息汇总，不构成参与建议）。<br>
            <b>② 候选清单</b>：下面「本期候选清单」是系统按双低排序输出的 15 只（含全市场排名/评级/流动性）。作者自用时按等权口径记录到「我的转债持仓」。<br>
            <b>③ 每周检查</b>：跌出清单的<b>卖</b>、新进清单的<b>买</b>；「强赎提醒」里的债<b>必须在最后交易日前卖掉或转股</b>。<br>
            <span class="muted-sm">系统每周一/周四早上自动刷新并推送，你只管照着做。其余标签是研究用的，可以不管。</span>
          </p>
        </div>
      </details>
      <div class="snapshot" id="cbStale" style="display:none;border-color:#7f1d1d"></div>
      <div class="snapshot" id="cbRisk" style="border-color:#7a5c12"></div>
      <div class="snapshot" id="cbStatus">加载中…</div>
      <div class="toolbar" id="cbChecklist" style="margin-top:10px"></div>
      <div class="snapshot">双低 = 收盘价 + 转股溢价率(%)，从低到高买前 N 只，每周轮动。
        下有债底、上有股性，是散户友好策略（机构容量小、看不上）。<b>已过滤</b>：已退市债、新债上市炒作、价格 &lt; 95 的信用风险债。</div>
      <div class="cards" id="cbSummary" style="margin-top:12px"></div>
    </div>
    <div class="panel">
      <h2>本期候选清单（系统输出示例 · 非投资建议）<span class="muted-sm" id="cbAsof"></span></h2>
      <div id="cbPicks" class="empty">加载中</div>
      <p class="empty">操作：按清单等权买入（每只约 1/15 仓位）；每周检查一次，卖出跌出名单的、补入新进名单的。中签/强赎按券商提示处理。</p>
    </div>
    <div class="panel">
      <div class="panel-head"><h2>我的转债持仓（自动盯盘）<span class="muted-sm" id="cbHoldSummary"></span></h2>
        <div class="toolbar">
          <input id="cbCode" placeholder="代码 如128134" style="width:130px">
          <input id="cbShares" type="number" placeholder="张数" value="10" style="width:90px">
          <input id="cbCost" type="number" step="0.01" placeholder="成本价" style="width:100px">
          <button class="mini-btn" id="cbAdd">记买入</button>
        </div>
      </div>
      <div id="cbHoldings" class="empty">加载中</div>
    </div>
    <div class="panel">
      <div class="panel-head"><h2>📋 Paper 对账（记录真实成交价）</h2>
        <div class="toolbar"><button class="mini-btn" id="cbPaperSnap">① 抓当前卖一价填入</button><button class="mini-btn" id="cbSavePaper">② 保存本周对账</button><span class="muted-sm" id="cbPaperMsg"></span><span class="muted-sm" id="cbPaperStat"></span></div>
      </div>
      <div id="cbPaper" class="empty">加载中</div>
      <p class="empty"><b>怎么记</b>：点「① 抓当前卖一价填入」→ 系统把 15 只债的<b>卖一价</b>填进"实际成交价"
      （卖一价 = 你实际要付的价，比收盘价更真实）→ 你核对一下、勾上"成交"→ 点「② 保存本周对账」。
      <b>每周一次，10 秒。</b><br>
      门槛：连续 <b>6~8 周</b>，<b>滑点中位 &lt; 0.3%</b> 且 <b>执行率 &gt; 90%</b>，才够格小资金实盘。<br>
      <b>佣金也要逐笔记</b>（券商常有"最低 5 元/笔"）：几千块的小资金下单，真实成本可能是回测 5bps 假设的 2~3 倍——这个数字只有你自己记了才知道。</p>
    </div>
    <div class="grid2">
      <div class="panel"><h2>⚠️ 强赎提醒（持仓必须处理）</h2><div id="cbRedeem" class="empty">加载中</div></div>
      <div class="panel"><h2>打新日历（新债申购）</h2><div id="cbIssues" class="empty">加载中</div></div>
    </div>
    <div class="grid2">
      <div class="panel"><h2>分年收益</h2><div id="cbYearly" class="empty">加载中</div></div>
      <div class="panel"><h2>净值曲线（2019 起）</h2><div class="chart-wrap"><canvas id="cbChart" style="height:240px"></canvas></div></div>
    </div>
    <details class="acc">
      <summary>回测 / 实盘到底用了哪些规则（点开核对）</summary>
      <div class="acc-body"><p class="muted-sm" id="cbRules">加载中…</p></div>
    </details>
  </div>
</section>
<div class="modal-bg" id="stockModalBg">
  <div class="modal">
    <button class="close" onclick="closeStock()">&times;</button>
    <h3 id="stockTitle">个股因子拆解</h3>
    <div id="stockDetail" class="muted">加载中</div>
  </div>
</div>
<div class="footer">研究用途，不构成投资建议。历史回测不代表未来收益。服务仅监听 127.0.0.1。盯盘服务全天运行，交易时段实时刷新、非交易时段自动降频。</div>
</main>
<script>
const $=id=>document.getElementById(id); let options={};
function pct(v){return (100*v).toFixed(2)+'%'} function num(v){return Number(v).toFixed(3)}
function className(v){return v>0?'positive':v<0?'negative':'neutral'}
async function getJSON(url,opt){const r=await fetch(url,opt);const j=await r.json();if(!r.ok||j.ok===false)throw new Error(j.error||r.statusText);return j}
function drawChart(a,b){const c=$('chart');if(!c)return;const dpr=window.devicePixelRatio||1;const w=c.clientWidth,h=c.clientHeight;if(w<10||h<10){setTimeout(()=>drawChart(a,b),120);return}c.width=w*dpr;c.height=h*dpr;const ctx=c.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);const pad={l:55,r:16,t:16,b:26};const all=a.concat(b),lo=Math.min(...all),hi=Math.max(...all),range=(hi-lo)||1;ctx.strokeStyle='#1d344a';ctx.lineWidth=1;ctx.font='11px sans-serif';ctx.fillStyle='#8fa6bd';for(let i=0;i<=4;i++){const y=pad.t+(h-pad.t-pad.b)*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.fillText((hi-range*i/4).toLocaleString(undefined,{maximumFractionDigits:0}),6,y+4)}function line(vals,color){ctx.beginPath();vals.forEach((v,i)=>{const x=pad.l+(w-pad.l-pad.r)*i/Math.max(1,vals.length-1);const y=pad.t+(h-pad.t-pad.b)*(1-(v-lo)/range);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.strokeStyle=color;ctx.lineWidth=2.4;ctx.stroke()}line(b,'#fbbf24');line(a,'#38bdf8')}
function renderRun(r){window.__last=r;$('resultTitle').textContent=r.dataset_label+' · '+r.period.join(' → ')+' · '+r.trade_count+' trades';const m=r.metrics;$('mCagr').textContent=pct(m.cagr||0);$('mSharpe').textContent=num(m.sharpe||0);$('mDd').textContent=pct(m.max_drawdown||0);$('mExcess').textContent=pct(m.excess_total_return||0);$('mTurnover').textContent=pct(m.annualized_turnover||0);$('mTurnoverGross').textContent=pct(m.annualized_gross_turnover||0);$('mTrades').textContent=String(r.trade_count||0);for(const [id,k] of [['mCagr','cagr'],['mSharpe','sharpe'],['mDd','max_drawdown'],['mExcess','excess_total_return']])$(id).className='v '+className(m[k]||0);drawChart(r.equity,r.benchmark);$('targets').innerHTML=r.targets.length?'<table><thead><tr><th>股票</th><th>目标权重</th></tr></thead><tbody>'+r.targets.map(x=>`<tr><td class=\"symlink\" onclick=\"showStock('${x[0]}')\">${x[0]}</td><td>${pct(x[1])}</td></tr>`).join('')+'</tbody></table><p class="empty">信号日：'+r.target_date+'，下一交易日执行</p>':'<div class="empty">无目标持仓</div>';$('recommendations').innerHTML=r.recommendations&&r.recommendations.length?'<table><thead><tr><th>股票</th><th>建议</th><th>优势</th><th>风险调整</th><th>年化波动</th><th>置信度</th><th>权重</th><th>理由</th><th>标记</th></tr></thead><tbody>'+r.recommendations.map(x=>`<tr><td class="symlink" onclick="showStock('${x.symbol}')">${x.symbol}</td><td><span class="tag">${x.action}</span></td><td>${x.score.toFixed(3)}</td><td class="${className(x.risk_adjusted-0.5)}">${x.risk_adjusted.toFixed(3)}</td><td>${pct(x.volatility)}</td><td>${pct(x.confidence)}</td><td>${pct(x.target_weight)}</td><td>${(x.reasons||[]).join("、")}${(x.risks||[]).length?' · 风险：'+x.risks.join("、"):''}</td><td><button class="mini-btn" onclick="markBought('${x.symbol}',0)">我买了</button></td></tr>`).join('')+'</tbody></table><p class="empty">优势分数不是预期收益率；风险调整分仅用于研究排序。</p>':'<div class="empty">暂无推荐</div>'}
async function run(){ $('run').disabled=true;$('error').textContent='';$('status').textContent='计算中…';$('resultTitle').textContent='正在加载数据并回测…（大数据集首次加载约 30–60 秒）';try{const payload={dataset:$('dataset').value,start:$('start').value,end:$('end').value,rebalance:$('rebalance').value,preset:$('preset').value,alpha_weight:+$('alpha_weight').value,target_volatility:+$('target_volatility').value,max_positions:+$('max_positions').value,max_weight:+$('max_weight').value,sector_max_weight:+$('sector_max_weight').value,cash_buffer:+$('cash_buffer').value,commission_bps:+$('commission_bps').value,stamp_duty_bps:+$('stamp_duty_bps').value,slippage_bps:+$('slippage_bps').value,rebalance_band:+$('rebalance_band').value,market_filter:$('market_filter').value,risk_off_exposure:+$('risk_off_exposure').value,weight_scheme:$('weight_scheme').value,max_turnover:+$('max_turnover').value,portfolio_drawdown_exposure:+$('portfolio_drawdown_exposure').value,portfolio_drawdown_limit:($('portfolio_drawdown_limit').value?+$('portfolio_drawdown_limit').value:null),cvar_alpha:($('cvar_alpha').value?+$('cvar_alpha').value:null),cvar_lambda:+$('cvar_lambda').value,vol_method:$('vol_method').value};const j=await getJSON('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});renderRun(j.result);$('status').textContent='计算完成';$('exportBtn').style.display='inline-block';loadPositionGap()}catch(e){$('error').textContent=e.message;$('status').textContent='运行失败'}finally{$('run').disabled=false}}
async function loadIC(){try{const j=await getJSON('/api/ic');$('ic').innerHTML='<table><thead><tr><th>因子</th><th>IC</th><th>ICIR</th><th>t</th><th>t_NW</th><th>p_NW</th><th>p_BHY</th><th>样本</th></tr></thead><tbody>'+j.rows.map(x=>{const sig=x.p_value_bhy<0.05?'good':(x.p_value_bhy<0.10?'warn':'');return `<tr><td>${x.factor}</td><td class="${className(x.mean_ic)}">${x.mean_ic.toFixed(4)}</td><td>${x.icir.toFixed(3)}</td><td>${x.t_stat.toFixed(2)}</td><td>${x.t_stat_nw.toFixed(2)}</td><td>${x.p_value.toFixed(3)}</td><td class="${sig}">${x.p_value_bhy.toFixed(3)}</td><td>${Number(x.observations||0).toFixed(0)}</td></tr>`}).join('')+'</tbody></table><p class="empty">p_BHY &lt; 0.05 绿色 = 多重检验后显著；0.05–0.10 黄色 = 边际。t_NW 为自相关稳健 t。</p>';$('icSource').textContent=j.source?('来源：'+j.source):''}catch(e){$('ic').textContent=e.message}}

async function loadWalkforward(){try{const w=await getJSON('/api/walkforward');if(!w.available){$('walkforward').innerHTML='<div class="empty">暂无严格样本外结果</div>';return}const dsr=w.deflated_sharpe||0,pbo=w.pbo||0;const dsrCls=dsr>=0.95?'good':(dsr>=0.5?'warn':'bad');const pboCls=pbo<=0.2?'good':(pbo<=0.5?'warn':'bad');$('walkforward').innerHTML='<div class="cards"><div class="card"><div class="k">OOS CAGR</div><div class="v ${className(w.cagr)}">'+pct(w.cagr)+'</div></div><div class="card"><div class="k">OOS Sharpe</div><div class="v">'+num(w.sharpe)+'</div></div><div class="card"><div class="k">超额 CAGR</div><div class="v ${className(w.excess_cagr)}">'+pct(w.excess_cagr)+'</div></div><div class="card"><div class="k">信息比率</div><div class="v ${className(w.information_ratio)}">'+num(w.information_ratio)+'</div></div><div class="card"><div class="k">Deflated Sharpe</div><div class="v '+dsrCls+'">'+num(dsr)+'</div></div><div class="card"><div class="k">PBO（过拟合概率）</div><div class="v '+pboCls+'">'+pct(pbo)+'</div></div><div class="card"><div class="k">MinTRL（年）</div><div class="v">'+num(w.min_track_record_length_years)+'</div></div><div class="card"><div class="k">MinBTL（年）</div><div class="v">'+num(w.min_backtest_length_years)+'</div></div></div><table style="margin-top:12px"><tbody><tr><td>beta</td><td>'+num(w.beta)+'</td><td>alpha（年化）</td><td>'+pct(w.alpha)+'</td></tr><tr><td>试验次数</td><td>'+Number(w.trials||0).toFixed(0)+'</td><td>Haircut Sharpe</td><td>'+num(w.haircut_sharpe_annual)+'</td></tr><tr><td>偏度</td><td>'+num(w.oos_skewness)+'</td><td>峰度</td><td>'+num(w.oos_kurtosis)+'</td></tr><tr><td>窗口数</td><td>'+Number(w.windows||0)+'</td><td>Sharpe(概率显著)</td><td>'+num(w.probabilistic_sharpe)+'</td></tr></tbody></table><p class="empty">Deflated Sharpe ≥0.95 且 PBO ≤0.2 才算通过；当前结论：<b>'+(dsr>=0.95&&pbo<=0.2?'通过':'未通过——不具备可部署的可信度')+'</b>。</p>'+'<p class="empty">Sharpe 标准误(年化) '+num(w.sharpe_se_annual)+' · 自相关修正 t 值 <b>'+num(w.sharpe_t_stat)+'</b> (iid 对照 '+num(w.sharpe_t_stat_iid)+', η='+num(w.autocorrelation_factor)+') → '+(Math.abs(w.sharpe_t_stat)>=1.96?'显著':'<b class="bad">不显著</b>')+'</p>'+'<p class="empty" id="betaNeutralNote"></p>'}catch(e){$('walkforward').textContent=e.message}
loadAttribution()}

async function loadAttribution(){try{const a=await getJSON('/api/attribution');if(!a.available)return;const el=document.getElementById('betaNeutralNote');if(!el)return;el.innerHTML=`Beta 中性：组合 beta=${num(a.beta)}，对冲后 alpha(年化)=${pct(a.hedged_alpha_annual)}，对冲后 Sharpe=${num(a.hedged_sharpe_annual)}，对冲后 t=${num(a.hedged_t_stat)}。原始 Sharpe 有相当一部分来自市场 beta，不是选股能力。`;}catch(e){}}

async function loadAttributionChart(){try{const a=await getJSON('/api/attribution');if(!a.available){$('attributionPanel').innerHTML='<div class="empty">暂无归因结果</div>';return}const exp=a.exposures||{};const entries=Object.entries(exp).sort((p,q)=>Math.abs(q[1])-Math.abs(p[1]));const maxAbs=Math.max(0.001,...entries.map(e=>Math.abs(e[1])));const rows=entries.map(([k,v])=>{const w=Math.round(Math.abs(v)/maxAbs*100);return `<div style="margin:6px 0"><div class="muted-sm" style="display:flex;justify-content:space-between"><span>${k}</span><span class="${v>=0?'good':'bad'}">${v.toFixed(3)}</span></div><div style="background:#14263a;border-radius:6px;height:10px"><div style="width:${w}%;height:10px;border-radius:6px;background:${v>=0?'#34d399':'#fb7185'}"></div></div></div>`}).join('');$('attributionPanel').innerHTML=`<div class="muted-sm">R²=${num(a.r_squared)} · 残差alpha(年化)=${pct(a.residual_alpha_annual)} · beta=${num(a.beta)} · 对冲后alpha=${pct(a.hedged_alpha_annual)} · 对冲后Sharpe=${num(a.hedged_sharpe_annual)}</div>`+rows}catch(e){$('attributionPanel').textContent=e.message}}
async function loadDatasetInfo(){try{const key=$('dataset').value;const d=await getJSON('/api/dataset-info?key='+encodeURIComponent(key));const bits=[];if(d.rows)bits.push('行数 '+Number(d.rows).toLocaleString());if(d.first_date)bits.push(d.first_date+' → '+d.last_date);if(d.size_mb)bits.push(d.size_mb+' MB');if(d.sha256)bits.push('SHA-256 '+d.sha256+'…');$('snapshot').textContent=bits.length?('数据快照 · '+d.label+' · '+bits.join(' · ')):('数据集：'+d.label)}catch(e){$('snapshot').textContent='数据快照不可用：'+e.message}}
async function loadExperiments(){try{const j=await getJSON('/api/experiments');if(!j.rows||!j.rows.length){$('experiments').innerHTML='<div class="empty">暂无实验产物'+((j.missing&&j.missing.length)?'（缺失：'+j.missing.join('、')+'）':'')+'</div>';return}$('experiments').innerHTML='<table><thead><tr><th>实验</th><th>CAGR</th><th>Sharpe</th><th>回撤</th><th>超额</th><th>IR</th></tr></thead><tbody>'+j.rows.map(x=>`<tr><td>${x.name}</td><td>${pct(x.cagr)}</td><td>${num(x.sharpe)}</td><td>${pct(x.max_drawdown)}</td><td class="${className(x.excess)}">${pct(x.excess)}</td><td>${num(x.ir)}</td></tr>`).join('')+'</tbody></table>'}catch(e){$('experiments').textContent=e.message}}
async function loadAssistant(){try{const d=await getJSON('/api/daily-plan');$('dailyPlan').innerHTML=d.plans&&d.plans.length?'<p class="empty">市场状态：'+((d.market_regime||{}).regime||'unknown')+' · 信号日 '+d.signal_date+'</p><table><thead><tr><th>股票</th><th>操作</th><th>买入区间</th><th>止损</th><th>止盈</th><th>理由/风险</th><th>标记</th></tr></thead><tbody>'+d.plans.map(x=>`<tr><td>${x.symbol}</td><td><span class="tag">${x.action}</span></td><td>${x.entry_zone.map(v=>v.toFixed(2)).join(' - ')}</td><td>${x.stop_loss.toFixed(2)}</td><td>${x.take_profit.toFixed(2)}</td><td>${(x.reasons||[]).join('、')}${(x.risks||[]).length?' · '+x.risks.join('、'):''}</td><td><button class="mini-btn" onclick="markBought('${x.symbol}',0)">我买了</button></td></tr>`).join('')+'</tbody></table>'+(d.holdings_analysis&&d.holdings_analysis.length?'<h3>实际持仓诊断</h3><table><thead><tr><th>股票</th><th>操作</th><th>成本</th><th>现价</th><th>盈亏</th><th>止损</th><th>止盈</th><th>理由</th><th>标记</th></tr></thead><tbody>'+d.holdings_analysis.map(x=>`<tr><td>${x.symbol}</td><td><span class="tag">${x.action}</span></td><td>${x.average_cost.toFixed(2)}</td><td>${x.price.toFixed(2)}</td><td class="${className(x.pnl_pct)}">${pct(x.pnl_pct)}</td><td>${x.stop_loss.toFixed(2)}</td><td>${x.take_profit.toFixed(2)}</td><td>${x.reason}</td><td><button class="mini-btn" onclick="markSold('${x.symbol}',${Number(x.shares)||0})">我卖了</button></td></tr>`).join('')+'</tbody></table>':'')+'':'<div class="empty">暂无每日方案，先运行助理任务</div>'}catch(e){$('dailyPlan').textContent=e.message}
try{const p=await getJSON('/api/portfolio');$('portfolio').innerHTML='<p class="empty">总资产 '+Number(p.total_value||p.cash||0).toLocaleString()+' · 现金 '+Number(p.cash||0).toLocaleString()+'</p><table><thead><tr><th>股票</th><th>股数</th><th>成本</th><th>现价</th><th>盈亏</th></tr></thead><tbody>'+(p.position_rows||[]).map(x=>`<tr><td>${x.symbol}</td><td>${x.shares}</td><td>${x.average_cost.toFixed(2)}</td><td>${x.price.toFixed(2)}</td><td class="${className(x.pnl)}">${x.pnl.toFixed(2)}</td></tr>`).join('')+'</tbody></table>'}catch(e){$('portfolio').textContent=e.message}
try{const n=await getJSON('/api/news');const items=[...(n.risk_events||[]),...(n.global||[]).slice(0,8)];$('news').innerHTML=items.length?'<table><thead><tr><th>类别</th><th>情绪</th><th>标题</th></tr></thead><tbody>'+items.slice(0,15).map(x=>`<tr><td>${x.category||''}</td><td class="${className(x.sentiment||0)}">${(x.sentiment||0).toFixed(1)}</td><td>${x.title||''}</td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无新闻快照</div>'}catch(e){$('news').textContent=e.message}
try{const w=await getJSON('/api/weekly-review');const r=w.review||{};$('weekly').innerHTML='<p class="empty">评估 '+Number(r.evaluated||0)+' · 方向命中率 '+pct(r.hit_rate||0)+' · 平均收益 '+pct(r.average_return||0)+'</p><ul>'+((r.reflections||[]).map(x=>`<li>${x}</li>`).join(''))+'</ul>'}catch(e){$('weekly').textContent=e.message}}
async function loadCryptoMonitor(){try{const c=await getJSON('/api/crypto-monitor');$('cryptoMonitor').innerHTML=(c.rows||[]).length?'<p class="empty">交易所 '+c.exchange+' · 就绪 '+Number(c.ready_count||0)+' · 更新时间 '+c.generated_at_utc+'</p><table><thead><tr><th>标的</th><th>状态</th><th>最新价</th><th>MA20</th><th>MA50</th><th>止损</th><th>止盈</th><th>理由</th></tr></thead><tbody>'+(c.rows||[]).map(x=>`<tr><td>${x.symbol}</td><td><span class="tag">${x.status}</span></td><td>${Number(x.last||0).toFixed(2)}</td><td>${Number(x.ma20||0).toFixed(2)}</td><td>${Number(x.ma50||0).toFixed(2)}</td><td>${Number(x.stop_loss||0).toFixed(2)}</td><td>${Number(x.take_profit||0).toFixed(2)}</td><td>${x.reason||''}</td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无加密盯盘数据</div>'}catch(e){$('cryptoMonitor').textContent=e.message}}
async function loadStrategyVersions(){try{const r=await getJSON('/api/strategy-versions');$('strategyVersions').innerHTML=(r||[]).length?'<table><thead><tr><th>版本</th><th>名称</th><th>阶段</th><th>Active</th><th>Sharpe</th><th>回撤</th><th>创建时间</th></tr></thead><tbody>'+(r||[]).map(x=>`<tr><td>${x.version_id}</td><td>${x.name}</td><td>${x.stage}</td><td>${x.active?'是':'否'}</td><td>${Number((x.metrics||{}).sharpe||0).toFixed(3)}</td><td>${pct((x.metrics||{}).max_drawdown||0)}</td><td>${x.created_at_utc||''}</td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无策略版本</div>'}catch(e){$('strategyVersions').textContent=e.message}}
async function loadPaperBroker(){try{const p=await getJSON('/api/paper-broker');$('paperBroker').innerHTML='<p class="empty">现金 '+Number(p.cash||0).toLocaleString()+' · 权益 '+Number(p.equity||0).toLocaleString()+' · 峰值 '+Number(p.peak_equity||0).toLocaleString()+'</p><table><thead><tr><th>标的</th><th>数量</th><th>成本</th><th>市价</th></tr></thead><tbody>'+Object.entries(p.positions||{}).map(([s,x])=>`<tr><td>${s}</td><td>${x.quantity}</td><td>${Number(x.average_cost).toFixed(4)}</td><td>${Number(x.market_price).toFixed(4)}</td></tr>`).join('')+'</tbody></table>'}catch(e){$('paperBroker').textContent=e.message}}
async function loadAlerts(){try{const a=await getJSON('/api/entry-alerts');$('entryAlerts').innerHTML=(a.rows||[]).length?'<p class="empty">就绪 '+Number(a.ready_count||0)+' · 新增提醒 '+Number(a.new_alert_count||0)+' · 数据日 '+a.latest_market_date+'</p><table><thead><tr><th>股票</th><th>状态</th><th>当前价</th><th>买入区间</th><th>止损</th><th>止盈</th><th>理由</th><th>标记</th></tr></thead><tbody>'+(a.rows||[]).map(x=>`<tr><td>${x.symbol}</td><td><span class="tag">${x.status}</span></td><td>${Number(x.current_price||0).toFixed(2)}</td><td>${(x.entry_zone||[]).map(v=>Number(v).toFixed(2)).join(' - ')}</td><td>${Number(x.stop_loss||0).toFixed(2)}</td><td>${Number(x.take_profit||0).toFixed(2)}</td><td>${x.reason||''}</td><td><button class="mini-btn" onclick="markBought('${x.symbol}',${Number(x.current_price||0)})">我买了</button></td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无观察池提醒</div>'}catch(e){$('entryAlerts').textContent=e.message}
try{const w=await getJSON('/api/watchlist');$('watchlist').innerHTML=Object.keys(w).length?'<table><thead><tr><th>股票</th><th>备注</th><th>操作</th></tr></thead><tbody>'+Object.entries(w).map(([s,x])=>`<tr><td>${s}</td><td>${x.note||''}</td><td><button onclick="removeWatch('${s}')">移除</button></td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无观察股票</div>'}catch(e){$('watchlist').textContent=e.message}}
async function addWatch(){try{const payload={action:'add',symbol:$('watchSymbol').value,note:$('watchNote').value};await getJSON('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await loadAlerts()}catch(e){$('error').textContent=e.message}}
async function removeWatch(symbol){try{await getJSON('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'remove',symbol})});await loadAlerts()}catch(e){$('error').textContent=e.message}}
async function paperTrade(){try{const payload={symbol:$('tradeSymbol').value,side:$('tradeSide').value,shares:+$('tradeShares').value,price:+$('tradePrice').value};await getJSON('/api/portfolio/trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await loadAssistant()}catch(e){$('error').textContent=e.message}}
async function markHolding(){try{const payload={symbol:$('holdingSymbol').value,shares:+$('holdingShares').value,average_cost:+$('holdingCost').value};await getJSON('/api/portfolio/holding',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await loadAssistant()}catch(e){$('error').textContent=e.message}}
function switchTab(name){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active',p.id==='page-'+name));try{localStorage.setItem('sq_tab',name)}catch(e){}if(name==='overview'&&window.__last){setTimeout(()=>drawChart(window.__last.equity,window.__last.benchmark),60)}if(name==='convertible'){setTimeout(()=>loadConvertible(),60)}if(name==='etf'){setTimeout(()=>loadEtf(),60)}if(name==='quantask'){setTimeout(()=>loadQuantAskStatus(),60)}}
let monitorTimer=null,monitorPolling=false;
function riskTag(level){return level==='high'?'<span class="tag warning">高</span>':level==='medium'?'<span class="tag">中</span>':'<span class="tag">低</span>'}
function renderMonitor(settings,snap,running,lastError){settings=settings||{};snap=snap||{};if(settings.enabled!==undefined){$('monitorEnabled').checked=!!settings.enabled;$('monitorSwitchLabel').textContent=settings.enabled?'盯盘中':'已关闭'}if(settings.interval_seconds){$('monitorInterval').value=String(settings.interval_seconds)}if(settings.notify!==undefined)$('monitorNotify').checked=!!settings.notify;const enabledNow=$('monitorEnabled').checked;snap.enabled=enabledNow;const parts=[];if(snap.signal_date)parts.push('信号日 '+snap.signal_date);if(snap.generated_at_utc)parts.push('更新 '+String(snap.generated_at_utc).replace('T',' ').slice(0,19));if(snap.dataset_label)parts.push(snap.dataset_label);parts.push(snap.quote_source==='sina_realtime'?('实时价 '+(snap.quote_time||'')):'收盘价');if(snap.market_session)parts.push(snap.market_session);if(running)parts.push('正在计算…');if(lastError)parts.push('错误：'+lastError);$('monitorStatus').textContent=parts.length?parts.join(' · '):(settings.enabled?'盯盘已开启，等待首次计算…':'盯盘未开启。');const s=snap.summary||{};const cards=[['持仓数',Number(snap.holdings||0),''],['止损/减仓',Number(s['止损/减仓']||0),'bad'],['止盈',Number(s['止盈/分批减仓']||0),'good'],['减仓',Number(s['减仓']||0),'warn'],['持有观察',Number(s['持有观察']||0),''],['持仓市值',Math.round(Number(snap.total_market_value||0)).toLocaleString(),'']];$('monitorSummary').innerHTML=cards.map(c=>`<div class="card"><div class="k">${c[0]}</div><div class="v ${c[2]}">${c[1]}</div></div>`).join('');const rows=snap.rows||[];$('monitorRows').innerHTML=rows.length?(snap.has_real_holdings===false?'<div class="empty">尚未录入实际持仓。请用上方「我买了」，或在「助手」「组合」页的列表里点「我买了」。</div>':'')+'<table><thead><tr><th>股票</th><th>股数</th><th>成本</th><th>现价</th><th>盈亏</th><th>权重</th><th>模型分</th><th>波动</th><th>风险</th><th>建议</th><th>止损</th><th>止盈</th><th>理由</th><th>操作</th></tr></thead><tbody>'+rows.map(r=>`<tr><td>${r.symbol}</td><td>${r.shares}</td><td>${Number(r.average_cost).toFixed(2)}</td><td>${Number(r.price).toFixed(2)}</td><td class="${className(r.pnl_pct)}">${pct(r.pnl_pct)}</td><td>${pct(r.weight)}</td><td>${r.score==null?'—':Number(r.score).toFixed(3)}</td><td>${pct(r.volatility)}</td><td>${riskTag(r.risk_level)}</td><td><span class="tag">${r.action}</span></td><td>${Number(r.stop_loss).toFixed(2)}</td><td>${Number(r.take_profit).toFixed(2)}</td><td>${r.reason}</td><td><button class="mini-btn" onclick="markSold('${r.symbol}',${Number(r.shares)||0})">我卖了</button></td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无实际持仓。请到「组合」页添加实际持仓。</div>'
const cbr=snap.cb_rows||[];$('monitorCbRows').innerHTML=cbr.length?('<table><thead><tr><th>代码</th><th>名称</th><th>张数</th><th>成本</th><th>现价</th><th>盈亏</th><th>市值</th><th>双低(全市场排名)</th><th>近20日</th><th>建议</th><th></th></tr></thead><tbody>'+cbr.map(cbRowHtml).join('')+'</tbody></table>'):'<div class="empty">暂无转债持仓。在「✅ 本周操作」页用「记买入」录入后会在这里自动盯盘（强赎/信用/急跌/跌出名单 都会提醒）。</div>'}
function scheduleMonitor(enabled,intervalSec){if(monitorTimer){clearInterval(monitorTimer);monitorTimer=null}monitorPolling=!!enabled;if(enabled){monitorTimer=setInterval(async()=>{if(!monitorPolling)return;try{const j=await getJSON('/api/monitor');renderMonitor(j.settings,j.snapshot,j.running,j.last_error)}catch(e){}},Math.max(60,Number(intervalSec||300))*1000)}}
async function loadMonitor(){try{const j=await getJSON('/api/monitor');renderMonitor(j.settings,j.snapshot,j.running,j.last_error);scheduleMonitor(j.settings&&j.settings.enabled,j.settings&&j.settings.interval_seconds)}catch(e){$('monitorStatus').textContent='盯盘加载失败：'+e.message}}
async function onMonitorToggle(){const enabled=$('monitorEnabled').checked;try{const j=await getJSON('/api/monitor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});renderMonitor(j.settings,j.snapshot);scheduleMonitor(enabled,j.settings&&j.settings.interval_seconds);if(enabled)refreshMonitor()}catch(e){$('monitorStatus').textContent='切换失败：'+e.message}}
async function onMonitorInterval(){const v=Number($('monitorInterval').value);try{const j=await getJSON('/api/monitor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval_seconds:v})});scheduleMonitor(j.settings&&j.settings.enabled,v);$('monitorStatus').textContent='已设为每 '+Math.round(v/60)+' 分钟自动刷新。'}catch(e){$('monitorStatus').textContent='设置失败：'+e.message}}
async function refreshMonitor(){const btn=$('monitorRefresh');btn.disabled=true;$('monitorStatus').textContent='正在刷新…（首次需加载行情，约 30–60 秒）';try{const j=await getJSON('/api/monitor/refresh',{method:'POST'});renderMonitor(j.settings||{},j.snapshot,false,null);await loadMonitorAlerts()}catch(e){$('monitorStatus').textContent='刷新失败：'+e.message}finally{btn.disabled=false}}
async function onMonitorNotify(){const notify=$('monitorNotify').checked;try{await getJSON('/api/monitor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({notify})});$('monitorStatus').textContent=notify?'推送提醒已开启（系统通知 + 已配置的微信/邮件）。':'推送提醒已关闭（仍会盯盘并记录提醒）。'}catch(e){$('monitorStatus').textContent='设置失败：'+e.message}}
async function testNotify(){const btn=$('monitorNotifyTest');btn.disabled=true;$('monitorStatus').textContent='正在发送测试推送…';try{const j=await getJSON('/api/monitor/notify/test',{method:'POST'});const ch=j.channels||{};const parts=Object.entries(ch).map(([k,v])=>k+':'+(v?'成功':'失败'));$('monitorStatus').textContent='已配置渠道：'+((j.configured||[]).join(', ')||'无')+' | 测试结果：'+(parts.join(' · ')||'无（未配置微信/邮件，系统通知见桌面）')}catch(e){$('monitorStatus').textContent='测试失败：'+e.message}finally{btn.disabled=false}}
async function markBought(symbol,price){const sharesStr=prompt('标记「已买入」 '+symbol+'\n\n买入股数（如 100）：','100');if(sharesStr===null)return;const shares=parseInt(sharesStr,10);if(!shares||shares<=0){$('error').textContent='股数无效';return}const hint=(price&&price>0)?Number(price).toFixed(2):'留空 = 用实时价';const pxStr=prompt('实际成交价（'+hint+'）\n提示：若是几天前买入，请填当时的成交价，否则成本与止损会算错：',(price&&price>0)?Number(price).toFixed(2):'');if(pxStr===null)return;const px=pxStr?Number(pxStr):0;try{const j=await getJSON('/api/portfolio/buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,shares:shares,price:px})});$('status').textContent='已标记买入 '+symbol+' '+shares+'股 @ '+Number(j.price).toFixed(2)+'，正在纳入盯盘…';await loadMonitorAlerts();setTimeout(()=>{loadMonitor();loadAssistant();loadAlerts();loadMonitorAlerts()},3000)}catch(e){$('error').textContent='标记失败：'+e.message}}
async function markSold(symbol,heldShares){const dflt=String(heldShares||100);const sharesStr=prompt('标记「已卖出」 '+symbol+'\n\n卖出股数（当前持有 '+dflt+' 股，填 '+dflt+' 即清仓）：',dflt);if(sharesStr===null)return;const shares=parseInt(sharesStr,10);if(!shares||shares<=0){$('error').textContent='股数无效';return}const pxStr=prompt('卖出价格（可留空）：','');if(pxStr===null)return;const px=pxStr?Number(pxStr):0;try{const j=await getJSON('/api/portfolio/sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,shares:shares,price:px})});$('status').textContent='已标记卖出 '+symbol+' '+shares+'股，正在更新盯盘…';await loadMonitorAlerts();setTimeout(()=>{loadMonitor();loadAssistant();loadAlerts();loadMonitorAlerts()},3000)}catch(e){$('error').textContent='标记失败：'+e.message}}
async function quickBuy(){const symbol=$('quickBuySymbol').value.trim();const shares=Number($('quickBuyShares').value||0);const price=Number($('quickBuyPrice').value||0);if(!symbol){$('error').textContent='请输入股票代码';return}await markBought(symbol,0)}
async function loadCpcv(){try{const j=await getJSON('/api/cpcv');if(!j.available){$('cpcv').innerHTML='<div class="empty">暂无 CPCV 结果</div>';return}const rows=j.rows||[];$('cpcv').innerHTML='<table><thead><tr><th>标签口径</th><th>路径数</th><th>平均IC</th><th>中位IC</th><th>标准差</th><th>最差</th><th>最好</th><th>正IC路径占比</th><th>t值</th><th>判定</th></tr></thead><tbody>'+rows.map(r=>{const sig=Math.abs(r.t_stat)>=1.96;return `<tr><td>${r.mode}</td><td>${r.paths}</td><td class="${className(r.mean_ic)}">${r.mean_ic.toFixed(4)}</td><td>${r.median_ic.toFixed(4)}</td><td>${r.std_ic.toFixed(4)}</td><td>${r.min_ic.toFixed(4)}</td><td>${r.max_ic.toFixed(4)}</td><td>${pct(r.positive_path_rate)}</td><td>${r.t_stat.toFixed(2)}</td><td>${sig?'<span class="good">显著</span>':'<span class="bad">不显著</span>'}</td></tr>`}).join('')+'</tbody></table><p class="empty">CPCV 用组合purged交叉验证给出样本外 IC 的<b>分布</b>而非单点。t 值不显著 = 该标签口径的预测力无法与噪声区分。单次切分的高 IC 会在这里被戳破。</p>'}catch(e){$('cpcv').textContent=e.message}}

async function loadLastBacktest(){try{const j=await getJSON('/api/backtest/last');if(j.available){renderRun(j.result);$('status').textContent='已加载上次回测结果';$('exportBtn').style.display='inline-block';loadPositionGap()}else{$('status').textContent='就绪 · 点击「运行回测」开始';$('resultTitle').textContent='等待运行';$('snapshot').textContent='选择数据集与因子组合后，点击「运行回测」。'}}catch(e){$('status').textContent='就绪'}}
async function showStock(sym){$('stockModalBg').classList.add('show');$('stockTitle').textContent=sym+' 因子拆解';$('stockDetail').textContent='加载中…';try{const j=await getJSON('/api/stock/'+encodeURIComponent(sym));if(!j.available){$('stockDetail').textContent='无数据'}else{const f=j.factors||[];const rows=f.map(x=>{const w=Math.max(-100,Math.min(100,(x.z||0)*60));return `<div class="fbar"><span>${x.name} <span class="muted-sm">${x.desc||''}</span></span><span class="${(x.z||0)>=0?'pos':'neg'}">${x.z==null?'—':x.z.toFixed(2)}</span></div>`}).join('');$('stockDetail').innerHTML=`<div class="muted-sm">模型分 ${j.score==null?'—':j.score.toFixed(3)} · 现价 ${j.close} · PE ${j.pe==null?'—':j.pe} · PB ${j.pb==null?'—':j.pb} · ROE ${j.roe==null?'—':j.roe} · 行业 ${j.sector}</div><div style="margin-top:10px">${rows}</div>`}}catch(e){$('stockDetail').textContent=e.message}}
function closeStock(){$('stockModalBg').classList.remove('show')}
async function loadCbRisk(){try{const j=await getJSON('/api/cb/risk');const bad=j.level!=='正常';
const mine=j.have_holdings?('我的持仓 浮盈 '+pct(j.user_pnl||0)+' · 我的回撤 '+(j.user_nav_points>=2?pct(j.user_drawdown):'记录中('+j.user_nav_points+'天)')):'（还没记录持仓，暂按策略回测）';
$('cbRisk').innerHTML=`<b>风控纪律</b> · 依据：<b>${j.based_on}</b> ｜ 状态 <b class="${bad?'bad':'good'}">${j.level}</b> · ${j.action}`
 +`<br>${mine} ｜ 策略回测回撤 ${pct(j.strategy_drawdown)}`
 +`<br>规则：回撤 ${j.rules.map(r=>r.threshold+'→'+r.do).join('；')} ｜ ${j.position_formula}`;
$('cbRisk').style.borderColor = bad ? '#7f1d1d' : '#155e45';
}catch(e){$('cbRisk').textContent='风控加载失败：'+e.message}}
async function loadCbPaper(){try{const j=await getJSON('/api/cb/paper');const rows=j.rows||[];
$('cbPaperStat').textContent=j.weeks?('已对账 '+j.weeks+' 周 · 执行率 '+(j.fill_rate==null?'—':pct(j.fill_rate))+' · 单边成本 '+(j.one_way_cost_bps==null?'—':j.one_way_cost_bps.toFixed(1)+' bps')+'（价差 '+(j.median_spread_bps||0).toFixed(1)+' + 佣金 '+(j.fee_bps==null?0:j.fee_bps).toFixed(1)+'）· 滑点(含跳空) '+pct(j.median_slippage)):'尚未对账';
$('cbPaper').innerHTML=rows.length?'<table><thead><tr><th>代码</th><th>名称</th><th>模型价</th><th>实际成交价</th><th>佣金(元)</th><th>成交</th></tr></thead><tbody>'
 +rows.map(r=>`<tr><td>${r.bond_code}</td><td>${r.bond_name}</td><td>${Number(r.price).toFixed(2)}</td>`
 +`<td><input type="number" step="0.001" style="width:90px" data-code="${r.bond_code}" value="${r.actual_price==null?'':r.actual_price}"></td>`
 +`<td><input type="number" step="0.01" style="width:80px" placeholder="如5" data-fee="${r.bond_code}" value="${r.fee==null?'':r.fee}"></td>`
 +`<td><input type="checkbox" data-filled="${r.bond_code}" ${r.filled?'checked':''}></td></tr>`).join('')+'</tbody></table>':'<div class="empty">暂无清单</div>';
}catch(e){$('cbPaper').textContent=e.message}}
async function snapCbPaper(){const btn=$('cbPaperSnap');btn.disabled=true;$('cbPaperMsg').textContent='正在抓取盘口…';
try{const j=await getJSON('/api/cb/paper/snapshot');let filled=0,missing=[];
 j.rows.forEach(r=>{const inp=document.querySelector(`[data-code="${r.bond_code}"]`);
  if(!inp)return;
  if(r.fill_price){inp.value=r.fill_price;filled++;window.__cbSnap=window.__cbSnap||{};window.__cbSnap[r.bond_code]={quote_time:r.quote_time,ask1:r.ask1,bid1:r.bid1};}
  else{missing.push(r.bond_code);}});
 $('cbPaperMsg').textContent=`已按 ${j.fetched_at} 的盘口填入 ${filled} 只`+(missing.length?`；${missing.length} 只没有盘口（可能停牌/无报价），请手动填`:'')+(j.error?` · 抓取异常 ${j.error}`:'');
}catch(e){$('cbPaperMsg').textContent='抓取失败：'+e.message}finally{btn.disabled=false}}
async function saveCbPaper(){const j=await getJSON('/api/cb/paper');const fills=[];
document.querySelectorAll('[data-code]').forEach(inp=>{const code=inp.dataset.code;const ap=parseFloat(inp.value);
 const cb=document.querySelector(`[data-filled="${code}"]`);const filled=!!(cb&&cb.checked);
 const feeInp=document.querySelector(`[data-fee="${code}"]`);const fee=feeInp?parseFloat(feeInp.value):NaN;
 const snapInfo=(window.__cbSnap||{})[code]||{};
 if(ap||filled) fills.push({code, model_price:(j.rows.find(r=>r.bond_code===code)||{}).price, actual_price:isNaN(ap)?null:ap, filled, fee:isNaN(fee)?null:fee, quote_time:snapInfo.quote_time||null, ask1:snapInfo.ask1||null, bid1:snapInfo.bid1||null});});
if(!fills.length){alert('还没填任何成交价');return}
try{await getJSON('/api/cb/paper/record',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({week:j.week,fills})});$('cbPaperMsg').textContent='✅ 已保存 '+(j.week||'')+' 对账（'+fills.length+' 条）';loadCbPaper()}catch(e){$('cbPaperMsg').textContent='保存失败：'+e.message}}
function cbRowHtml(r){
 const cls=r.severity==='critical'?'bad':(r.severity==='warning'?'warn':'good');
 const rank=(r.rank==null)?'—':(r.rank+'/'+r.rank_universe);
 const dl=(r.double_low==null)?'—':(Number(r.double_low).toFixed(1)+' <span class="muted-sm">('+rank+')</span>');
 const drop=(r.drop_20d==null)?'—':'<span class="'+className(r.drop_20d)+'">'+pct(r.drop_20d)+'</span>';
 const flag=r.stale?' <span class="warn">无行情</span>':'';
 return `<tr><td>${r.code}</td><td>${r.name}${flag}</td><td>${r.shares}</td><td>${Number(r.cost).toFixed(2)}</td>`
  +`<td>${Number(r.price).toFixed(2)}</td><td class="${className(r.pnl_pct)}">${pct(r.pnl_pct)}</td>`
  +`<td>${Number(r.market_value).toLocaleString()}</td><td>${dl}</td><td>${drop}</td>`
  +`<td><span class="${cls}"><b>${r.action}</b></span><div class="muted-sm">${(r.reasons||[]).join('；')}</div></td>`
  +`<td><button class="mini-btn" onclick="cbSell('${r.code}',${r.shares},${Number(r.price)||0})">卖</button></td></tr>`}
async function loadCbHoldings(){try{const j=await getJSON('/api/cb/holdings');const rows=j.rows||[];
const warn=Number(j.warnings||0);
$('cbHoldSummary').textContent=rows.length?('市值 '+Number(j.total_value).toLocaleString()+' · 盈亏 '+pct(j.pnl_pct)+(warn?(' · '+warn+' 条预警'):'')):'';
$('cbHoldings').innerHTML=rows.length?('<table><thead><tr><th>代码</th><th>名称</th><th>张数</th><th>成本</th><th>现价</th><th>盈亏</th><th>市值</th><th>双低(全市场排名)</th><th>近20日</th><th>建议</th><th></th></tr></thead><tbody>'+rows.map(cbRowHtml).join('')+'</tbody></table><p class="empty">建议由策略规则自动生成：强赎/正股ST/正股&lt;3元/跌破95债底区/近20日急跌&gt;15%/浮亏&gt;15%/跌出前 15 名。红灯的当天就该处理。</p>')
 :'<div class="empty">还没有记录。买入后在上面填代码/张数/成本点「记买入」，即可自动盯盘（强赎、信用风险、急跌、跌出名单都会提示）。</div>';
}catch(e){$('cbHoldings').textContent=e.message}}
async function cbAdd(){const code=$('cbCode').value.trim(),sh=+$('cbShares').value,px=+$('cbCost').value;if(!code||!sh||!px){alert('填代码/张数/成本');return}
try{await getJSON('/api/cb/holding',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,shares:sh,price:px})});$('cbCode').value='';loadCbHoldings();loadCbRisk();loadMonitor()}catch(e){alert('失败 '+e.message)}}
async function cbSell(code,held,px){const sh=prompt('卖出张数（当前 '+held+'）',String(held));if(sh===null)return;
const price=prompt('成交价（用于记录盈亏，可留空）',px>0?String(px):'');if(price===null)return;
try{await getJSON('/api/cb/sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,shares:parseInt(sh,10),price:parseFloat(price)||0})});loadCbHoldings();loadCbRisk()}catch(e){alert('失败 '+e.message)}}
async function loadEtf(){
  try{
    const j=await getJSON('/api/etf');
    const money=v=>Number(v||0).toLocaleString('zh-CN',{maximumFractionDigits:0});
    if(!j.available){
      $('etfMeta').textContent='暂无计划（先跑 scripts/etf_plan.py）';
      $('etfStatus').innerHTML='<div class="error" style="display:block"><b>没有可读取的 ETF 计划</b> —— 不会执行任何订单。</div>';
      $('etfCards').innerHTML='';
      $('etfAsof').textContent='';
      $('etfOrders').innerHTML='<div class="empty">暂无计划。</div>';
      $('etfHoldMeta').textContent='';
      $('etfHoldings').innerHTML='<div class="empty">暂无持仓数据。</div>';
      return;
    }
    const ex=j.execution||{};
    const gate=j.plan_gate||{};
    const executable=!!j.executable;
    const status=j.plan_status||gate.status||'unknown';
    const statusText=executable?'可执行':(status==='demo'?'演示（不可执行）':'已阻断');
    const statusCls=executable?'good':(status==='demo'?'warn':'bad');
    const verdict=ex.verdict||(ex.pending>0?'待执行':'—');
    const vCls=verdict==='PASS'?'good':(verdict==='NOT YET'?'bad':'warn');
    $('etfMeta').textContent='数据日 '+(j.data_asof||j.asof||'—')+' · 本金 '+money(j.capital)+' 元 · 执行日 '+(j.execute_on||'—')+' · '+statusText;
    $('etfCards').innerHTML=[
      ['执行门控',statusText,statusCls],
      ['判据',verdict,vCls],
      ['滑点中位',ex.slippage_median==null?'—':num(ex.slippage_median*100)+'%',ex.slippage_median!=null&&Math.abs(ex.slippage_median)<0.003?'good':'warn'],
      ['执行率',ex.fill_rate==null?'—':pct(ex.fill_rate),ex.fill_rate==null?'warn':(ex.fill_rate>0.9?'good':'bad')],
      ['待成交/已成交',(ex.pending||0)+' / '+(ex.filled||0),''],
      ['本期订单',String(executable?(j.orders||[]).length:(j.paper_orders||[]).length),executable?'':((j.paper_orders||[]).length?'warn':'')],
      ['L1 误差',j.l1_error==null?'—':pct(j.l1_error),j.l1_error==null?'warn':(j.l1_error<=0.20?'good':'bad')],
      ['现金残留',j.cash_residual==null?'—':pct(j.cash_residual),j.cash_residual==null?'warn':(j.cash_residual<=0.20?'good':'bad')],
      ['计划失效',j.expires_at?String(j.expires_at).slice(0,16).replace('T',' '):'—',''],
      ['预估佣金',money(j.estimated_commission)+' 元',''],
    ].map(([k,v,cls])=>`<div class="card"><div class="k">${k}</div><div class="v ${cls||''}">${v}</div></div>`).join('');

    const dropped=Object.entries(j.dropped||{});
    const reasons=(j.blocked_reasons||gate.messages||[]);
    const reasonHtml=reasons.length?('<div class="error" style="display:block;margin-bottom:8px"><b>⛔ 执行已阻断</b><br>'+reasons.map(r=>'• '+r).join('<br>')+'</div>'):'';
    const gateMsg=executable?'':reasonHtml;
    const metricHtml=((j.metrics||{}).cagr==null?'未计算回测指标（计划被阻断）':`回测口径（含整手约束）：CAGR ${pct(j.metrics.cagr)} · Sharpe ${num(j.metrics.sharpe||0)} · 最大回撤 ${pct(j.metrics.max_drawdown||0)} · 年化换手(单边) ${num(j.metrics.annualized_turnover||0)}`);
    $('etfStatus').innerHTML=gateMsg+(dropped.length
      ? `<b class="bad">⚠️ ${dropped.length} 个袖套因整手金额过大无法持有</b>（${dropped.map(([s,w])=>s+'：'+w).join('；')}）→ 实际组合风险高于设计组合。`
      : '') + ` <span class="muted-sm">数据日 ${j.data_asof||j.asof||'—'} · 计划生成 ${j.generated_at?String(j.generated_at).slice(0,19).replace('T',' '):'—'} · ${metricHtml}</span>`;

    $('etfAsof').textContent=(j.data_asof||j.asof)?('（数据 '+ (j.data_asof||j.asof) +' 收盘 / 执行 '+ (j.execute_on||'—') +' 开盘）'):'';
    const orders=executable?(j.orders||[]):[];
    const paperOrders=status==='demo'?(j.paper_orders||[]):[];
    let orderHtml='';
    if(executable){
      orderHtml=orders.length?('<table><thead><tr><th>方向</th><th>标的</th><th>份数</th><th>参考价</th><th>金额</th><th>目标权重</th></tr></thead><tbody>'+orders.map(o=>`<tr><td><span class="tag">${o.side}</span></td><td>${o.symbol}</td><td>${Number(o.shares).toLocaleString()}</td><td>${Number(o.ref_price).toFixed(3)}</td><td>${money(o.amount)}</td><td>${pct(o.target_weight)}</td></tr>`).join('')+'</tbody></table>'):'<div class="empty">本期无操作（偏离都在不交易带宽内）</div>';
    }else if(paperOrders.length){
      orderHtml='<div class="error" style="display:block;margin-bottom:8px"><b>🧪 纸面演练清单，绝对不可下单</b> —— 这只是按 4,000 元本金和整手约束算出的模拟订单，用来练流程/记账，不是五袖套可执行策略。</div><table><thead><tr><th>方向</th><th>标的</th><th>模拟份数</th><th>参考价</th><th>模拟金额</th><th>目标权重</th></tr></thead><tbody>'+paperOrders.map(o=>`<tr><td><span class="tag">${o.side}</span></td><td>${o.symbol}</td><td>${Number(o.shares).toLocaleString()}</td><td>${Number(o.ref_price).toFixed(3)}</td><td>${money(o.amount)}</td><td>${pct(o.target_weight)}</td></tr>`).join('')+'</tbody></table>';
    }else{
      orderHtml='<div class="empty">⛔ 计划不可执行，订单已隐藏。</div>';
    }
    $('etfOrders').innerHTML=orderHtml;

    const held=Object.entries(j.holdings||{});
    $('etfHoldMeta').textContent=j.holdings_updated_at?('（更新于 '+String(j.holdings_updated_at).slice(0,19).replace('T',' ')+'）'):'';
    $('etfHoldings').innerHTML=held.length?('<table><thead><tr><th>标的</th><th>份数</th><th>参考价</th><th>市值</th><th>目标权重</th></tr></thead><tbody>'+held.map(([s,sh])=>{const px=Number((j.prices||{})[s]||0);const w=Number((j.targets||{})[s]||0);return `<tr><td>${s}</td><td>${Number(sh).toLocaleString()}</td><td>${px.toFixed(3)}</td><td>${money(px*sh)}</td><td>${pct(w)}</td></tr>`}).join('')+'</tbody></table>'):'<div class="empty">空仓（或还没录入成交明细）</div>';
  }catch(e){$('etfMeta').textContent='加载失败：'+e.message}
}

function parseQaPositions(){
  const text=$('qaPositions').value.trim();
  if(!text)return {};
  const out={};
  for(const chunk of text.split(',')){
    const [symbol,shares]=chunk.split('=').map(x=>x.trim());
    const qty=Number(shares);
    if(!symbol||!Number.isFinite(qty)||qty<=0)throw new Error('持仓格式应为 代码=股数，用逗号分隔');
    out[symbol]=qty;
  }
  return out;
}
async function loadQuantAskStatus(){
  try{
    const j=await getJSON('/api/quant/llm/status');
    $('qaMode').textContent=j.mode==='provider'?('真实模型 · '+j.client):('Mock / 确定性模式 · '+j.client);
  }catch(e){$('qaMode').textContent='模型状态不可用'}
}
async function askQuant(){
  const b=$('qaAsk'); b.disabled=true; $('qaStatus').textContent='查询中…';
  try{
    const positions=parseQaPositions();
    const payload={dataset:$('qaDataset').value,as_of:$('qaAsOf').value,question:$('qaQuestion').value.trim(),positions,session_id:window.__qaSession||''};
    if(!payload.question)throw new Error('请先输入问题');
    const j=await getJSON('/api/quant/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const r=j.result||{};window.__qaSession=r.session_id||payload.session_id;try{localStorage.setItem('sq_qa_session',window.__qaSession||'')}catch(e){}
    $('qaAnswer').style.display='block';
    $('qaAnswer').textContent=(r.answer||'')+(r.degraded?'\n\n[已降级到确定性查询]':'');
    $('qaTools').textContent=JSON.stringify(r.tool_results||[],null,2);
    $('qaStatus').textContent='完成 · '+(r.mode||'unknown')+(r.degraded?' · degraded':'');
  }catch(e){$('qaStatus').textContent='失败：'+e.message}
  finally{b.disabled=false}
}

async function clearQuantSession(){
  try{if(window.__qaSession)await getJSON('/api/quant/session/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:window.__qaSession})})}catch(e){}
  window.__qaSession='';try{localStorage.removeItem('sq_qa_session')}catch(e){}
  $('qaAnswer').style.display='none';$('qaAnswer').textContent='';$('qaTools').textContent='';$('qaStatus').textContent='新对话';
}
async function loadConvertible(){try{const j=await getJSON('/api/convertible');if(!j.available){$('cbPicks').textContent='暂无数据，请先运行 scripts/cb_double_low_report.py';return}
const m=j.metrics||{};const p=j.params||{};$('cbParams').textContent=`top ${p.top_n} · ${p.rebalance_days}天轮动 · 价格≥${p.min_price} · 成本${p.cost_bps}bps · 自${p.start}`;
$('cbSummary').innerHTML=[['年化','cagr'],['Sharpe','sharpe'],['最大回撤','max_drawdown'],['总收益','total_return'],['夏普胜率','win_rate']].map(([k,f])=>`<div class="card"><div class="k">${k}</div><div class="v ${className(f==='sharpe'?(m[f]-1):m[f])}">${f==='sharpe'?Number(m[f]||0).toFixed(2):pct(m[f]||0)}</div></div>`).join('');
$('cbAsof').textContent='数据日 '+(j.picks_asof||'');
const exR=(j.excluded_redeem||[]).length, exS=(j.excluded_st||[]).length;
$('cbStatus').innerHTML=`数据日 <b>${j.picks_asof||'—'}</b> · 本次已剔除 <b>${exR}</b> 只强赎债、<b>${exS}</b> 只正股ST/低价正股债 · 清单 ${(j.picks||[]).length} 只 · 建议每周一刷新`;
const lag=Number(j.data_lag_trading_days);const stale=$('cbStale');
if(isFinite(lag)&&lag>2){stale.style.display='block';stale.innerHTML='⚠️ <b>行情落后 '+lag+' 个交易日</b>（清单数据 '+(j.picks_asof||'—')+'，最近交易日 '+(j.last_trading_day||'—')+'）。这份清单可能已过期，<b>别照着旧清单下单</b>；等自动刷新，或在这台电脑上运行 scripts/update_cb_data.py。'}else{stale.style.display='none'}
$('cbChecklist').innerHTML='<span class="muted-sm">本周三步：</span>'
 +'<span class="chip">① 新债信息：看下方日历</span>'
 +'<span class="chip">② 调仓：按清单等权买入，卖出跌出名单的</span>'
 +'<span class="chip">③ 查强赎：持仓若在强赎名单，最后交易日前卖出/转股</span>';
const pk=j.picks||[];$('cbPicks').innerHTML=pk.length?'<table><thead><tr><th>顺序</th><th>代码</th><th>名称</th><th>价格</th><th>转股溢价率</th><th>双低值</th><th>全市场双低排名</th><th>20日均成交额</th><th>评级</th><th>值得买分*</th></tr></thead><tbody>'+pk.map((x,i)=>`<tr><td>${i+1}</td><td>${x.bond_code}</td><td>${x.bond_name}</td><td>${Number(x.price).toFixed(2)}</td><td>${Number(x.premium).toFixed(2)}%</td><td>${Number(x.double_low).toFixed(2)}</td><td><b>${x.market_rank==null?'—':x.market_rank}</b>/${x.market_universe==null?'—':x.market_universe}</td><td>${x.amount==null?'—':(Number(x.amount)/1e4).toFixed(0)+' 万'}</td><td>${x.rating||''}</td><td class="muted-sm">${x.worth_score==null?'—':x.worth_score}</td></tr>`).join('')+'</tbody></table><p class="empty">* 这 15 只<b>都是全市场双低排名最靠前的</b>（选股依据 = 那一列）；「值得买分」只是把双低值+评级+流动性合成一个顺序，决定<b>你先买哪只</b>。它的权重是人为定的（±20% 扰动下顺序不变，见 docs/RESEARCH_LOG.md），所以别把它当预期收益。</p>':'<div class="empty">无</div>';
const rd=j.redeem_risk||{};const rk=Object.keys(rd);
$('cbRedeem').innerHTML=rk.length?'<table><thead><tr><th>代码</th><th>名称</th><th>状态</th><th>最后交易日</th></tr></thead><tbody>'+rk.map(c=>{const v=rd[c];const inList=j.picks.some(p=>p.bond_code===c);return `<tr><td>${c}</td><td>${v.name||''}</td><td class="bad">${v.status||''}</td><td>${v.last_trade_date||'—'}</td></tr>`}).join('')+'</tbody></table><p class="empty">以上为全市场已公告强赎的债（已自动从买入清单剔除）。<b>若你持有其中任何一只，务必在最后交易日前卖出或转股</b>，否则按 ~100 被赎回。</p>':'<div class="empty">当前无强赎债</div>';
const iss=j.issues||[];
$('cbIssues').innerHTML=iss.length?'<table><thead><tr><th>申购日</th><th>名称</th><th>申购代码</th><th>评级</th><th>上市日</th></tr></thead><tbody>'+iss.slice(0,12).map(x=>`<tr><td>${x.apply_date||''}</td><td>${x.name||''}</td><td>${x.apply_code||''}</td><td>${x.rating||''}</td><td>${x.list_date||'—'}</td></tr>`).join('')+'</tbody></table><p class="empty"><b class="warn">⚠️ 先确认你已开通「可转债权限」</b>：新开户需「2 年交易经验 + 10 万资产」（存量客户豁免），不满足时 App 里根本不显示该权限入口、申购会被拒。<br>打新债：申购日当天在券商 App 按上限申购，中签才缴款，上市首日卖出。<b>零成本、零风险，但别指望它赚钱</b>：2024-2026 年 146 只新债上市首日均涨 <b>+39.7%</b>、破发率仅 <b>0.7%</b>（每签期望约 397 元），可是中签率只有 ~0.1~0.2%，<b>每次按上限申购的期望只有 5~8 元</b>。</p>':'<div class="empty">近期无新债</div>';
const yr=j.yearly||{};$('cbYearly').innerHTML='<table><thead><tr><th>年份</th><th>收益</th></tr></thead><tbody>'+Object.keys(yr).sort().map(y=>`<tr><td>${y}</td><td class="${className(yr[y])}">${pct(yr[y])}</td></tr>`).join('')+'</tbody></table>';
drawLineChart('cbChart',(j.curve||[]).map(x=>x.equity),'#34d399');
const ru=j.rules||{};const rt=$('cbRules');
if(rt){rt.innerHTML='回测规则：'+(((ru.backtest||[]).join('、'))||'—')+'<br>实盘额外剔除：'+(((ru.live_extra||[]).join('、'))||'—')+'<br><b>预期收益请按实盘规则看，别直接引用回测年化。</b>'+((ru.backtest_used_live_exclude)?'<br><span class="warn">注意：本次报告用了含未来函数的旧口径（仅对照）</span>':'')}
}catch(e){$('cbPicks').textContent=e.message}}
function drawLineChart(id,vals,color){const c=$(id);if(!c||!vals.length)return;const dpr=window.devicePixelRatio||1;const w=c.clientWidth,h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;const ctx=c.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);const pad={l:46,r:12,t:12,b:20};const lo=Math.min(...vals),hi=Math.max(...vals),rng=(hi-lo)||1;ctx.strokeStyle='#1d344a';ctx.font='10px sans-serif';ctx.fillStyle='#8fa6bd';for(let i=0;i<=3;i++){const y=pad.t+(h-pad.t-pad.b)*i/3;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.fillText((hi-rng*i/3).toFixed(2),4,y+3)}ctx.beginPath();vals.forEach((v,i)=>{const x=pad.l+(w-pad.l-pad.r)*i/Math.max(1,vals.length-1);const y=pad.t+(h-pad.t-pad.b)*(1-(v-lo)/rng);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke()}
async function loadForwardTest(){try{const j=await getJSON('/api/forward-test');if(!j.available){$('forwardTest').textContent=j.reason?('已冻结 '+j.as_of+'，但尚无后续数据'):'尚未冻结目标组合。先运行回测，再点「冻结当前目标组合」'}else{$('forwardTest').innerHTML='<table><tbody><tr><td>冻结日</td><td>'+j.as_of+'</td><td>评估日</td><td>'+j.evaluated_at+'</td><td>天数</td><td>'+j.days+'</td></tr><tr><td>目标只数</td><td>'+j.n_targets+'</td><td>可定价</td><td>'+j.n_priced+'</td><td></td><td></td></tr><tr><td>组合前向收益</td><td class=\'"+className(j.portfolio_return)+"\'>"+pct(j.portfolio_return)+"</td><td>基准前向收益</td><td>"+pct(j.benchmark_return)+"</td><td>超额</td><td class=\'"+className(j.excess_return)+"\'>"+pct(j.excess_return)+"</td></tr></tbody></table>'}}catch(e){$('forwardTest').textContent=e.message}}
async function freezeTargets(){const b=$('freezeTargets');b.disabled=true;try{const j=await getJSON('/api/forward-test/freeze',{method:'POST'});if(j.ok){$('status').textContent='已冻结目标组合（'+j.as_of+'，'+j.n+' 只）';await loadForwardTest()}else{$('status').textContent=j.error||'冻结失败'}}catch(e){$('status').textContent='冻结失败：'+e.message}finally{b.disabled=false}}
async function loadMarketStatus(){try{const s=await getJSON('/api/market-status');$('marketStatus').textContent='A股 '+s.state+' '+s.time}catch(e){}}
async function loadPositionGap(){try{const g=await getJSON('/api/position-gap');if(g.available===false){$('positionGap').innerHTML='<div class="empty">'+(g.message||'请先运行回测')+'</div>';return}const rows=g.rows||[];const s=g.summary||{};$('positionGap').innerHTML=(g.holdings||0)===0?'<div class="empty">尚未录入实际持仓。请点上方「我买了」。</div>':('<p class="empty">总资产 '+Number(g.total_value||0).toLocaleString()+' · 持仓 '+Number(g.holdings||0)+' 只 · 模型目标 '+Number(g.targets||0)+' 只 · 目标日 '+String(g.target_date||'')+'</p><p class="empty">加仓 '+Number(s.add||0)+' · 减仓 '+Number(s.trim||0)+' · 清仓 '+Number(s.exit||0)+' · 新建 '+Number(s.new||0)+' · 持有 '+Number(s.hold||0)+'</p><table><thead><tr><th>股票</th><th>当前股数</th><th>现价</th><th>当前权重</th><th>模型目标</th><th>差额</th><th>建议</th><th>目标股数</th><th>需买卖</th><th>金额</th><th>备注</th></tr></thead><tbody>'+rows.map(r=>`<tr><td>${r.symbol}</td><td>${r.shares}</td><td>${Number(r.price).toFixed(2)}</td><td>${pct(r.current_weight)}</td><td>${pct(r.target_weight)}</td><td class="${className(r.gap)}">${pct(r.gap)}</td><td><span class="tag">${r.action}</span></td><td>${r.target_shares}</td><td class="${className(r.shares_delta)}">${r.shares_delta>0?'+':''}${r.shares_delta}</td><td>${Math.round(Number(r.delta_notional||0)).toLocaleString()}</td><td>${r.note||''}</td></tr>`).join('')+'</tbody></table><p class="empty">当前权重按你的总资产计算，模型目标为回测目标权重，两者口径不同，差额只表示方向与幅度。</p>')}catch(e){$('positionGap').textContent=e.message}}

async function loadMonitorAlerts(){try{const j=await getJSON('/api/monitor/alerts');const rows=j.rows||[];const unread=Number(j.unread||0);const badge=$('monitorBadge');if(unread>0){badge.textContent=unread;badge.style.display='inline-block'}else{badge.style.display='none'}$('monitorAlerts').innerHTML=rows.length?'<div class="alert-list">'+rows.slice(0,10).map(a=>`<div class="alert-item ${a.severity}"><b>${a.title}</b><div class="msg">${a.message}</div></div>`).join('')+'</div>'+'<button class="mini-btn" id="markAlertsRead">全部标记已读</button>':'<div class="empty">暂无提醒</div>';const mk=$('markAlertsRead');if(mk)mk.onclick=async()=>{try{await getJSON('/api/monitor/alerts/read',{method:'POST'});await loadMonitorAlerts()}catch(e){}}}catch(e){$('monitorAlerts').textContent=e.message}}

async function loadCbResearch(){try{const j=await getJSON('/api/cb/research');
 const a=j.attribution||{}, lv=a.levels||{}, fs=a.factors||{}, race=j.race||[], dsr=j.dsr_pbo||{},
       ens=j.ensemble||{}, fr=j.failure_rules||{}, ipo=j.push_ipo||{}, sens=j.score_sensitivity||{};
 const pctS=v=>(v==null?'—':(100*v).toFixed(2)+'%');
 const row=(k,v,note)=>`<tr><td>${k}</td><td>${v}</td><td class="muted-sm">${note||''}</td></tr>`;
 let h='';
 if(lv.market_annual!=null){
  h+='<h3>钱从哪来（2019-2026 年化，三级分解）</h3><table><thead><tr><th>层级</th><th>数值</th><th>说明</th></tr></thead><tbody>'
   +row('① 转债市场本身（全部可交易债等权）',pctS(lv.market_annual),'市场 beta，谁都能拿到')
   +row('② +过滤链（价格≥95/流动性/纯债溢价率/次新/可成交）','+'+pctS(lv.filter_gain),'避开烂债与买不到的债')
   +row('③ +双低排序 Top-15','+'+pctS(lv.rank_gain),'策略真正的"选股"部分')
   +row('<b>合计</b>','<b>'+pctS(lv.picks_annual)+'</b>','回测口径（含成本后约 '+pctS((a.levels||{}).picks_annual==null?null:0.1767)+'）')
   +'</tbody></table>';
 }
 if(fs.premium){
  h+='<h3>双低排序赚的是什么（Fama-MacBeth，'+((a.periods)||'—')+' 期横截面回归）</h3><table><thead><tr><th>因子</th><th>年化贡献</th><th>t 值 / 是否显著</th></tr></thead><tbody>';
  const order=['premium','log_price','pbond','log_size','log_amount','age_years'];
  const zh={premium:'低转股溢价率（双低的另一半）',log_price:'低价',pbond:'信用（纯债溢价率接近债底）',log_size:'规模（我们偏大盘=拖累）',log_amount:'流动性',age_years:'上市天数'};
  for(const k of order){const f=fs[k];if(!f)continue;
   const sig=Math.abs(f.nw_t)>=2?'<b class="good">显著</b>':(Math.abs(f.nw_t)>=1.65?'<span class="warn">边际</span>':'<span class="muted">不显著</span>');
   h+=row(zh[k]||k,(f.annual_contribution>=0?'+':'')+pctS(f.annual_contribution),'t = '+Number(f.nw_t).toFixed(2)+' · '+sig);}
  h+=row('<b>残差（双低排序本身）</b>','<b>'+(a.residual_annual>=0?'+':'')+pctS(a.residual_annual)+'</b>','线性因子解释不了的剩余部分');
  h+='</tbody></table><p class="empty">读法：排序的超额<strong>几乎都能被"低转股溢价率"解释</strong>（+4.33%/年、显著），"双低"里那点独立信息只有 '+pctS(a.residual_annual)+'；规模上是<b>拖累</b>（我们买的多是大盘债）。</p>';
 }
 if(race.length){
  h+='<h3>赛马：只改排序，不改过滤</h3><table><thead><tr><th>配置</th><th>CAGR</th><th>回撤</th></tr></thead><tbody>';
  for(const r of race){const star=r.label.indexOf('当前')>=0?' <b class="good">← 当前</b>':'';
   h+=row(r.label+star,pctS(r.cagr)+' / Sharpe '+Number(r.sharpe).toFixed(2),'最大回撤 '+pctS(r.max_drawdown));}
  h+='</tbody></table><p class="empty">结论：只按低溢价赚得最多（21.5%）但回撤 −34.7%；只按低价回撤小但赚得少。<b>「双低」的价值不是收益最高，而是回撤最小</b>——两条腿互相抵消风险。</p>';
 }
 if(ens.ensemble_50_50){
  h+='<h3>参数选择偏差（B2）</h3><table><thead><tr><th>方案</th><th>CAGR</th><th>最大回撤</th></tr></thead><tbody>'
   +row('Top-15 / 5 天（当前）',pctS(ens.sleeve_a_top15_5d.cagr),'Sharpe '+Number(ens.sleeve_a_top15_5d.sharpe).toFixed(2)+' · 回撤 '+pctS(ens.sleeve_a_top15_5d.max_drawdown))
   +row('Top-20 / 10 天',pctS(ens.sleeve_b_top20_10d.cagr),'Sharpe '+Number(ens.sleeve_b_top20_10d.sharpe).toFixed(2)+' · 回撤 '+pctS(ens.sleeve_b_top20_10d.max_drawdown))
   +row('<b>一半一半（参数对冲）</b>','<b>'+pctS(ens.ensemble_50_50.cagr)+'</b>','Sharpe '+Number(ens.ensemble_50_50.sharpe).toFixed(2)+' · 回撤 '+pctS(ens.ensemble_50_50.max_drawdown)+' ← 风险调整最好')
   +'</tbody></table><p class="empty">top_n=15 与 5 天恰好是全网格的峰值（=看着回测挑出来的），所以对外预期用<b>全网格中位数 '+(100*(ens.grid_median_cagr||0.175)).toFixed(1)+'%</b>，不要用峰值。<br>过滤链的"保费"：不加风险过滤 18.97%/回撤 −20.2%，加过滤 17.67%/回撤 −12.4% —— <b>每年花 1.3% 买 8 个点的回撤保护</b>。</p>';
 }
 if(dsr.dsr!=null){
  h+='<h3>有多可信（多重检验）</h3><table><tbody>'
   +row('DSR（紧缩夏普）',Number(dsr.dsr).toFixed(3)+` &nbsp;<span class="${dsr.dsr>=0.95?'good':'warn'}">${dsr.dsr>=0.95?'通过':'未通过 (>0.95 才算)'}</span>`,'我们诚实登记了 '+(dsr.n_trials||'—')+' 次试验')
   +row('PBO（回测过拟合概率）',Number((dsr.pbo||{}).pbo||0).toFixed(3),'越高越说明"只是拟合了历史"')
   +row('值得买分稳健性','Spearman '+(sens.p5_rho==null?'—':Number(sens.p5_rho).toFixed(3)),'权重 ±20% 扰动下排序是否稳定（>0.95 即稳）')
   +'</tbody></table><p class="empty"><b>诚实结论</b>：回测证据够支撑"小资金实测"，<b>不足以支撑"加大资金"</b>。—— 这正是先跑 paper 对账的原因。</p>';
 }
 if(fr.grid&&fr.grid.length){
  h+='<h3>什么时候该停（历史验证过的规则）</h3><table><thead><tr><th>规则</th><th>CAGR / 回撤</th><th>触发</th><th>有效/误报</th></tr></thead><tbody>';
  for(const g of fr.grid.slice(0,6)){h+=row(g.label,pctS(g.cagr)+' / '+pctS(g.max_drawdown),g.triggers+' 次',g.true_positive+' / '+g.false_positive);}
  h+='</tbody></table><p class="empty"><b>定稿建议</b>：保留「回撤 −15% 减半 / −20% 清仓」（历史 0 次触发 = 零成本保险）；<b>不要采纳</b>「滚动 12 个月 &lt;−5% 停手」——4 次触发里 2 次停手全是误报（之后 3 个月 +21.9%），还每年少赚 0.4%。收紧到 −12%/−15% 看着更漂亮，但那是样本内挑参数。</p>';
 }
 if(ipo['2024']){
  const m=['2024','2025','2026'].map(y=>ipo[y]).filter(Boolean);
  const n=m.reduce((s,x)=>s+x.n,0), mean=m.reduce((s,x)=>s+x.mean*x.n,0)/n, br=m.reduce((s,x)=>s+x.break_rate*x.n,0)/n;
  h+='<h3>打新债值多少钱（数据说话）</h3><table><tbody>'
   +row('2024-2026 新债数',n+' 只','')
   +row('上市首日均值','<b class="good">+'+pctS(mean)+'</b>','中位/破发率 '+pctS(br))
   +row('每签(1000 元)期望','约 '+(mean*1000).toFixed(0)+' 元','')
   +row('单次按上限申购期望','约 5~8 元','中签率 ~0.1~0.2%')
   +'</tbody></table><p class="empty">打新<b>零成本零风险</b>，但别指望它成为收入来源。</p>';
 }
 $('cbResearch').innerHTML=h||'<div class="empty">暂无研究结果，请先运行 scripts/cb_attribution.py 等脚本</div>';
}catch(e){$('cbResearch').textContent='研究结果加载失败：'+e.message}}

async function init(){options=await getJSON('/api/options');$('dataset').innerHTML=options.datasets.map(x=>`<option value="${x.key}">${x.label}</option>`).join('');$('preset').innerHTML=options.presets.map(x=>`<option value="${x}">${x}</option>`).join('');const first=options.datasets[0];$('start').value=first.start;$('end').value=first.end;$('preset').value='forecast_quality';$('dataset').onchange=()=>{const d=options.datasets.find(x=>x.key===$('dataset').value);$('start').value=d.start;$('end').value=d.end;loadDatasetInfo()};$('qaDataset').innerHTML=options.datasets.map(x=>`<option value="${x.key}">${x.label}</option>`).join('');const qaDefault=options.datasets.find(x=>x.key==='csi300_full')||first;$('qaDataset').value=qaDefault.key;$('qaAsOf').value=qaDefault.end;$('run').onclick=run;$('paperTrade').onclick=paperTrade;$('qaAsk').onclick=askQuant;$('qaClear').onclick=clearQuantSession;$('qaExampleFactor').onclick=()=>{$('qaQuestion').value='000001.SZ 的因子分怎么样？'};$('qaExampleRisk').onclick=()=>{$('qaQuestion').value='这个组合的风险和下跌压力怎么样？';};$('qaDataset').onchange=()=>{const d=options.datasets.find(x=>x.key===$('qaDataset').value);if(d)$('qaAsOf').value=d.end};$('markHolding').onclick=markHolding;$('watchAdd').onclick=addWatch;window.removeWatch=removeWatch;document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));let saved='etf';try{saved=localStorage.getItem('sq_tab')||'etf';window.__qaSession=localStorage.getItem('sq_qa_session')||''}catch(e){};if(saved==='overview'){}switchTab(saved);$('monitorEnabled').onchange=onMonitorToggle;$('monitorInterval').onchange=onMonitorInterval;$('monitorRefresh').onclick=refreshMonitor;$('monitorNotify').onchange=onMonitorNotify;$('monitorNotifyTest').onclick=testNotify;$('quickBuyBtn').onclick=quickBuy;$('freezeTargets').onclick=freezeTargets;$('refreshForward').onclick=loadForwardTest;$('stockModalBg').onclick=e=>{if(e.target.id==='stockModalBg')closeStock()};$('cbAdd').onclick=cbAdd;window.cbSell=cbSell;$('cbSavePaper').onclick=saveCbPaper;$('cbPaperSnap').onclick=snapCbPaper;loadIC();loadWalkforward();loadCpcv();loadExperiments();loadDatasetInfo();loadMarketStatus();loadAssistant();loadAlerts();loadPaperBroker();loadStrategyVersions();loadCryptoMonitor();loadMonitor();loadMonitorAlerts();loadLastBacktest();loadForwardTest();loadAttributionChart();loadConvertible();loadCbHoldings();loadCbRisk();loadCbPaper();loadCbResearch();loadQuantAskStatus();loadEtf()}
window.addEventListener('resize',()=>{if(window.__last)drawChart(window.__last.equity,window.__last.benchmark)});init();
</script>
</body></html>'''


def _prewarm_default_dataset() -> None:
    """Load the default dataset in the background so the first backtest is fast.

    Building the 1.1M-row MarketData takes ~30s cold; doing it at startup (the
    service runs 24/7) means the user's first click is served from memory.
    """
    try:
        key = next(iter(DATASETS))
        _get_market(key)
    except Exception:
        pass
    # 转债面板（79 万行行情 + 77 万行成交额）第一次读要 4~5 秒。
    # 服务是常驻的，启动时热一下，用户点"本周操作"就不会卡。
    try:
        _cb_panel(str(PROJECT_ROOT / "data" / "real" / "convertible_bonds_hist.csv"),
                  str(PROJECT_ROOT / "data" / "real" / "cb_volume.csv"))
    except Exception:
        pass



def _lan_ip() -> str:
    """Best-effort LAN IPv4 of this Mac (no traffic is actually sent)."""
    import socket as _s
    try:
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sk.settimeout(0.5)
        sk.connect(("8.8.8.8", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Local StockQuant dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    url = f"http://{args.host}:{args.port}"
    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"StockQuant Control Room: {url}")
    # LAN URL for family use (phone on the same WiFi)
    try:
        _lan = _lan_ip()
        if _lan:
            print("  " + "=" * 52)
            print("  给家人用（手机连同一个 WiFi 打开）:")
            print(f"    http://{_lan}:{args.port}/family?k={_access_token()}")
            print("  " + "=" * 52)
    except Exception:
        pass
    _start_monitor_thread()
    threading.Thread(target=_prewarm_default_dataset, name="dataset-prewarm", daemon=True).start()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
