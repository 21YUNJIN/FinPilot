from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from stockquant.assistant.daily import DailyAssistant, save_daily_plan
from stockquant.assistant.news import NewsEngine
from stockquant.assistant.portfolio import PortfolioLedger
from stockquant.assistant.weekly import WeeklyReview
from stockquant.assistant.watchlist import WatchlistStore
from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.data import load_csv, load_membership_csv
from stockquant.factors import DEFAULT_FACTORS
from stockquant.knowledge import LearningEngine
from stockquant.live import update_market_dataset
from stockquant.portfolio import PortfolioConstraints
from stockquant.recommendation import build_recommendations
from stockquant.rl import train_offline_action_model
from stockquant.walkforward import CANDIDATES, with_weights


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / "data" / "state"
PREFERRED_DATA_PATH = PROJECT_ROOT / "data" / "real" / "csi300_hist_akshare_2018_2026.csv"
LEGACY_DATA_PATH = PROJECT_ROOT / "data" / "real" / "qlib_csi300_hist80_v3_clean_2018_2025.csv"
DATA_PATH = PREFERRED_DATA_PATH if PREFERRED_DATA_PATH.exists() else LEGACY_DATA_PATH
MEMBERSHIP_PATH = PROJECT_ROOT / "data" / "real" / "dolt_csi300_membership_2018_2025.csv"


def load_market():
    path = PREFERRED_DATA_PATH if PREFERRED_DATA_PATH.exists() else LEGACY_DATA_PATH
    market = load_csv(str(path))
    if MEMBERSHIP_PATH.exists():
        market.membership_by_date = load_membership_csv(str(MEMBERSHIP_PATH))
    return market


def base_config() -> BacktestConfig:
    preset = "forecast_quality"
    return BacktestConfig(
        initial_cash=1_000_000.0,
        commission_bps=3.0,
        stamp_duty_bps=5.0,
        slippage_bps=5.0,
        lot_size=100,
        rebalance="quarterly",
        start="2018-07-01",
        end=date.today().isoformat(),
        min_history=100,
        sector_neutral=True,
        target_volatility=0.18,
        alpha_weight=0.5,
        rebalance_band=0.02,
        min_trade_weight=0.005,
        constraints=PortfolioConstraints(
            max_positions=12,
            max_weight=0.10,
            sector_max_weight=0.30,
            cash_buffer=0.03,
            weight_scheme="score",
        ),
        factors=with_weights(DEFAULT_FACTORS, CANDIDATES[preset]),
    )


def command_daily(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    if not args.skip_update:
        try:
            update_market_dataset(
                str(DATA_PATH),
                str(MEMBERSHIP_PATH),
                end_date=date.today().isoformat(),
                symbols_file=str(PROJECT_ROOT / "data" / "real" / "dolt_sample80_symbols.txt"),
                workers=1,
            )
        except Exception as exc:
            print(f"live update warning: {type(exc).__name__}: {exc}")
    market = load_market()
    config = base_config()
    ledger = PortfolioLedger(str(state_dir / "portfolio.json"))
    if args.refresh_news:
        result = BacktestEngine(market, config).run()
        recs = build_recommendations(market, config, result, top_n=20)
        holdings = list(ledger.load().get("positions", {}).keys())
        symbols = sorted(set(holdings) | {str(x["symbol"]) for x in recs[:20]})
        NewsEngine(str(state_dir)).snapshot(symbols, global_limit=80, stock_news_per_symbol=8)
    plan = DailyAssistant(str(state_dir), str(PROJECT_ROOT)).generate(market, config, ledger)
    save_daily_plan(plan, str(state_dir))
    print(f"daily plan: {state_dir / 'daily_plan.json'}")
    print(f"signal date: {plan.get('signal_date')}")
    print(f"plans: {len(plan.get('plans', []))}")
    return 0


def command_news(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    market = load_market()
    config = base_config()
    result = BacktestEngine(market, config).run()
    recs = build_recommendations(market, config, result, top_n=20)
    ledger = PortfolioLedger(str(state_dir / "portfolio.json"))
    symbols = sorted(set(ledger.load().get("positions", {}).keys()) | {str(x["symbol"]) for x in recs[:20]})
    snapshot = NewsEngine(str(state_dir)).snapshot(symbols, global_limit=args.global_limit, stock_news_per_symbol=args.per_symbol)
    print(f"news snapshot: {state_dir / 'news_snapshot.json'}")
    print(snapshot.get("counts", {}))
    return 0


def command_train_rl(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    market = load_market()
    model = train_offline_action_model(market, base_config(), horizon=args.horizon, ridge_lambda=args.ridge_lambda)
    model.save(str(state_dir / "rl_model.json"))
    print(f"rl model: {state_dir / 'rl_model.json'}")
    print(f"features: {model.feature_names}")
    return 0


def command_weekly(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    market = load_market()
    review = WeeklyReview(str(state_dir)).generate(market)
    update = LearningEngine(str(state_dir)).weekly_update(review)
    print(f"weekly review: {state_dir / 'weekly_review.json'}")
    print(f"knowledge update: {state_dir / 'knowledge_update.json'}")
    print(f"evaluated: {review.get('evaluated')} hit_rate: {review.get('hit_rate')}")
    print(f"hypotheses: {len(update.get('hypotheses', []))}")
    return 0


def command_watch_add(args: argparse.Namespace) -> int:
    payload = WatchlistStore(str(Path(args.state_dir))).add(args.symbol, note=args.note)
    print(payload)
    return 0


def command_watch_remove(args: argparse.Namespace) -> int:
    payload = WatchlistStore(str(Path(args.state_dir))).remove(args.symbol)
    print(payload)
    return 0


def command_watch_list(args: argparse.Namespace) -> int:
    payload = WatchlistStore(str(Path(args.state_dir))).load()
    print(payload)
    return 0


def command_holding(args: argparse.Namespace) -> int:
    ledger = PortfolioLedger(str(Path(args.state_dir) / "portfolio.json"))
    payload = ledger.set_holding(args.symbol, args.shares, args.cost, note=args.note)
    print(f"cash: {payload['cash']:.2f}")
    print(f"external_positions: {payload.get('external_positions', {})}")
    return 0


def command_trade(args: argparse.Namespace) -> int:
    ledger = PortfolioLedger(str(Path(args.state_dir) / "portfolio.json"))
    payload = ledger.trade(args.symbol, args.side, args.shares, args.price, reason=args.reason)
    print(f"cash: {payload['cash']:.2f}")
    print(f"positions: {payload['positions']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stockquant-assistant")
    parser.add_argument("--state-dir", default=str(STATE_DIR))
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily")
    daily.add_argument("--refresh-news", action="store_true", default=True)
    daily.add_argument("--skip-update", action="store_true")
    daily.set_defaults(func=command_daily)

    news = sub.add_parser("news")
    news.add_argument("--global-limit", type=int, default=80)
    news.add_argument("--per-symbol", type=int, default=8)
    news.set_defaults(func=command_news)

    weekly = sub.add_parser("weekly")
    weekly.set_defaults(func=command_weekly)

    rl = sub.add_parser("train-rl")
    rl.add_argument("--horizon", type=int, default=20)
    rl.add_argument("--ridge-lambda", type=float, default=20.0)
    rl.set_defaults(func=command_train_rl)

    watch_add = sub.add_parser("watch-add")
    watch_add.add_argument("--symbol", required=True)
    watch_add.add_argument("--note", default="")
    watch_add.set_defaults(func=command_watch_add)

    watch_remove = sub.add_parser("watch-remove")
    watch_remove.add_argument("--symbol", required=True)
    watch_remove.set_defaults(func=command_watch_remove)

    watch_list = sub.add_parser("watch-list")
    watch_list.set_defaults(func=command_watch_list)

    holding = sub.add_parser("holding")
    holding.add_argument("--symbol", required=True)
    holding.add_argument("--shares", type=int, required=True, help="0 removes the holding")
    holding.add_argument("--cost", type=float, required=True)
    holding.add_argument("--note", default="manual_actual_holding")
    holding.set_defaults(func=command_holding)

    trade = sub.add_parser("trade")
    trade.add_argument("--symbol", required=True)
    trade.add_argument("--side", choices=["buy", "sell"], required=True)
    trade.add_argument("--shares", type=int, required=True)
    trade.add_argument("--price", type=float, required=True)
    trade.add_argument("--reason", default="paper_manual")
    trade.set_defaults(func=command_trade)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
