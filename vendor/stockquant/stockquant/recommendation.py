from __future__ import annotations

import math
import statistics
from typing import Dict, List, Tuple

from stockquant.backtest import BacktestConfig, BacktestResult
from stockquant.domain import MarketData
from stockquant.factors import FactorEngine


FACTOR_LABELS = {
    "value": "估值",
    "quality": "质量",
    "momentum": "动量",
    "low_vol": "低波动",
    "reversal": "短期反转",
    "liquidity": "流动性",
    "growth": "增长",
    "gross_margin": "毛利率",
    "forecast": "业绩预告",
    "holder": "股东集中度",
}


def _realized_vol(market: MarketData, symbol: str, date: str, lookback: int = 60) -> float:
    bars = market.history(symbol, date, lookback=lookback + 1)
    rets = []
    for a, b in zip(bars, bars[1:]):
        if a.close > 0 and b.close > 0:
            rets.append(b.close / a.close - 1.0)
    if len(rets) < 10:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(252.0)


def build_recommendations(
    market: MarketData,
    config: BacktestConfig,
    result: BacktestResult,
    top_n: int = 20,
) -> List[Dict[str, object]]:
    if not result.signal_history or not result.target_history:
        return []
    signal_date = max(result.signal_history)
    scores = result.signal_history[signal_date]
    target_weights = result.target_history.get(signal_date, {})
    engine = FactorEngine(
        factors=config.factors,
        min_history=config.min_history,
        sector_neutral=config.sector_neutral,
    )
    components, _ = engine.components(market, signal_date)
    symbols = [s for s in scores if s in target_weights]
    if not symbols:
        return []
    rows = []
    vols = {}
    for symbol in symbols:
        vols[symbol] = _realized_vol(market, symbol, signal_date)
    ordered_scores = sorted(scores[s] for s in symbols)
    ordered_vols = sorted(vols[s] for s in symbols)

    def percentile(values: List[float], value: float) -> float:
        if not values:
            return 0.5
        lo, hi = values[0], values[-1]
        if hi - lo <= 1e-12:
            return 0.5
        return max(0.0, min(1.0, (value - lo) / (hi - lo)))

    for symbol in symbols:
        factor_contrib = []
        valid = 0
        for spec in config.factors:
            z = components.get(spec.name, {}).get(symbol)
            if z is None:
                continue
            valid += 1
            factor_contrib.append((spec.name, spec.weight * float(z), float(z)))
        factor_contrib.sort(key=lambda x: x[1], reverse=True)
        score = scores[symbol]
        vol = vols[symbol]
        score_pct = percentile(ordered_scores, score)
        vol_pct = percentile(ordered_vols, vol)
        risk_adjusted = 0.7 * score_pct + 0.3 * (1.0 - vol_pct)
        confidence = max(0.05, min(0.95, 0.25 + 0.45 * abs(score) + 0.03 * valid))
        if risk_adjusted >= 0.72 and confidence >= 0.55:
            action = "重点关注"
        elif risk_adjusted >= 0.45:
            action = "观察"
        else:
            action = "谨慎"
        reasons = [
            f"{FACTOR_LABELS.get(name, name)}贡献"
            for name, contrib, _ in factor_contrib
            if contrib > 0
        ][:3]
        risks = []
        if vol >= 0.45:
            risks.append("高波动")
        if valid < 6:
            risks.append("因子覆盖不足")
        bar = market.bar(symbol, signal_date)
        if bar and bar.forecast_positive is not None and bar.forecast_positive < 0:
            risks.append("业绩预告偏负")
        rows.append(
            {
                "symbol": symbol,
                "score": score,
                "score_pct": score_pct,
                "risk_adjusted": risk_adjusted,
                "volatility": vol,
                "risk_pct": vol_pct,
                "confidence": confidence,
                "action": action,
                "target_weight": target_weights.get(symbol, 0.0),
                "reasons": reasons,
                "risks": risks,
                "valid_factors": valid,
                "signal_date": signal_date,
            }
        )
    rows.sort(key=lambda r: (float(r["risk_adjusted"]), float(r["score"])), reverse=True)
    return rows[:top_n]
