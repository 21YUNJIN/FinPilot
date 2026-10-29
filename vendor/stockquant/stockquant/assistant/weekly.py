from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from stockquant.domain import MarketData


class WeeklyReview:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)

    def generate(self, market: MarketData) -> Dict[str, object]:
        history_path = self.state_dir / "daily_plan_history.jsonl"
        plans: List[Dict[str, object]] = []
        if history_path.exists():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                try:
                    plans.append(json.loads(line))
                except Exception:
                    continue
        latest_dates = market.dates[-5:] if len(market.dates) >= 5 else market.dates
        evaluations = []
        for snapshot in plans[-10:]:
            signal_date = str(snapshot.get("signal_date") or "")
            if not signal_date or signal_date not in market.dates:
                continue
            end_date = latest_dates[-1]
            if signal_date >= end_date:
                continue
            for plan in snapshot.get("plans", []):
                symbol = str(plan.get("symbol"))
                a = market.bar(symbol, signal_date)
                b = market.bar(symbol, end_date)
                if a is None or b is None or a.close <= 0:
                    continue
                ret = b.close / a.close - 1.0
                positive_call = str(plan.get("action", "")) in {"等待触发买入", "持有/可加仓", "重点关注"}
                evaluations.append(
                    {
                        "plan_date": signal_date,
                        "end_date": end_date,
                        "symbol": symbol,
                        "action": plan.get("action"),
                        "return": ret,
                        "risk_adjusted": plan.get("risk_adjusted"),
                        "correct_direction": (ret > 0) if positive_call else (ret <= 0),
                    }
                )
        hit_rate = (
            sum(1 for row in evaluations if row["correct_direction"]) / len(evaluations)
            if evaluations
            else 0.0
        )
        avg_return = sum(float(row["return"]) for row in evaluations) / len(evaluations) if evaluations else 0.0
        errors = sorted([row for row in evaluations if not row["correct_direction"]], key=lambda x: x["return"])
        strengths = sorted([row for row in evaluations if row["correct_direction"]], key=lambda x: x["return"], reverse=True)
        recommendations = []
        if errors:
            recommendations.append("复核错误股票在计划日之后是否出现财务、公告或行业事件，区分模型错误和不可预测事件。")
        if avg_return < 0:
            recommendations.append("降低主动权重或提高现金缓冲，等待因子 IC 恢复。")
        recommendations.extend(
            [
                "检查新增业绩预告和股东户数是否按期进入特征库。",
                "若滚动 IC 连续两周为负，保持 champion 不变，不自动提升 challenger。",
                "将高波动和负面新闻标的加入下一周风险观察名单。",
            ]
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "evaluated": len(evaluations),
            "hit_rate": hit_rate,
            "average_return": avg_return,
            "best": strengths[:10],
            "worst": errors[:10],
            "reflections": recommendations,
            "status": "research_only",
        }
        (self.state_dir / "weekly_review.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        markdown = [
            "# Weekly Quant Review",
            "",
            f"- Evaluated plans: {len(evaluations)}",
            f"- Direction hit rate: {hit_rate:.2%}",
            f"- Average return: {avg_return:.2%}",
            "",
            "## Reflections",
        ]
        markdown.extend(f"- {item}" for item in recommendations)
        (self.state_dir / "weekly_review.md").write_text("\n".join(markdown), encoding="utf-8")
        return payload
