from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from stockquant.data import load_csv, load_membership_csv
from stockquant.execution.engine import DailyPlanStrategy, TradingEngine
from stockquant.execution.paper import PaperBroker
from stockquant.execution.risk import RiskLimits, RiskManager
from stockquant.execution.runner import ExecutionRunner
from stockquant.execution.quotes import AkshareSpotQuoteProvider, SnapshotQuoteProvider
from stockquant.execution.versioning import StrategyRegistry
from stockquant.execution.crypto_monitor import CryptoMonitor
from stockquant.execution.crypto_execution import CryptoExecutionEngine, CryptoMonitorStrategy, CryptoPolicy
from stockquant.execution.config import load_env_file, validate_broker_config
from stockquant.execution.types import OrderRequest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / "data" / "state"


def _market():
    data_path = PROJECT_ROOT / "data" / "real" / "qlib_csi300_hist80_v3_clean_2018_2025.csv"
    membership_path = PROJECT_ROOT / "data" / "real" / "dolt_csi300_membership_2018_2025.csv"
    market = load_csv(str(data_path))
    if membership_path.exists():
        market.membership_by_date = load_membership_csv(str(membership_path))
    return market


def command_strategy_register(args):
    registry = StrategyRegistry(str(Path(args.state_dir) / "strategy_registry.json"))
    record = registry.register(args.name, {"file": args.config}, {"sharpe": args.sharpe, "max_drawdown": args.max_drawdown}, stage=args.stage)
    print(record)
    return 0


def command_strategy_list(args):
    registry = StrategyRegistry(str(Path(args.state_dir) / "strategy_registry.json"))
    print(registry.list_versions())
    return 0


def command_strategy_promote(args):
    registry = StrategyRegistry(str(Path(args.state_dir) / "strategy_registry.json"))
    print(registry.promote(args.version_id, args.stage))
    return 0


def command_strategy_rollback(args):
    registry = StrategyRegistry(str(Path(args.state_dir) / "strategy_registry.json"))
    print(registry.rollback())
    return 0


def command_paper_run(args):
    state_dir = Path(args.state_dir)
    market = _market()
    quote_provider = AkshareSpotQuoteProvider() if args.live_quotes else SnapshotQuoteProvider(market)
    broker = PaperBroker(
        str(state_dir / "paper_broker.json"),
        initial_cash=args.initial_cash,
        commission_bps=args.commission_bps,
        slippage_bps=args.slippage_bps,
        quote_provider=quote_provider,
    )
    limits = RiskLimits(
        max_order_notional=args.max_order_notional,
        max_position_weight=args.max_position_weight,
        max_daily_loss=args.max_daily_loss,
        max_drawdown=args.max_drawdown,
        max_quote_age_seconds=args.max_quote_age_seconds,
    )
    risk = RiskManager(limits, str(state_dir / "TRADING_HALTED"))
    engine = TradingEngine(broker, risk, DailyPlanStrategy(), str(state_dir))
    runner = ExecutionRunner(engine, interval_seconds=args.interval)
    runner.run(max_iterations=args.iterations)
    print(f"paper broker: {state_dir / 'paper_broker.json'}")
    print(f"execution log: {state_dir / 'execution_log.jsonl'}")
    return 0


def _load_env_file(path: str) -> None:
    load_env_file(path, os.environ)


def _require_valid_broker_config(args) -> dict:
    report = validate_broker_config(
        args.env_file,
        live=bool(getattr(args, "live", False)),
        require_live_confirmation=bool(getattr(args, "live", False)),
    )
    if not report["ok"]:
        raise SystemExit("broker configuration rejected: " + "; ".join(report["errors"]))
    for warning in report["warnings"]:
        print(f"broker config warning: {warning}", file=sys.stderr)
    return report


def command_live_run(args):
    _load_env_file(args.env_file)
    _require_valid_broker_config(args)
    if os.environ.get("STOCKQUANT_LIVE_CONFIRM") != "YES_I_ACCEPT_RISK" or not args.enable_live:
        raise SystemExit("Live trading requires --enable-live and STOCKQUANT_LIVE_CONFIRM=YES_I_ACCEPT_RISK")
    from stockquant.execution.ccxt_broker import CcxtBroker

    state_dir = Path(args.state_dir)
    proxy = args.proxy or os.environ.get("STOCKQUANT_HTTP_PROXY", "http://127.0.0.1:7897")
    broker = CcxtBroker(args.exchange, testnet=not args.live, proxy_url=proxy)
    limits = RiskLimits(
        max_order_notional=args.max_order_notional,
        max_position_weight=args.max_position_weight,
        max_daily_loss=args.max_daily_loss,
        max_drawdown=args.max_drawdown,
    )
    risk = RiskManager(limits, str(state_dir / "TRADING_HALTED"))
    engine = TradingEngine(broker, risk, DailyPlanStrategy(), str(state_dir))
    runner = ExecutionRunner(engine, interval_seconds=args.interval)
    runner.run(max_iterations=args.iterations)
    return 0


def command_broker_check(args):
    _load_env_file(args.env_file)
    _require_valid_broker_config(args)
    proxy = args.proxy or os.environ.get("STOCKQUANT_HTTP_PROXY", "http://127.0.0.1:7897")
    from stockquant.execution.ccxt_broker import CcxtBroker

    broker = CcxtBroker(args.exchange, testnet=not args.live, proxy_url=proxy)
    account = broker.get_account()
    print(json.dumps({"exchange": broker.exchange.id, "cash": account.cash, "equity": account.equity, "positions": list(account.positions)}, ensure_ascii=False, indent=2))
    return 0


def command_broker_preflight(args):
    report = validate_broker_config(
        args.env_file,
        live=bool(args.live),
        require_live_confirmation=bool(args.live),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def command_crypto_monitor(args):
    monitor = CryptoMonitor(str(Path(args.state_dir)), exchange=args.exchange, proxy_url=args.proxy)
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    import time
    count = 0
    while True:
        payload = monitor.snapshot(symbols)
        print(json.dumps(payload, ensure_ascii=False, default=str))
        count += 1
        if args.iterations and count >= args.iterations:
            break
        time.sleep(max(5, args.interval))
    return 0


def _symbols(raw: str):
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


def _prepare_crypto_broker(args):
    _load_env_file(args.env_file)
    live = bool(getattr(args, "live", False))
    _require_valid_broker_config(args)
    if live:
        if not getattr(args, "enable_live", False) or os.environ.get("STOCKQUANT_LIVE_CONFIRM") != "YES_I_ACCEPT_RISK":
            raise SystemExit("Real trading requires --enable-live and STOCKQUANT_LIVE_CONFIRM=YES_I_ACCEPT_RISK")
    from stockquant.execution.ccxt_broker import CcxtBroker

    proxy = args.proxy or os.environ.get("STOCKQUANT_HTTP_PROXY", "http://127.0.0.1:7897")
    if args.exchange.lower() == "okx":
        simulated = not live
    else:
        simulated = None
    return CcxtBroker(args.exchange, testnet=not live, proxy_url=proxy, simulated_trading=simulated), proxy


def command_crypto_live_run(args):
    broker, proxy = _prepare_crypto_broker(args)
    symbols = _symbols(args.symbols)
    state_dir = Path(args.state_dir)
    policy = CryptoPolicy(
        symbols=tuple(symbols),
        target_weight=args.target_weight,
        min_order_notional=args.min_order_notional,
        buy_cooldown_seconds=args.buy_cooldown_seconds,
        take_profit_fraction=args.take_profit_fraction,
    )
    limits = RiskLimits(
        max_order_notional=args.max_order_notional,
        max_position_weight=args.max_position_weight,
        max_daily_loss=args.max_daily_loss,
        max_drawdown=args.max_drawdown,
        max_quote_age_seconds=args.max_quote_age_seconds,
        max_spread_bps=args.max_spread_bps,
    )
    risk = RiskManager(limits, str(state_dir / "TRADING_HALTED"), str(state_dir / "crypto_risk_state.json"))
    strategy = CryptoMonitorStrategy(policy)
    monitor = CryptoMonitor(str(state_dir), exchange=args.exchange, proxy_url=proxy)
    engine = CryptoExecutionEngine(broker, risk, strategy, str(state_dir), policy, dry_run=args.dry_run)
    count = 0
    while True:
        payload = monitor.snapshot(symbols)
        result = engine.run_once(payload)
        result["generated_at_utc"] = payload.get("generated_at_utc")
        print(json.dumps(result, ensure_ascii=False, default=str))
        count += 1
        if args.iterations and count >= args.iterations:
            return 0
        time.sleep(max(5, args.interval))


def command_crypto_smoke_test(args):
    if args.market_notional > 0 and not args.place_order:
        raise SystemExit("--market-notional requires --place-order")
    broker, _proxy = _prepare_crypto_broker(args)
    symbol = args.symbol.upper()
    quote = broker.get_quotes([symbol])[symbol]
    account = broker.get_account()
    initial_position = account.positions.get(symbol)
    initial_position_qty = float(initial_position.quantity) if initial_position else 0.0
    result = {
        "exchange": broker.exchange.id,
        "symbol": symbol,
        "account": {"cash": account.cash, "equity": account.equity, "positions": list(account.positions)},
        "quote": quote.__dict__,
        "place_order": bool(args.place_order),
    }
    if args.place_order:
        market = broker.exchange.market(symbol)
        min_amount = (((market.get("limits", {}) or {}).get("amount", {}) or {}).get("min") or 0.0)
        min_cost = (((market.get("limits", {}) or {}).get("cost", {}) or {}).get("min") or 0.0)
        limit_price = quote.bid * (1.0 - max(0.001, args.limit_offset_pct))
        notional = max(args.notional, float(min_cost or 0.0))
        qty = max(notional / limit_price, float(min_amount or 0.0))
        order = OrderRequest(
            symbol=symbol,
            side="buy",
            quantity=qty,
            order_type="limit",
            limit_price=limit_price,
            reason="demo post-only limit smoke test",
            strategy_id="crypto_smoke_v1",
            params={"postOnly": True},
        )
        fill = broker.submit_order(order, quote)
        time.sleep(max(1, args.wait_seconds))
        open_orders = broker.fetch_open_orders(symbol)
        matched = [o for o in open_orders if str(o.get("id")) == fill.order_id]
        if matched:
            broker.cancel_order(fill.order_id, symbol)
        remaining = broker.fetch_open_orders(symbol)
        result["limit_order"] = {
            "order_id": fill.order_id,
            "submitted": True,
            "open_after_submit": bool(matched),
            "cancelled": bool(matched),
            "still_open_after_cancel": any(str(o.get("id")) == fill.order_id for o in remaining),
        }
    if args.market_notional > 0:
        market_qty = args.market_notional / quote.ask
        buy = broker.submit_order(
            OrderRequest(
                symbol=symbol,
                side="buy",
                quantity=market_qty,
                reason="demo market buy fill smoke test",
                strategy_id="crypto_smoke_v1",
            ),
            quote,
        )
        time.sleep(max(1, args.wait_seconds))
        buy_status = broker.fetch_order(buy.order_id, symbol)
        raw_buy_filled = buy_status.get("filled")
        buy_filled = float(raw_buy_filled if raw_buy_filled is not None else 0.0)
        if buy_filled <= 0 and str(buy_status.get("status", "")).lower() in {"closed", "filled"}:
            buy_filled = float(buy_status.get("amount") or 0.0)
        account_after_buy = broker.get_account()
        account_after_sell = account_after_buy
        position = account_after_buy.positions.get(symbol)
        position_qty_after_buy = float(position.quantity) if position else 0.0
        result["market_buy"] = {
            "order_id": buy.order_id,
            "status": buy_status.get("status"),
            "filled": buy_filled,
            "average": buy_status.get("average") or buy_status.get("price"),
            "position_quantity_before": initial_position_qty,
            "position_quantity_after_buy": position_qty_after_buy,
            "position_delta_after_buy": position_qty_after_buy - initial_position_qty,
        }
        if buy_filled > 0:
            sell_qty = min(buy_filled, float(position.quantity) if position else buy_filled)
            sell = broker.submit_order(
                OrderRequest(
                    symbol=symbol,
                    side="sell",
                    quantity=sell_qty,
                    reason="demo market sell reconciliation smoke test",
                    strategy_id="crypto_smoke_v1",
                ),
                quote,
            )
            time.sleep(max(1, args.wait_seconds))
            sell_status = broker.fetch_order(sell.order_id, symbol)
            account_after_sell = broker.get_account()
            raw_sell_filled = sell_status.get("filled")
            sell_filled = float(raw_sell_filled if raw_sell_filled is not None else 0.0)
            if sell_filled <= 0 and str(sell_status.get("status", "")).lower() in {"closed", "filled"}:
                sell_filled = float(sell_status.get("amount") or 0.0)
            position_qty_after_sell = (
                float(account_after_sell.positions[symbol].quantity)
                if symbol in account_after_sell.positions
                else 0.0
            )
            result["market_sell"] = {
                "order_id": sell.order_id,
                "status": sell_status.get("status"),
                "filled": sell_filled,
                "average": sell_status.get("average") or sell_status.get("price"),
                "position_quantity_after": position_qty_after_sell,
                "position_delta_vs_before": position_qty_after_sell - initial_position_qty,
            }
        result["reconciled_account"] = {"cash": account_after_sell.cash if buy_filled > 0 else account_after_buy.cash, "equity": account_after_sell.equity if buy_filled > 0 else account_after_buy.equity}
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


def command_halt(args):
    path = Path(args.state_dir) / "TRADING_HALTED"
    if args.resume:
        path.unlink(missing_ok=True)
        print("trading resumed")
    else:
        path.write_text("halted\n", encoding="utf-8")
        print("trading halted")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="stockquant-execution")
    parser.add_argument("--state-dir", default=str(STATE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("strategy-register")
    reg.add_argument("--name", required=True)
    reg.add_argument("--config", required=True)
    reg.add_argument("--stage", choices=["backtest", "paper", "small_live", "live", "retired"], default="backtest")
    reg.add_argument("--sharpe", type=float, default=0.0)
    reg.add_argument("--max-drawdown", type=float, default=0.0)
    reg.set_defaults(func=command_strategy_register)

    lst = sub.add_parser("strategy-list")
    lst.set_defaults(func=command_strategy_list)

    promote = sub.add_parser("strategy-promote")
    promote.add_argument("--version-id", required=True)
    promote.add_argument("--stage", required=True)
    promote.set_defaults(func=command_strategy_promote)

    rollback = sub.add_parser("strategy-rollback")
    rollback.set_defaults(func=command_strategy_rollback)

    paper = sub.add_parser("paper-run")
    paper.add_argument("--iterations", type=int, default=1)
    paper.add_argument("--interval", type=int, default=10)
    paper.add_argument("--live-quotes", action="store_true")
    paper.add_argument("--initial-cash", type=float, default=1_000_000.0)
    paper.add_argument("--commission-bps", type=float, default=3.0)
    paper.add_argument("--slippage-bps", type=float, default=5.0)
    paper.add_argument("--max-order-notional", type=float, default=20_000.0)
    paper.add_argument("--max-position-weight", type=float, default=0.10)
    paper.add_argument("--max-daily-loss", type=float, default=0.02)
    paper.add_argument("--max-drawdown", type=float, default=0.10)
    paper.add_argument("--max-quote-age-seconds", type=int, default=30)
    paper.set_defaults(func=command_paper_run)

    live = sub.add_parser("live-run")
    live.add_argument("--exchange", required=True)
    live.add_argument("--proxy", default=None)
    live.add_argument("--env-file", default="~/.config/stockquant/okx.env")
    live.add_argument("--enable-live", action="store_true")
    live.add_argument("--live", action="store_true")
    live.add_argument("--iterations", type=int, default=None)
    live.add_argument("--interval", type=int, default=5)
    live.add_argument("--max-order-notional", type=float, default=5_000.0)
    live.add_argument("--max-position-weight", type=float, default=0.05)
    live.add_argument("--max-daily-loss", type=float, default=0.01)
    live.add_argument("--max-drawdown", type=float, default=0.05)
    live.set_defaults(func=command_live_run)

    check = sub.add_parser("broker-check")
    check.add_argument("--exchange", default="okx")
    check.add_argument("--live", action="store_true")
    check.add_argument("--proxy", default=None)
    check.add_argument("--env-file", default="~/.config/stockquant/okx.env")
    check.set_defaults(func=command_broker_check)

    preflight = sub.add_parser("broker-preflight")
    preflight.add_argument("--live", action="store_true")
    preflight.add_argument("--env-file", default="~/.config/stockquant/okx.env")
    preflight.set_defaults(func=command_broker_preflight)

    crypto = sub.add_parser("crypto-monitor")
    crypto.add_argument("--exchange", default="okx")
    crypto.add_argument("--proxy", default=os.environ.get("STOCKQUANT_HTTP_PROXY", "http://127.0.0.1:7897"))
    crypto.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    crypto.add_argument("--interval", type=int, default=60)
    crypto.add_argument("--iterations", type=int, default=1)
    crypto.set_defaults(func=command_crypto_monitor)

    crypto_live = sub.add_parser("crypto-live-run")
    crypto_live.add_argument("--exchange", default="okx")
    crypto_live.add_argument("--proxy", default=None)
    crypto_live.add_argument("--env-file", default="~/.config/stockquant/okx.env")
    crypto_live.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    crypto_live.add_argument("--live", action="store_true")
    crypto_live.add_argument("--enable-live", action="store_true")
    crypto_live.add_argument("--dry-run", action="store_true")
    crypto_live.add_argument("--iterations", type=int, default=1)
    crypto_live.add_argument("--interval", type=int, default=60)
    crypto_live.add_argument("--target-weight", type=float, default=0.02)
    crypto_live.add_argument("--min-order-notional", type=float, default=10.0)
    crypto_live.add_argument("--buy-cooldown-seconds", type=int, default=3600)
    crypto_live.add_argument("--take-profit-fraction", type=float, default=0.5)
    crypto_live.add_argument("--max-order-notional", type=float, default=100.0)
    crypto_live.add_argument("--max-position-weight", type=float, default=0.05)
    crypto_live.add_argument("--max-daily-loss", type=float, default=0.01)
    crypto_live.add_argument("--max-drawdown", type=float, default=0.05)
    crypto_live.add_argument("--max-quote-age-seconds", type=int, default=90)
    crypto_live.add_argument("--max-spread-bps", type=float, default=30.0)
    crypto_live.set_defaults(func=command_crypto_live_run)

    crypto_smoke = sub.add_parser("crypto-smoke-test")
    crypto_smoke.add_argument("--exchange", default="okx")
    crypto_smoke.add_argument("--proxy", default=None)
    crypto_smoke.add_argument("--env-file", default="~/.config/stockquant/okx.env")
    crypto_smoke.add_argument("--symbol", default="BTC/USDT")
    crypto_smoke.add_argument("--live", action="store_true")
    crypto_smoke.add_argument("--enable-live", action="store_true")
    crypto_smoke.add_argument("--place-order", action="store_true")
    crypto_smoke.add_argument("--notional", type=float, default=20.0)
    crypto_smoke.add_argument("--market-notional", type=float, default=0.0)
    crypto_smoke.add_argument("--limit-offset-pct", type=float, default=0.02)
    crypto_smoke.add_argument("--wait-seconds", type=int, default=2)
    crypto_smoke.set_defaults(func=command_crypto_smoke_test)

    halt = sub.add_parser("halt")
    halt.add_argument("--resume", action="store_true")
    halt.set_defaults(func=command_halt)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        message = str(exc)
        if "50110" in message or "IP whitelist" in message:
            print(
                "OKX IP whitelist rejected the current proxy exit IP. "
                "Add the exit IP to the OKX Demo API whitelist, or clear the IP restriction for a demo-only key. "
                f"Raw error: {message}",
                file=sys.stderr,
            )
            return 2
        if "50101" in message or "does not match current environment" in message:
            print(
                "OKX API key environment mismatch: this key is a live/API-trading key, not a Demo Trading key (or vice versa). "
                "Create a separate API key inside OKX Demo Trading and replace the local config with that key. "
                f"Raw error: {message}",
                file=sys.stderr,
            )
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
