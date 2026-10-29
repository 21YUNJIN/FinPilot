"""Natural-language query layer over the read-only StockQuant Quant API.

The LLM is a router/explainer.  It never calculates financial numbers, never
reads arbitrary files and never reaches the trading or strategy-mutation paths.
Tool results are deterministic JSON from :mod:`stockquant.quant_api`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from stockquant.assistant.llm import (
    LLMCallLogger,
    LLMClient,
    LLMConfig,
    LLMResponse,
    MockLLMClient,
    ToolCall,
    build_llm_client,
)
from stockquant.domain import MarketData
from stockquant.quant_api import factor_snapshot, portfolio_risk, stress_test


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "factor_snapshot",
            "description": "Read-only factor z-scores and composite score for one stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A-share code, e.g. 000001.SZ"},
                    "as_of": {"type": "string", "description": "YYYY-MM-DD; omit for latest"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "portfolio_risk",
            "description": "Read-only market value, weights and annualized volatility for positions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "positions": {"type": "object", "description": "symbol -> shares"},
                    "as_of": {"type": "string", "description": "YYYY-MM-DD; omit for latest"},
                },
                "required": ["positions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stress_test",
            "description": "Read-only first-order market/symbol shocks and volatility multiplier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "positions": {"type": "object", "description": "symbol -> shares"},
                    "as_of": {"type": "string", "description": "YYYY-MM-DD; omit for latest"},
                    "scenarios": {"type": "object", "description": "scenario name -> shock spec"},
                },
                "required": ["positions"],
            },
        },
    },
]

_SYMBOL_RE = re.compile(r"\b(\d{6}(?:\.(?:SH|SZ))?)\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


class QuantToolbox:
    def __init__(self, market: MarketData, dataset: str, default_as_of: str) -> None:
        self.market = market
        self.dataset = dataset
        self.default_as_of = default_as_of

    def execute(self, name: str, args: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            if name == "factor_snapshot":
                result = factor_snapshot(self.market, str(args.get("symbol") or ""),
                                         str(args.get("as_of") or self.default_as_of))
            elif name == "portfolio_risk":
                result = portfolio_risk(self.market, args.get("positions") or {},
                                        str(args.get("as_of") or self.default_as_of))
            elif name == "stress_test":
                result = stress_test(self.market, args.get("positions") or {},
                                     str(args.get("as_of") or self.default_as_of),
                                     args.get("scenarios"))
            else:
                return {"ok": False, "error": f"TOOL_NOT_ALLOWED: {name}"}
            return {"ok": True, "result": result}
        except Exception as exc:  # convert tool errors into data, not hidden exceptions
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class QuantAssistant:
    def __init__(self, market: MarketData, dataset: str, default_as_of: str,
                 llm: Optional[LLMClient] = None, log_path: Optional[str] = None) -> None:
        self.market = market
        self.dataset = dataset
        self.default_as_of = default_as_of
        self.toolbox = QuantToolbox(market, dataset, default_as_of)
        self.llm = llm or build_llm_client()
        self.call_logger = LLMCallLogger(log_path) if log_path else None

    def _fallback(self, question: str, as_of: Optional[str],
                  positions: Optional[Mapping[str, float]], *,
                  history: Optional[Sequence[Mapping[str, str]]] = None,
                  degraded: bool = False,
                  warning: Optional[str] = None) -> Dict[str, Any]:
        history_text = " ".join(str(item.get("content") or "") for item in (history or [])[-6:])
        lookup_text = question + " " + history_text
        date = str(as_of or _first_match(_DATE_RE, lookup_text) or self.default_as_of)
        calls: List[Dict[str, Any]] = []
        symbol_match = _first_match(_SYMBOL_RE, lookup_text)
        if symbol_match:
            calls.append({"name": "factor_snapshot",
                          "args": {"symbol": symbol_match, "as_of": date}})
        if positions:
            calls.append({"name": "portfolio_risk",
                          "args": {"positions": dict(positions), "as_of": date}})
            if any(word in question for word in ("风险", "压力", "下跌", "情景", "stress")):
                calls.append({"name": "stress_test",
                              "args": {"positions": dict(positions), "as_of": date}})
        tool_results = []
        for call in calls:
            result = self.toolbox.execute(call["name"], call["args"])
            tool_results.append({"tool": call["name"], "args": call["args"], "result": result})
        if not tool_results:
            return {
                "mode": "deterministic",
                "answer": "请提供 6 位股票代码，或提供 positions（代码→股数）后询问因子/组合风险。",
                "tool_results": [],
                "needs_input": True,
                "degraded": degraded,
                "warnings": [warning] if warning else [],
            }
        lines = []
        for item in tool_results:
            name = item["tool"]
            result = item["result"]
            if not result.get("ok"):
                lines.append(f"{name}: 失败（{result.get('error')}）")
                continue
            data = result["result"]
            if name == "factor_snapshot":
                lines.append(f"{data['symbol']} {data['as_of']} 综合因子分 {data['composite_score']:+.4f}")
            elif name == "portfolio_risk":
                lines.append(f"组合总市值 {data['total_value']:,.0f}，年化波动率 "
                             f"{_fmt_pct(data.get('annualized_volatility'))}")
            elif name == "stress_test":
                parts = [f"{x['name']} {_fmt_pct(x.get('pnl_pct'))}" for x in data.get("scenarios", [])]
                lines.append("压力测试：" + "；".join(parts))
        return {
            "mode": "deterministic",
            "answer": "\n".join(lines) + "\n（研究结果，不构成投资建议）",
            "tool_results": tool_results,
            "needs_input": False,
            "degraded": degraded,
            "warnings": [warning] if warning else [],
            "dataset": self.dataset,
        }

    def _finish(self, result: Dict[str, Any], *, history: Optional[Sequence[Mapping[str, str]]],
                question: str, usage: Optional[Mapping[str, int]] = None,
                cost: Optional[float] = None, llm_meta: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        prior = [{"role": str(item.get("role")), "content": str(item.get("content"))}
                 for item in (history or [])[-6:]
                 if item.get("role") in {"user", "assistant"} and item.get("content")]
        prior.extend([
            {"role": "user", "content": question},
            {"role": "assistant", "content": str(result.get("answer") or "")},
        ])
        result["history"] = prior[-8:]
        result["usage"] = dict(usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        result["estimated_cost_usd"] = cost
        result["llm"] = dict(llm_meta or {})
        return result

    def ask(self, question: str, *, as_of: Optional[str] = None,
            positions: Optional[Mapping[str, float]] = None,
            history: Optional[Sequence[Mapping[str, str]]] = None) -> Dict[str, Any]:
        question = str(question or "").strip()
        if not question:
            raise ValueError("question cannot be empty")
        if isinstance(self.llm, MockLLMClient) and self.llm.responder is None:
            result = self._fallback(question, as_of, positions, history=history)
            return self._finish(result, history=history, question=question,
                                llm_meta={"provider": "mock", "model": "mock", "duration_ms": 0})

        date = str(as_of or _first_match(_DATE_RE, question) or self.default_as_of)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": (
                "你是 StockQuant 的只读量化解释器。只能调用提供的工具；"
                "不要自行计算或编造数字；工具结果就是唯一数据来源。"
                "回答简洁，区分事实和风险，不做投资建议。"
            )},
        ]
        for item in (history or [])[-6:]:
            if item.get("role") in {"user", "assistant"} and item.get("content"):
                messages.append({"role": str(item["role"]), "content": str(item["content"])})
        messages.append({"role": "user", "content": (
            f"数据集: {self.dataset}\n分析日: {date}\n"
            f"持仓: {json.dumps(dict(positions or {}), ensure_ascii=False)}\n"
            f"问题: {question}"
        )})
        tool_results: List[Dict[str, Any]] = []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cost_total = 0.0
        llm_meta: Dict[str, Any] = {}
        for _ in range(3):
            response: LLMResponse = self.llm.complete(messages, tools=TOOL_SPECS)
            if self.call_logger:
                self.call_logger.record(response, question=question,
                                        tool_names=[call.name for call in response.tool_calls])
            for key in usage_total:
                usage_total[key] += int((response.usage or {}).get(key) or 0)
            if response.estimated_cost_usd is not None:
                cost_total += float(response.estimated_cost_usd)
            llm_meta = {"provider": response.provider, "model": response.model,
                        "duration_ms": response.duration_ms, "error_code": response.error_code}
            if not response.ok:
                result = self._fallback(question, as_of, positions, history=history, degraded=True,
                                        warning=f"LLM {response.error_code}: {response.error_message}")
                return self._finish(result, history=history, question=question,
                                    usage=usage_total, cost=cost_total or None, llm_meta=llm_meta)
            if response.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": response.text or None,
                    "tool_calls": [call.to_openai() for call in response.tool_calls],
                })
                for call in response.tool_calls:
                    result = self.toolbox.execute(call.name, call.arguments)
                    tool_results.append({"tool": call.name, "args": call.arguments, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })
                continue
            if response.text.strip():
                quantitative = bool(_SYMBOL_RE.search(question) or positions)
                if quantitative and not tool_results:
                    result = self._fallback(question, as_of, positions, history=history, degraded=True,
                                            warning="LLM answered without calling a deterministic tool; fallback used")
                    return self._finish(result, history=history, question=question,
                                        usage=usage_total, cost=cost_total or None, llm_meta=llm_meta)
                result = {
                    "mode": "llm",
                    "answer": response.text.strip(),
                    "tool_results": tool_results,
                    "needs_input": False,
                    "degraded": False,
                    "warnings": [],
                    "dataset": self.dataset,
                }
                return self._finish(result, history=history, question=question,
                                    usage=usage_total, cost=cost_total or None, llm_meta=llm_meta)
        result = self._fallback(question, as_of, positions, history=history, degraded=True,
                                warning="LLM returned no usable text or tool call")
        return self._finish(result, history=history, question=question,
                            usage=usage_total, cost=cost_total or None, llm_meta=llm_meta)

    def status(self) -> Dict[str, Any]:
        return {
            "llm_class": type(self.llm).__name__,
            "mode": "mock" if isinstance(self.llm, MockLLMClient) else "provider",
            "tools": [item["function"]["name"] for item in TOOL_SPECS],
            "read_only": True,
        }


def _first_match(pattern: re.Pattern, text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(1) if match else None


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value)*100:+.2f}%"
    except Exception:
        return "—"


__all__ = ["TOOL_SPECS", "QuantToolbox", "QuantAssistant"]
