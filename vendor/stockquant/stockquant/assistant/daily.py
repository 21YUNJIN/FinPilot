from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from stockquant.backtest import BacktestConfig, BacktestEngine
from stockquant.domain import MarketData
from stockquant.portfolio import PortfolioConstraints, build_target_weights, volatility_scale
from stockquant.portfolio.risk import market_trend_scale
from stockquant.recommendation import build_recommendations
from stockquant.assistant.news import load_news_snapshot
from stockquant.assistant.portfolio import PortfolioLedger
from stockquant.factors import FactorEngine
from stockquant.rl import OfflineActionModel
from stockquant.assistant.watchlist import EntryAlertEngine
from stockquant.execution.versioning import StrategyRegistry


class DailyAssistant:
    def __init__(self, state_dir: str, project_root: str):
        self.state_dir = Path(state_dir)
        self.project_root = Path(project_root)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atr(market: MarketData, symbol: str, date: str, lookback: int = 14) -> float:
        bars = market.history(symbol, date, lookback=lookback + 1)
        values = []
        for prev, cur in zip(bars, bars[1:]):
            values.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        return statistics.mean(values) if values else 0.0

    @staticmethod
    def _realized_vol(market: MarketData, symbol: str, date: str, lookback: int = 60) -> float:
        bars = market.history(symbol, date, lookback=lookback + 1)
        rets = [b.close / a.close - 1.0 for a, b in zip(bars, bars[1:]) if a.close > 0 and b.close > 0]
        return statistics.pstdev(rets) * math.sqrt(252.0) if len(rets) > 10 else 0.0

    @staticmethod
    def _market_regime(market: MarketData, date: str, lookback: int = 200) -> Dict[str, object]:
        dates = [d for d in market.dates if d <= date]
        if len(dates) < 60:
            return {"regime": "unknown", "level": 0.0, "ma": 0.0}
        level = 100.0
        series = []
        prev = None
        for cur in dates:
            if prev is not None:
                a = market.snapshot(prev)
                b = market.snapshot(cur)
                rets = []
                for symbol, left in a.items():
                    right = b.get(symbol)
                    if right and left.close > 0 and right.close > 0 and not right.suspended:
                        rets.append(right.close / left.close - 1.0)
                if rets:
                    level *= 1.0 + sum(rets) / len(rets)
            series.append(level)
            prev = cur
        window = series[-lookback:] if len(series) >= lookback else series
        ma = sum(window) / len(window)
        if level > ma * 1.01:
            regime = "risk_on"
        elif level < ma * 0.99:
            regime = "risk_off"
        else:
            regime = "neutral"
        return {"regime": regime, "level": level, "ma": ma}

    @staticmethod
    def _current_recommendations(market: MarketData, config: BacktestConfig, result):
        latest_date = market.dates[-1]
        engine = FactorEngine(config.factors, min_history=config.min_history, sector_neutral=config.sector_neutral)
        scores, _ = engine.score(market, latest_date)
        snapshot = market.snapshot(latest_date)
        targets = build_target_weights(scores, snapshot, config.constraints)
        if config.alpha_weight < 1.0 and snapshot:
            index_constraints = PortfolioConstraints(
                max_positions=len(snapshot), max_weight=0.08, sector_max_weight=1.0,
                cash_buffer=config.constraints.cash_buffer, min_score=-1e9, weight_scheme="equal",
            )
            base = build_target_weights({s: 1.0 for s in snapshot}, snapshot, index_constraints)
            alpha_w = max(0.0, min(1.0, config.alpha_weight))
            symbols = set(targets) | set(base)
            targets = {s: alpha_w * targets.get(s, 0.0) + (1.0 - alpha_w) * base.get(s, 0.0) for s in symbols}
        scale = volatility_scale(market, latest_date, config.target_volatility, lookback=20)
        if config.market_filter == "ma":
            scale *= market_trend_scale(market, latest_date, config.market_filter_lookback, config.risk_off_exposure)
        targets = {s: w * scale for s, w in targets.items()}
        proxy = SimpleNamespace(signal_history={latest_date: scores}, target_history={latest_date: targets})
        return build_recommendations(market, config, proxy, top_n=20)

    def generate(self, market: MarketData, config: BacktestConfig, ledger: PortfolioLedger) -> Dict[str, object]:
        result = BacktestEngine(market, config).run()
        recommendations = self._current_recommendations(market, config, result)
        news = load_news_snapshot(str(self.state_dir / "news_snapshot.json"))
        portfolio = ledger.mark_to_market(market)
        holdings = {row["symbol"]: row for row in portfolio.get("position_rows", [])}
        latest_market_date = market.dates[-1]
        signal_date = recommendations[0]["signal_date"] if recommendations else latest_market_date
        rl_model = None
        rl_path = self.state_dir / "rl_model.json"
        if rl_path.exists():
            try:
                rl_model = OfflineActionModel.load(str(rl_path))
            except Exception:
                rl_model = None
        components = {}
        if rl_model and signal_date:
            components, _ = FactorEngine(config.factors, min_history=config.min_history, sector_neutral=config.sector_neutral).components(market, signal_date)
        regime = self._market_regime(market, signal_date) if signal_date else {"regime": "unknown", "level": 0.0, "ma": 0.0}
        stale_days = (date.today() - date.fromisoformat(latest_market_date)).days if latest_market_date else 9999
        data_stale = stale_days > 5
        stock_news = {}
        for item in news.get("stock", []):
            symbol = str(item.get("symbol") or "")
            stock_news.setdefault(symbol, []).append(item)
        plans = []
        for rec in recommendations[:12]:
            symbol = str(rec["symbol"])
            bar = market.bar(symbol, signal_date)
            if bar is None:
                continue
            atr = self._atr(market, symbol, signal_date)
            # ATR is measured on the (adjusted) series; the entry/stop/target
            # levels must be expressed in tradeable raw prices, otherwise a
            # user copying them would trade at the wrong scale.
            ref_px = bar.raw_close if bar.raw_close > 0 else bar.close
            atr_ratio = (atr / bar.close) if bar.close > 0 else 0.0
            atr_raw = atr_ratio * ref_px
            entry_mid = ref_px
            entry_low = max(0.01, entry_mid - 0.5 * atr_raw)
            entry_high = entry_mid + 0.5 * atr_raw
            stop = max(0.01, entry_mid - 2.0 * atr_raw)
            target = entry_mid + 3.0 * atr_raw
            news_items = stock_news.get(symbol, [])
            negative_news = [x for x in news_items if float(x.get("sentiment", 0)) < 0 and bool(x.get("direct_match", False))]
            positive_news = [x for x in news_items if float(x.get("sentiment", 0)) > 0 and bool(x.get("direct_match", False))]
            related_news = [x for x in news_items if not bool(x.get("direct_match", False))]
            risk_adjusted = float(rec["risk_adjusted"])
            action = str(rec["action"])
            if symbol in holdings and holdings[symbol].get("pnl", 0) > 0 and risk_adjusted >= 0.65:
                action = "持有/可加仓"
            elif symbol in holdings and risk_adjusted < 0.45:
                action = "减仓/退出观察"
            elif symbol not in holdings:
                action = "等待触发买入"
            if negative_news and action == "等待触发买入":
                action = "暂缓买入"
            if regime["regime"] == "risk_off" and action == "等待触发买入":
                action = "等待市场转强"
            if data_stale:
                action = "数据过期·禁止执行"
            rl_action = None
            if rl_model and signal_date:
                feature_map = {name: components.get(name, {}).get(symbol, 0.0) for name in [f.name for f in config.factors]}
                feature_map["volatility"] = rec["volatility"]
                feature_map["score"] = rec["score"]
                rl_action = rl_model.predict(feature_map)
            plan = {
                "symbol": symbol,
                "signal_date": signal_date,
                "action": action,
                "model_score": rec["score"],
                "risk_adjusted": risk_adjusted,
                "confidence": rec["confidence"],
                "annualized_volatility": rec["volatility"],
                "suggested_weight": rec["target_weight"],
                "entry_zone": [entry_low, entry_high],
                "stop_loss": stop,
                "take_profit": target,
                "buy_window": "09:45-14:30；避开开盘前5分钟和14:55后",
                "trim_condition": "跌破止损、因子分数跌出前20%、或出现重大负面公告",
                "add_condition": "已有盈利缓冲、逻辑未失效、组合风险预算允许；禁止用加仓摊薄亏损",
                "reasons": rec.get("reasons", []),
                "risks": rec.get("risks", []) + (["直接负面新闻"] if negative_news else []),
                "positive_news": [x.get("title") for x in positive_news[:3]],
                "negative_news": [x.get("title") for x in negative_news[:3]],
                "related_news": [x.get("title") for x in related_news[:3]],
                "holding": holdings.get(symbol),
                "rl_action": rl_action,
                "paper_only": True,
            }
            plans.append(plan)
        # Real (externally recorded) holdings only - the paper portfolio is a
        # separate ledger and must not be presented as the user's positions.
        holding_rows = portfolio.get("external_rows", [])
        rec_map = {str(x["symbol"]): x for x in recommendations}
        current_engine = FactorEngine(config.factors, min_history=config.min_history, sector_neutral=config.sector_neutral)
        current_scores, _ = current_engine.score(market, latest_market_date)
        external_total = sum(float(x.get("market_value", 0.0)) for x in holding_rows)
        holdings_analysis = []
        for holding in holding_rows:
            symbol = str(holding.get("symbol"))
            bar = market.bar(symbol, latest_market_date)
            mark_px = (bar.raw_close if bar and bar.raw_close > 0 else bar.close) if bar else float(holding.get("price", 0.0))
            cost = float(holding.get("average_cost", 0.0))
            shares = int(holding.get("shares", 0))
            pnl_pct = (mark_px / cost - 1.0) if cost > 0 else 0.0
            atr = self._atr(market, symbol, latest_market_date)
            atr_ratio = atr / bar.close if bar and bar.close > 0 else 0.0
            score = current_scores.get(symbol)
            vol = self._realized_vol(market, symbol, latest_market_date)
            news_items = stock_news.get(symbol, [])
            direct_negative = [x for x in news_items if float(x.get("sentiment", 0)) < 0 and bool(x.get("direct_match", False))]
            if mark_px <= cost * (1.0 - 2.0 * atr_ratio):
                action = "止损/减仓"
                reason = "价格触及 ATR 止损线"
            elif mark_px >= cost * (1.0 + 3.0 * atr_ratio):
                action = "止盈/分批减仓"
                reason = "价格触及 ATR 止盈线"
            elif direct_negative:
                action = "风险复核/减仓"
                reason = "出现直接负面新闻"
            elif score is None:
                action = "持有观察"
                reason = "因子数据不足"
            elif score > 0.5 and pnl_pct > 0 and vol < 0.45:
                action = "持有/可加仓"
                reason = "因子和盈利缓冲仍在，风险可接受"
            elif score < -0.5:
                action = "减仓"
                reason = "最新模型分数转负"
            else:
                action = "持有观察"
                reason = "信号中性"
            holdings_analysis.append({
                "symbol": symbol,
                "shares": shares,
                "average_cost": cost,
                "price": mark_px,
                "pnl_pct": pnl_pct,
                "market_value": float(holding.get("market_value", 0.0)),
                "portfolio_weight": (float(holding.get("market_value", 0.0)) / external_total) if external_total > 0 else 0.0,
                "action": action,
                "reason": reason,
                "score": score,
                "volatility": vol,
                "stop_loss": max(0.01, cost * (1.0 - 2.0 * atr_ratio)) if cost > 0 else 0.0,
                "take_profit": cost * (1.0 + 3.0 * atr_ratio) if cost > 0 else 0.0,
                "direct_negative_news": [x.get("title") for x in direct_negative[:3]],
                "recommendation": rec_map.get(symbol),
            })
        payload = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "signal_date": signal_date,
            "market_regime": regime,
            "data_freshness": {"latest_trading_date": latest_market_date, "signal_date": signal_date, "stale_calendar_days": stale_days, "stale": data_stale},
            "portfolio": portfolio,
            "plans": plans,
            "holdings_analysis": holdings_analysis,
            "risk_events": news.get("risk_events", [])[:10],
            "disclaimer": "Research decision support only; no guaranteed returns and no broker execution.",
        }
        payload["entry_alerts"] = EntryAlertEngine(str(self.state_dir)).generate(market, config, payload)
        version_config = {
            "rebalance": config.rebalance,
            "alpha_weight": config.alpha_weight,
            "target_volatility": config.target_volatility,
            "market_filter": config.market_filter,
            "risk_off_exposure": config.risk_off_exposure,
            "rebalance_band": config.rebalance_band,
            "constraints": config.constraints.__dict__,
            "factors": {f.name: f.weight for f in config.factors},
        }
        registry = StrategyRegistry(str(self.state_dir / "strategy_registry.json"))
        version = registry.register(
            "forecast_quality_daily_plan",
            version_config,
            {"sharpe": result.metrics.get("sharpe", 0.0), "max_drawdown": result.metrics.get("max_drawdown", 0.0), "deflated_sharpe": result.metrics.get("deflated_sharpe_ratio", 0.0)},
            stage="paper",
        )
        payload["strategy_version"] = version
        return payload


def save_daily_plan(plan: Dict[str, object], state_dir: str) -> None:
    base = Path(state_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "daily_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (base / "daily_plan_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(plan, ensure_ascii=False, default=str) + "\n")
