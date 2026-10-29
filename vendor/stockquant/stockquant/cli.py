from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Optional

from stockquant.analysis import analyze_factor_ic
from stockquant.analysis.attribution import style_attribution
from stockquant.analysis.data_diagnostics import diagnose_csv_missingness, format_report
from stockquant.analysis.ic import analyze_ic_decay, write_ic_report
from stockquant.data.versioning import verify_manifest, write_manifest
from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.data import (
    AkshareProvider,
    DoltHubProvider,
    QlibArchiveProvider,
    FetchResult,
    build_fetch_result,
    TushareProvider,
    load_membership_csv,
    membership_union,
    write_membership_csv,
    generate_demo_market,
    load_csv,
    normalize_symbol,
    save_fetch_result,
    validate_market,
    write_csv,
)
from stockquant.domain import MarketData
from stockquant.experiment import append_experiment
from stockquant.factors import DEFAULT_FACTORS
from stockquant.portfolio import PortfolioConstraints
from stockquant.report import (
    render_html,
    render_walkforward_html,
    write_metrics_json,
    write_trades_csv,
)
from stockquant.walkforward import CANDIDATES, WalkForwardRunner, with_weights


def _constraints(args: argparse.Namespace) -> PortfolioConstraints:
    return PortfolioConstraints(
        max_positions=args.max_positions,
        max_weight=args.max_weight,
        sector_max_weight=args.sector_max_weight,
        cash_buffer=args.cash_buffer,
        min_score=args.min_score,
        weight_scheme=args.weight_scheme,
        max_turnover=getattr(args, "max_turnover", 1.0),
        cvar_alpha=getattr(args, "cvar_alpha", None),
        cvar_lambda=getattr(args, "cvar_lambda", 0.0),
    )


def _config(args: argparse.Namespace) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=args.initial_cash,
        commission_bps=args.commission_bps,
        stamp_duty_bps=args.stamp_duty_bps,
        slippage_bps=args.slippage_bps,
        lot_size=args.lot_size,
        rebalance=args.rebalance,
        start=args.start,
        end=args.end,
        min_history=args.min_history,
        sector_neutral=not args.no_sector_neutral,
        neutralize_by=getattr(args, "neutralize_by", "sector"),
        target_volatility=None if args.no_target_vol else args.target_volatility,
        market_filter=args.market_filter,
        market_filter_lookback=args.market_filter_lookback,
        risk_off_exposure=args.risk_off_exposure,
        alpha_weight=args.alpha_weight,
        rebalance_band=args.rebalance_band,
        min_trade_weight=args.min_trade_weight,
        constraints=_constraints(args),
        factors=with_weights(DEFAULT_FACTORS, CANDIDATES[args.preset]),
        factor_timing=getattr(args, "factor_timing", False),
        factor_timing_lookback=getattr(args, "factor_timing_lookback", 3),
        factor_timing_risk_off=getattr(args, "factor_timing_risk_off", 0.5),
        portfolio_drawdown_limit=getattr(args, "portfolio_drawdown_limit", None),
        portfolio_drawdown_exposure=getattr(args, "portfolio_drawdown_exposure", 0.5),
        vol_method=getattr(args, "vol_method", "realized"),
        pit_policy=getattr(args, "pit_policy", "session"),
        optimizer_candidate_multiplier=getattr(args, "optimizer_candidate_multiplier", 1.0),
        kelly_fraction=getattr(args, "kelly_fraction", 0.5),
    )


def _load_market_for_run(data_path: str, membership_path: Optional[str] = None) -> MarketData:
    market = load_csv(data_path)
    if membership_path:
        market.membership_by_date = load_membership_csv(membership_path)
    return market


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--rebalance", choices=["monthly", "quarterly", "semiannual", "annual", "weekly", "daily"], default="monthly")
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--max-weight", type=float, default=0.12)
    parser.add_argument("--sector-max-weight", type=float, default=0.35)
    parser.add_argument("--cash-buffer", type=float, default=0.03)
    parser.add_argument("--min-score", type=float, default=-9.0)
    parser.add_argument("--weight-scheme", choices=["score", "equal", "optimize", "kelly"], default="score")
    parser.add_argument("--max-turnover", type=float, default=1.0,
                        help="One-way turnover budget per rebalance (1.0 = unlimited).")
    parser.add_argument("--portfolio-drawdown-limit", type=float, default=None,
                        help="Reduce exposure once running drawdown exceeds this (e.g. 0.12).")
    parser.add_argument("--portfolio-drawdown-exposure", type=float, default=0.5)
    parser.add_argument("--cvar-alpha", type=float, default=None)
    parser.add_argument("--cvar-lambda", type=float, default=0.0)
    parser.add_argument("--vol-method", choices=["realized", "garch"], default="realized")
    parser.add_argument("--pit-policy", choices=["off", "session", "strict"], default="session",
                        help="Gate announcement-derived factors by their availability stamp. "
                             "'session' enforces it where declared and reports the rest as unverified; "
                             "'strict' refuses datasets without stamps; 'off' = legacy behaviour.")
    parser.add_argument("--optimizer-candidate-multiplier", type=float, default=1.0,
                        help="Names entering the risk model before max_positions is enforced (x max_positions). "
                             "1.0 = historical Top-N screen; >1 is an open experiment (measured worse on CSI300-30).")
    parser.add_argument("--kelly-fraction", type=float, default=0.5,
                        help="Share of investable capital deployed by the kelly weight scheme.")
    parser.add_argument("--commission-bps", type=float, default=3.0)
    parser.add_argument("--stamp-duty-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--min-history", type=int, default=80)
    parser.add_argument("--target-volatility", type=float, default=0.18)
    parser.add_argument("--no-target-vol", action="store_true")
    parser.add_argument("--no-sector-neutral", action="store_true")
    parser.add_argument("--neutralize-by", default="sector",
                        help="Neutralisation grouping: sector | cap_decile | random:N | none "
                             "(sector labels are the vendor's CURRENT classification, back-filled into history).")
    parser.add_argument("--market-filter", choices=["none", "ma"], default="none")
    parser.add_argument("--market-filter-lookback", type=int, default=200)
    parser.add_argument("--risk-off-exposure", type=float, default=0.5)
    parser.add_argument("--alpha-weight", type=float, default=1.0)
    parser.add_argument("--rebalance-band", type=float, default=0.02)
    parser.add_argument("--min-trade-weight", type=float, default=0.005)
    parser.add_argument("--preset", choices=sorted(CANDIDATES), default="balanced")
    parser.add_argument("--membership", default=None, help="Optional membership CSV with date,symbol")
    parser.add_argument("--factor-timing", action="store_true",
                        help="Scale exposure by the realized recent cross-sectional IC (factor momentum).")
    parser.add_argument("--factor-timing-lookback", type=int, default=3)
    parser.add_argument("--factor-timing-risk-off", type=float, default=0.5)


def _print_summary(result, label: str = "Backtest") -> None:
    m = result.metrics
    print(f"\n{label} summary")
    print(f"  period: {result.dates[0]} -> {result.dates[-1]}")
    print(f"  CAGR: {100*m.get('cagr',0):.2f}%")
    print(f"  total return: {100*m.get('total_return',0):.2f}%")
    print(f"  Sharpe: {m.get('sharpe',0):.3f}")
    print(f"  max drawdown: {100*m.get('max_drawdown',0):.2f}%")
    method = getattr(getattr(result, "config", None), "benchmark_method", "cap")
    basis = "market-cap weighted" if method == "cap" else "equal-weight"
    print(f"  excess total return vs {basis} benchmark: {100*m.get('excess_total_return',0):.2f}%")
    print(f"  annualized turnover (one-way): {100*m.get('annualized_turnover',0):.2f}%")
    print(f"  annualized gross traded notional: {100*m.get('annualized_gross_turnover',0):.2f}%")
    print(f"  trades: {len(result.trades)}")
    if result.warnings:
        print(f"  warnings: {len(result.warnings)}")


def cmd_demo(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = out_dir / "demo_market.csv"
    rows = generate_demo_market(start=args.data_start, end=args.data_end, seed=args.seed)
    write_csv(rows, str(data_path))
    market = MarketData.from_rows(rows)
    cfg = _config(args)
    cfg = BacktestConfig(
        **{**cfg.__dict__, "start": cfg.start, "end": cfg.end}
    )
    result = BacktestEngine(market, cfg).run()
    render_html(result, str(out_dir / "backtest_report.html"))
    write_metrics_json(result, str(out_dir / "metrics.json"))
    write_trades_csv(result, str(out_dir / "trades.csv"))
    _print_summary(result)
    append_experiment(
        str(out_dir / "experiments.jsonl"),
        kind="demo_backtest",
        config={
            "rebalance": cfg.rebalance,
            "max_positions": cfg.constraints.max_positions,
            "max_weight": cfg.constraints.max_weight,
            "sector_max_weight": cfg.constraints.sector_max_weight,
            "commission_bps": cfg.commission_bps,
            "stamp_duty_bps": cfg.stamp_duty_bps,
            "slippage_bps": cfg.slippage_bps,
        },
        metrics=result.metrics,
        extra={"data": str(data_path), "synthetic": True},
    )
    print(f"  data: {data_path}")
    print(f"  report: {out_dir / 'backtest_report.html'}")

    if args.walkforward:
        runner = WalkForwardRunner(
            market,
            base_config=cfg,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            model=args.wf_model,
            ridge_lambda=args.ridge_lambda,
            embargo_days=args.embargo_days,
            purge_days=args.purge_days,
            label_horizon_days=args.label_horizon_days,
            pbo_splits=args.pbo_splits,
            declared_trials=getattr(args, "declared_trials", 0),
        )
        wf = runner.run()
        render_walkforward_html(wf, str(out_dir / "walkforward_report.html"))
        payload = {
            "oos_metrics": wf.oos_metrics,
            "validation": wf.validation,
            "windows": [
                {
                    "train_start": w.train_start,
                    "train_end": w.train_end,
                    "test_start": w.test_start,
                    "test_end": w.test_end,
                    "selected": w.selected,
                    "train_sharpe": w.train_sharpe,
                    "test_metrics": w.test_metrics,
                    "trades": w.trades,
                }
                for w in wf.windows
            ],
        }
        (out_dir / "walkforward.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        append_experiment(
            str(out_dir / "experiments.jsonl"),
            kind="walkforward",
            config={
                "rebalance": cfg.rebalance,
                "train_days": args.train_days,
                "test_days": args.test_days,
                "step_days": args.step_days,
                "purge_days": args.purge_days,
                "embargo_days": args.embargo_days,
                "label_horizon_days": args.label_horizon_days,
                "pbo_splits": args.pbo_splits,
            },
            metrics=wf.oos_metrics,
            extra={"data": str(data_path), "window_count": len(wf.windows), "synthetic": True},
        )
        print("\nWalk-forward OOS summary")
        print(f"  CAGR: {100*wf.oos_metrics.get('cagr',0):.2f}%")
        print(f"  Sharpe: {wf.oos_metrics.get('sharpe',0):.3f}")
        print(f"  max drawdown: {100*wf.oos_metrics.get('max_drawdown',0):.2f}%")
        print(f"  information ratio: {wf.oos_metrics.get('information_ratio',0):.3f}")
        print(f"  PBO: {wf.oos_metrics.get('pbo', float('nan')):.3f}")
        print(f"  deflated Sharpe: {wf.oos_metrics.get('deflated_sharpe_ratio',0):.3f}")
        print(f"  report: {out_dir / 'walkforward_report.html'}")
        for warning in (wf.validation or {}).get("warnings", []):
            print(f"  WARNING: {warning}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    market = _load_market_for_run(args.data, args.membership)
    cfg = _config(args)
    result = BacktestEngine(market, cfg).run()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    render_html(result, str(out_dir / "backtest_report.html"))
    write_metrics_json(result, str(out_dir / "metrics.json"))
    write_trades_csv(result, str(out_dir / "trades.csv"))
    _print_summary(result)
    append_experiment(
        str(out_dir / "experiments.jsonl"),
        kind="backtest",
        config={
            "rebalance": cfg.rebalance,
            "max_positions": cfg.constraints.max_positions,
            "max_weight": cfg.constraints.max_weight,
            "sector_max_weight": cfg.constraints.sector_max_weight,
            "commission_bps": cfg.commission_bps,
            "stamp_duty_bps": cfg.stamp_duty_bps,
            "slippage_bps": cfg.slippage_bps,
        },
        metrics=result.metrics,
        extra={"data": args.data},
    )
    print(f"report: {out_dir / 'backtest_report.html'}")
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    market = _load_market_for_run(args.data, args.membership)
    cfg = _config(args)
    # Honest multiple-testing accounting: if the researcher did not explicitly
    # declare past trials, default to the cumulative distinct trial count recorded
    # in the local registry, so the Deflated Sharpe does not quietly assume N=0.
    declared = int(getattr(args, "declared_trials", 0) or 0)
    if declared <= 0:
        try:
            from stockquant.analysis.trial_registry import cumulative_trials
            declared = cumulative_trials()
        except Exception:
            declared = 0
    runner = WalkForwardRunner(
        market,
        base_config=cfg,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        model=args.wf_model,
        ridge_lambda=args.ridge_lambda,
        embargo_days=args.embargo_days,
        purge_days=args.purge_days,
        label_horizon_days=args.label_horizon_days,
        pbo_splits=args.pbo_splits,
        declared_trials=declared,
        selection_penalty=getattr(args, "selection_penalty", 0.0),
        shrink_to_equal=getattr(args, "shrink_to_equal", 0.0),
    )
    wf = runner.run()
    try:
        from stockquant.analysis.trial_registry import record_trials
        record_trials([str(t["signature"]) for t in runner.trial_ledger])
    except Exception:
        pass
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    render_walkforward_html(wf, str(out_dir / "walkforward_report.html"))
    payload = {
        "oos_metrics": wf.oos_metrics,
        "validation": wf.validation,
        "windows": [
            {
                "train_start": w.train_start,
                "train_end": w.train_end,
                "test_start": w.test_start,
                "test_end": w.test_end,
                "selected": w.selected,
                "train_sharpe": w.train_sharpe,
                "test_metrics": w.test_metrics,
                "trades": w.trades,
            }
            for w in wf.windows
        ],
    }
    (out_dir / "walkforward.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    append_experiment(
        str(out_dir / "experiments.jsonl"),
        kind="walkforward",
        config={
            "rebalance": cfg.rebalance,
            "train_days": args.train_days,
            "test_days": args.test_days,
            "step_days": args.step_days,
            "purge_days": args.purge_days,
            "embargo_days": args.embargo_days,
            "label_horizon_days": args.label_horizon_days,
            "pbo_splits": args.pbo_splits,
        },
        metrics=wf.oos_metrics,
        extra={"data": args.data, "window_count": len(wf.windows)},
    )
    print("Walk-forward summary")
    print(f"  periods: {len(wf.windows)}")
    print(f"  OOS CAGR: {100*wf.oos_metrics.get('cagr',0):.2f}%")
    print(f"  OOS Sharpe: {wf.oos_metrics.get('sharpe',0):.3f}")
    print(f"  OOS max drawdown: {100*wf.oos_metrics.get('max_drawdown',0):.2f}%")
    print(f"  OOS deflated Sharpe: {wf.oos_metrics.get('deflated_sharpe_ratio',0):.3f}")
    print(f"  PBO: {wf.oos_metrics.get('pbo', float('nan')):.3f}")
    print(f"  report: {out_dir / 'walkforward_report.html'}")
    return 0


def cmd_enrich_alternatives(args: argparse.Namespace) -> int:
    rows = list(csv.DictReader(open(args.data, "r", encoding="utf-8-sig", newline="")))
    if not rows:
        raise SystemExit("No rows in input data")
    symbols = sorted({str(r["symbol"]) for r in rows})
    result = FetchResult(rows=rows, symbols={s: "" for s in symbols}, errors={}, warnings=[], metadata={"source": args.data})
    provider = AkshareProvider(max_workers=1, retries=args.retries, retry_sleep=1.5, pause=0.2)
    if args.alternative in ("forecast", "all"):
        result = provider.enrich_with_forecasts(result, args.start.replace("-", ""), args.end.replace("-", ""))
    if args.alternative in ("holders", "all"):
        result = provider.enrich_with_holders(result, symbols, workers=args.workers)
    out = Path(args.output)
    metadata = out.with_name(out.stem + "_metadata.json")
    save_fetch_result(result, str(out), str(metadata))
    print(f"rows: {len(result.rows)}")
    print(f"data: {out}")
    print(f"metadata: {metadata}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_build_qlib_dataset(args: argparse.Namespace) -> int:
    symbols = None
    if args.symbols_file:
        symbols = [line.strip().upper() for line in Path(args.symbols_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not symbols and args.membership:
        membership = load_membership_csv(args.membership)
        from stockquant.data.membership import deterministic_sample, membership_union

        symbols = deterministic_sample(membership_union(membership), args.sample_size, args.seed)
    if not symbols:
        raise SystemExit("Provide --symbols-file or --membership")
    provider = QlibArchiveProvider(args.archive_url)
    print(f"extracting {len(symbols)} symbols from Qlib archive...")
    rows, errors = provider.build_market_rows(symbols, args.cache_dir)
    if not rows:
        raise SystemExit(f"No rows extracted. Errors: {errors}")
    result = build_fetch_result(rows, symbols, errors, args.cache_dir)
    if args.financial_frequency != "none":
        ak = AkshareProvider(max_workers=1, retries=args.retries, retry_sleep=1.5, pause=0.2)
        print("loading point-in-time performance reports...")
        result = ak.enrich_with_financials(
            result,
            start=args.start.replace("-", ""),
            end=args.end.replace("-", ""),
            annual_only=args.financial_frequency == "annual",
        )
    out = Path(args.output)
    metadata = out.with_name(out.stem + "_metadata.json")
    save_fetch_result(result, str(out), str(metadata))
    selected_path = out.with_name(out.stem + "_symbols.txt")
    selected_path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    print(f"rows: {len(result.rows)}")
    print(f"symbols loaded: {len({r['symbol'] for r in result.rows})}")
    print(f"data: {out}")
    print(f"symbols: {selected_path}")
    print(f"metadata: {metadata}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_fetch_dolt_membership(args: argparse.Namespace) -> int:
    provider = DoltHubProvider()
    membership = provider.fetch_membership(
        index_code=args.index,
        start=args.start,
        end=args.end,
    )
    write_membership_csv(membership, args.output)
    print(f"snapshots: {len(membership)}")
    print(f"union symbols: {len(membership_union(membership))}")
    print(f"output: {args.output}")
    return 0


def cmd_build_dolt_dataset(args: argparse.Namespace) -> int:
    provider = DoltHubProvider()
    result, membership = provider.build_dataset(
        index_code=args.index,
        start=args.start,
        end=args.end,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    if not result.rows:
        raise SystemExit(f"No DoltHub rows returned. Errors: {result.errors}")
    if args.financial_frequency != "none":
        ak = AkshareProvider(max_workers=1, retries=args.retries, retry_sleep=1.5, pause=0.2)
        print("loading point-in-time performance reports...")
        result = ak.enrich_with_financials(
            result,
            start=args.start.replace("-", ""),
            end=args.end.replace("-", ""),
            annual_only=args.financial_frequency == "annual",
        )
    out = Path(args.output)
    membership_out = Path(args.membership_output)
    metadata_out = out.with_name(out.stem + "_metadata.json")
    save_fetch_result(result, str(out), str(metadata_out))
    write_membership_csv(membership, str(membership_out))
    print(f"rows: {len(result.rows)}")
    print(f"symbols loaded: {len({r['symbol'] for r in result.rows})}")
    print(f"data: {out}")
    print(f"membership: {membership_out}")
    print(f"metadata: {metadata_out}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_fetch_akshare(args: argparse.Namespace) -> int:
    provider = AkshareProvider(
        max_workers=args.workers,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        pause=args.pause,
    )
    if args.symbols:
        symbols = {normalize_symbol(s): "" for s in args.symbols.split(",") if s.strip()}
        universe_label = "explicit_symbols"
    else:
        symbols = provider.index_constituents(index_code=args.index)
        universe_label = args.index
        if args.limit:
            keep = sorted(symbols)[: args.limit]
            symbols = {s: symbols[s] for s in keep}
    if not symbols:
        raise SystemExit("No symbols selected")
    start = args.start.replace("-", "")
    end = args.end.replace("-", "")
    print(f"fetching {len(symbols)} symbols from AKShare: {start} -> {end}")
    result = provider.fetch_daily(
        symbols=symbols,
        start=start,
        end=end,
        adjust=args.adjust,
        align_calendar=not args.no_align,
    )
    if args.financial_frequency != "none" and result.rows:
        print("loading point-in-time performance reports...")
        result = provider.enrich_with_financials(
            result,
            start=start,
            end=end,
            annual_only=args.financial_frequency == "annual",
        )
    if not result.rows:
        raise SystemExit(f"AKShare returned no rows. Errors: {result.errors}")
    out = Path(args.output)
    metadata = out.with_name(out.stem + "_metadata.json")
    save_fetch_result(result, str(out), str(metadata))
    print(f"rows: {len(result.rows)}")
    print(f"symbols loaded: {len({r['symbol'] for r in result.rows})}")
    print(f"data: {out}")
    print(f"metadata: {metadata}")
    if result.errors:
        print(f"symbol errors: {len(result.errors)}")
        for symbol, error in list(result.errors.items())[:10]:
            print(f"  {symbol}: {error}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_fetch_tushare(args: argparse.Namespace) -> int:
    from stockquant.execution.config import resolve_tushare_token
    token = resolve_tushare_token(args.token)
    if not token:
        raise SystemExit(
            "Tushare token missing. Put TUSHARE_TOKEN=... in ~/.config/stockquant/tushare.env "
            "or pass --token (never paste it into chat)."
        )
    provider = TushareProvider(token)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    result = provider.fetch_dataset(
        index_code=args.index,
        symbols=symbols,
        start=args.start,
        end=args.end,
        limit=args.limit,
    )
    if not result.rows:
        raise SystemExit(f"Tushare returned no rows. Errors: {result.errors}")
    out = Path(args.output)
    metadata = out.with_name(out.stem + "_metadata.json")
    save_fetch_result(result, str(out), str(metadata))
    print(f"rows: {len(result.rows)}")
    print(f"symbols loaded: {len({r['symbol'] for r in result.rows})}")
    print(f"data: {out}")
    print(f"metadata: {metadata}")
    if result.errors:
        print(f"symbol errors: {len(result.errors)}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_analyze_factors(args: argparse.Namespace) -> int:
    market = _load_market_for_run(args.data, args.membership)
    result = analyze_factor_ic(
        market,
        min_history=args.min_history,
        horizon=args.horizon,
        rebalance=args.rebalance,
        sector_neutral=not args.no_sector_neutral,
    )
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_ic_report(
        result,
        str(out_dir / "factor_ic_report.html"),
        str(out_dir / "factor_ic.json"),
    )
    print("Factor IC summary (t_nw = Newey-West, p_bhy = BHY-adjusted)")
    for factor, m in sorted(result.summary.items(), key=lambda kv: kv[1]["mean_ic"], reverse=True):
        print(
            f"  {factor:10s} mean_ic={m['mean_ic']:.4f} "
            f"icir={m['ic_ir']:.3f} t_nw={m['t_stat_nw']:.3f} "
            f"p_bhy={m.get('p_value_bhy', 1.0):.3f} positive={100*m['positive_rate']:.1f}%"
        )
    if getattr(args, "decay", False):
        decay = analyze_ic_decay(
            market,
            horizons=(5, 10, 20, 60),
            min_history=args.min_history,
            rebalance=args.rebalance,
            sector_neutral=not args.no_sector_neutral,
        )
        lines = ["factor,horizon_days,mean_ic"]
        for factor, per_h in decay.items():
            for horizon, value in per_h.items():
                lines.append(f"{factor},{horizon},{value:.6f}")
        (out_dir / "factor_ic_decay.csv").write_text("\n".join(lines), encoding="utf-8")
        print(f"decay: {out_dir / 'factor_ic_decay.csv'}")
    print(f"report: {out_dir / 'factor_ic_report.html'}")
    return 0


def _rebalance_period_returns(result) -> dict:
    dates = result.dates
    idx = {d: i for i, d in enumerate(dates)}
    signals = sorted(result.target_history)
    out = {}
    for a, b in zip(signals, signals[1:]):
        ia, ib = idx.get(a), idx.get(b)
        if ia is None or ib is None:
            continue
        eq_a = result.equity_curve[ia]
        if eq_a > 0:
            out[a] = result.equity_curve[ib] / eq_a - 1.0
    return out


def cmd_attribution(args: argparse.Namespace) -> int:
    market = _load_market_for_run(args.data, args.membership)
    result = BacktestEngine(market, _config(args)).run()
    portfolio = _rebalance_period_returns(result)
    attribution = style_attribution(
        market,
        portfolio,
        min_history=args.min_history,
        horizon=20,
        rebalance=args.rebalance,
        sector_neutral=not args.no_sector_neutral,
    )
    print("Style exposure attribution")
    print(f"  observations: {int(attribution.get('observations', 0))}")
    print(f"  R^2: {attribution.get('r_squared', 0.0):.3f}")
    print(f"  annualized residual alpha: {100*attribution.get('alpha_annual', 0.0):.2f}%")
    exposures = attribution.get("exposures", {})
    for factor, beta in sorted(exposures.items(), key=lambda kv: abs(kv[1]), reverse=True):
        print(f"  exposure[{factor}] = {beta:.3f}")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "style_attribution.json").write_text(
        json.dumps(attribution, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"report: {out_dir / 'style_attribution.json'}")
    return 0


def cmd_data_diagnostics(args: argparse.Namespace) -> int:
    fields = [x.strip() for x in args.fields.split(",")] if args.fields else None
    report = diagnose_csv_missingness(args.data, fields=fields)
    text = format_report(report, top_n=args.top)
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"report: {args.output}")
    return 0


def cmd_data_manifest(args: argparse.Namespace) -> int:
    if args.verify:
        ok = verify_manifest(args.data)
        print("OK: file unchanged" if ok else "MISMATCH: file has changed")
        return 0 if ok else 1
    dest = write_manifest(args.data, note=args.note)
    print(f"manifest: {dest}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    market = load_csv(args.data)
    dates = market.dates
    print(f"rows: {sum(len(v) for v in market.bars_by_date.values())}")
    print(f"symbols: {len(market.symbols)}")
    print(f"dates: {len(dates)}")
    print(f"range: {dates[0]} -> {dates[-1]}")
    symbols = ", ".join(market.symbols[:30])
    print(f"sample symbols: {symbols}")
    report = validate_market(market)
    print(f"validation_ok: {report.ok}")
    print(f"negative_prices: {report.negative_prices}")
    print(f"invalid_ohlc: {report.invalid_ohlc}")
    print(f"negative_volume: {report.negative_volume}")
    print(f"missing_sector: {report.missing_sector}")
    for issue in report.issues:
        print(f"issue: {issue}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockquant",
        description="Dependency-free stock quant research MVP",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Generate synthetic data and run a full demo")
    demo.add_argument("--output-dir", default="outputs/stockquant_demo")
    demo.add_argument("--data-start", default="2022-01-03")
    demo.add_argument("--data-end", default="2025-12-31")
    demo.add_argument("--seed", type=int, default=20261003)
    demo.add_argument("--walkforward", action="store_true")
    demo.add_argument("--train-days", type=int, default=252)
    demo.add_argument("--test-days", type=int, default=63)
    demo.add_argument("--step-days", type=int, default=63)
    demo.add_argument("--wf-model", choices=["select", "default", "balanced", "momentum", "defensive", "value_quality", "value_low_vol", "quality_growth", "forecast_quality", "ic", "ridge", "ic_blend"], default="select")
    demo.add_argument("--ridge-lambda", type=float, default=50.0)
    demo.add_argument("--embargo-days", type=int, default=25)
    demo.add_argument("--purge-days", type=int, default=0)
    demo.add_argument("--label-horizon-days", type=int, default=20)
    demo.add_argument("--pbo-splits", type=int, default=10)
    _add_common(demo)
    demo.set_defaults(func=cmd_demo)

    bt = sub.add_parser("backtest", help="Run a backtest on a long-format CSV")
    bt.add_argument("--data", required=True)
    bt.add_argument("--output-dir", default="outputs/stockquant_backtest")
    _add_common(bt)
    bt.set_defaults(func=cmd_backtest)

    wf = sub.add_parser("walkforward", help="Run rolling walk-forward evaluation")
    wf.add_argument("--data", required=True)
    wf.add_argument("--output-dir", default="outputs/stockquant_walkforward")
    wf.add_argument("--train-days", type=int, default=252)
    wf.add_argument("--test-days", type=int, default=63)
    wf.add_argument("--step-days", type=int, default=63)
    wf.add_argument("--wf-model", choices=["select", "default", "balanced", "momentum", "defensive", "value_quality", "value_low_vol", "quality_growth", "forecast_quality", "ic", "ridge", "ic_blend"], default="select")
    wf.add_argument("--ridge-lambda", type=float, default=50.0)
    wf.add_argument("--embargo-days", type=int, default=25)
    wf.add_argument("--purge-days", type=int, default=0)
    wf.add_argument("--label-horizon-days", type=int, default=20)
    wf.add_argument("--pbo-splits", type=int, default=10)
    wf.add_argument("--declared-trials", type=int, default=0,
                    help="Number of trials already run outside this walk-forward (for Deflated Sharpe)")
    wf.add_argument("--selection-penalty", type=float, default=0.0,
                    help="Select on train_sharpe - penalty*train_turnover (0 = historical rule).")
    wf.add_argument("--shrink-to-equal", type=float, default=0.0,
                    help="Blend the selected weight vector toward equal weight by this fraction (0 = off).")
    _add_common(wf)
    wf.set_defaults(func=cmd_walkforward)

    enrich = sub.add_parser("enrich-alternatives", help="Add announcement-date alternative data to an existing CSV")
    enrich.add_argument("--data", required=True)
    enrich.add_argument("--output", required=True)
    enrich.add_argument("--alternative", choices=["forecast", "holders", "all"], default="all")
    enrich.add_argument("--start", default="2018-01-01")
    enrich.add_argument("--end", default="2025-12-31")
    enrich.add_argument("--workers", type=int, default=4)
    enrich.add_argument("--retries", type=int, default=3)
    enrich.set_defaults(func=cmd_enrich_alternatives)

    qlib_data = sub.add_parser("build-qlib-dataset", help="Extract selected symbols from a public Qlib archive")
    qlib_data.add_argument("--symbols-file", default=None)
    qlib_data.add_argument("--membership", default=None)
    qlib_data.add_argument("--sample-size", type=int, default=80)
    qlib_data.add_argument("--seed", type=int, default=20261003)
    qlib_data.add_argument("--start", default="2018-01-01")
    qlib_data.add_argument("--end", default="2025-12-31")
    qlib_data.add_argument("--cache-dir", default="data/real/qlib_cache")
    qlib_data.add_argument("--archive-url", default=QlibArchiveProvider.__init__.__defaults__[0])
    qlib_data.add_argument("--financial-frequency", choices=["annual", "quarterly", "none"], default="annual")
    qlib_data.add_argument("--retries", type=int, default=3)
    qlib_data.add_argument("--output", required=True)
    qlib_data.set_defaults(func=cmd_build_qlib_dataset)

    dolt_mem = sub.add_parser("fetch-dolt-membership", help="Fetch historical index membership from DoltHub")
    dolt_mem.add_argument("--index", default="399300.SZ")
    dolt_mem.add_argument("--start", default="2015-01-01")
    dolt_mem.add_argument("--end", default="2025-12-31")
    dolt_mem.add_argument("--output", required=True)
    dolt_mem.set_defaults(func=cmd_fetch_dolt_membership)

    dolt_data = sub.add_parser("build-dolt-dataset", help="Build a historical-membership A-share dataset from DoltHub")
    dolt_data.add_argument("--index", default="399300.SZ")
    dolt_data.add_argument("--start", default="2015-01-01")
    dolt_data.add_argument("--end", default="2025-12-31")
    dolt_data.add_argument("--sample-size", type=int, default=60)
    dolt_data.add_argument("--seed", type=int, default=20261003)
    dolt_data.add_argument("--financial-frequency", choices=["annual", "quarterly", "none"], default="annual")
    dolt_data.add_argument("--retries", type=int, default=3)
    dolt_data.add_argument("--output", required=True)
    dolt_data.add_argument("--membership-output", required=True)
    dolt_data.set_defaults(func=cmd_build_dolt_dataset)

    fetch_ak = sub.add_parser("fetch-akshare", help="Fetch real A-share daily bars from AKShare")
    fetch_ak.add_argument("--index", default="000300")
    fetch_ak.add_argument("--symbols", default=None, help="Comma-separated A-share codes; overrides index")
    fetch_ak.add_argument("--limit", type=int, default=50)
    fetch_ak.add_argument("--start", default="2020-01-01")
    fetch_ak.add_argument("--end", default="2025-12-31")
    fetch_ak.add_argument("--adjust", choices=["", "qfq", "hfq"], default="hfq")
    fetch_ak.add_argument("--workers", type=int, default=1)
    fetch_ak.add_argument("--retries", type=int, default=3)
    fetch_ak.add_argument("--retry-sleep", type=float, default=1.5)
    fetch_ak.add_argument("--pause", type=float, default=0.3)
    fetch_ak.add_argument("--no-align", action="store_true", help="Do not fill missing sessions as suspended")
    fetch_ak.add_argument("--financial-frequency", choices=["annual", "quarterly", "none"], default="annual")
    fetch_ak.add_argument("--output", required=True)
    fetch_ak.set_defaults(func=cmd_fetch_akshare)

    fetch_ts = sub.add_parser("fetch-tushare", help="Fetch PIT-oriented A-share data from Tushare")
    fetch_ts.add_argument("--token", default=None, help="Defaults to TUSHARE_TOKEN")
    fetch_ts.add_argument("--index", default="399300.SZ")
    fetch_ts.add_argument("--symbols", default=None, help="Comma-separated ts_codes; overrides index")
    fetch_ts.add_argument("--limit", type=int, default=50)
    fetch_ts.add_argument("--start", default="2020-01-01")
    fetch_ts.add_argument("--end", default="2025-12-31")
    fetch_ts.add_argument("--output", required=True)
    fetch_ts.set_defaults(func=cmd_fetch_tushare)

    ic = sub.add_parser("analyze-factors", help="Compute factor IC/ICIR diagnostics")
    ic.add_argument("--data", required=True)
    ic.add_argument("--output-dir", default="outputs/stockquant_factor_ic")
    ic.add_argument("--horizon", type=int, default=20)
    ic.add_argument("--rebalance", choices=["monthly", "quarterly", "semiannual", "annual", "weekly", "daily"], default="monthly")
    ic.add_argument("--min-history", type=int, default=80)
    ic.add_argument("--no-sector-neutral", action="store_true")
    ic.add_argument("--membership", default=None)
    ic.set_defaults(func=cmd_analyze_factors)

    inspect = sub.add_parser("inspect", help="Inspect a long-format CSV")
    inspect.add_argument("--data", required=True)
    inspect.set_defaults(func=cmd_inspect)

    ic.add_argument("--decay", action="store_true", help="Also write multi-horizon IC decay")

    attr = sub.add_parser("attribution", help="Style exposure attribution of a preset backtest")
    attr.add_argument("--data", required=True)
    attr.add_argument("--output-dir", default="outputs/stockquant_attribution")
    _add_common(attr)
    attr.set_defaults(func=cmd_attribution)

    diag = sub.add_parser("data-diagnostics", help="Report missingness by field and year")
    diag.add_argument("--data", required=True)
    diag.add_argument("--fields", default=None, help="Comma-separated fields; defaults to the factor schema")
    diag.add_argument("--top", type=int, default=20)
    diag.add_argument("--output", default=None)
    diag.set_defaults(func=cmd_data_diagnostics)

    manifest = sub.add_parser("data-manifest", help="Write or verify a data snapshot manifest")
    manifest.add_argument("--data", required=True)
    manifest.add_argument("--verify", action="store_true")
    manifest.add_argument("--note", default="")
    manifest.set_defaults(func=cmd_data_manifest)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
