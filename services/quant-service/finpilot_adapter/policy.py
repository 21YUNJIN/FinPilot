"""What FinPilot may do when the quant service is unavailable.

The SRS distinguishes two cases and so does this policy:

* metrics only decorate a comparison          -> show "NA" and keep the bank's
  own terms/fees/liquidity ordering;
* metrics are a hard risk condition            -> stop the affected decision.

The policy never turns "no data" into "low risk".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional


@dataclass
class RiskGate:
    require_metrics_for_ranking: bool = False

    def decide(self, report: Optional[Mapping[str, Any]]) -> dict:
        if report is None:
            if self.require_metrics_for_ranking:
                return {
                    "allowed": False,
                    "mode": "block",
                    "reason": "QUANT_UNAVAILABLE_HARD_GATE",
                    "user_message": "风险数据暂不可用，暂不能完成该产品的申购比较。",
                }
            return {
                "allowed": True,
                "mode": "degrade",
                "reason": "QUANT_UNAVAILABLE_OPTIONAL",
                "user_message": "风险指标暂不可用，已按产品条款、费用和流动性展示，风险指标标为 NA。",
            }
        if not report.get("usable_for_ranking"):
            return {
                "allowed": not self.require_metrics_for_ranking,
                "mode": "degrade",
                "reason": "INSUFFICIENT_NAV_HISTORY",
                "user_message": "该产品历史净值不足，无法给出可比较的风险指标，已明确标记为 NA。",
            }
        return {"allowed": True, "mode": "full", "reason": "OK", "user_message": ""}

    def annotate(self, reports: List[Mapping[str, Any]]) -> List[dict]:
        return [
            {
                "product_id": report.get("product_id"),
                "decision": self.decide(report),
                "missing_metrics": [
                    name for name, item in (report.get("metrics") or {}).items()
                    if isinstance(item, Mapping) and item.get("value") is None
                ],
            }
            for report in reports
        ]