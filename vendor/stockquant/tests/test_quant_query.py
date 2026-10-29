"""Natural-language quant query layer: deterministic fallback and tool loop."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from stockquant import dashboard as dash
from stockquant.assistant.llm import LLMResponse, MockLLMClient, ToolCall
from stockquant.assistant.quant_query import QuantAssistant
from stockquant.domain import Bar, MarketData


def make_market(days=180):
    rows = []
    prices = {"000001.SZ": 12.0, "600000.SH": 9.0}
    for i in range(days):
        month = 1 + i // 28
        day = 1 + i % 28
        date = f"2024-{month:02d}-{day:02d}" if month <= 12 else f"2025-{month-12:02d}-{day:02d}"
        for symbol, base in prices.items():
            px = base + 0.01 * i + 0.04 * math.sin(i / 5.0)
            rows.append(Bar(date=date, symbol=symbol, open=px, high=px * 1.01,
                            low=px * 0.99, close=px, volume=1_000_000 + i * 1000,
                            market_cap=1e10))
    return MarketData.from_rows([b.__dict__ for b in rows])


class QuantQueryTests(unittest.TestCase):
    def setUp(self):
        self.market = make_market()
        self.as_of = self.market.dates[-1]

    def test_default_mock_uses_deterministic_factor_path(self):
        assistant = QuantAssistant(self.market, "csi300_full", self.as_of)
        out = assistant.ask("000001.SZ 的因子分怎么样")
        self.assertEqual(out["mode"], "deterministic")
        self.assertIn("综合因子分", out["answer"])
        self.assertEqual(out["tool_results"][0]["tool"], "factor_snapshot")

    def test_positions_trigger_risk_and_stress_tools(self):
        assistant = QuantAssistant(self.market, "csi300_full", self.as_of)
        out = assistant.ask(
            "这个组合的风险和下跌压力怎么样",
            positions={"000001": 100, "600000": 100},
        )
        self.assertEqual([x["tool"] for x in out["tool_results"]],
                         ["portfolio_risk", "stress_test"])
        self.assertIn("压力测试", out["answer"])

    def test_llm_tool_loop_executes_whitelisted_tool_then_answers(self):
        calls = {"n": 0}

        def responder(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return LLMResponse(tool_calls=[ToolCall("call-1", "factor_snapshot",
                                                         {"symbol": "000001.SZ", "as_of": self.as_of})])
            return LLMResponse(text="工具结果已返回，综合因子分为正。")

        assistant = QuantAssistant(self.market, "csi300_full", self.as_of,
                                   llm=MockLLMClient(responder))
        out = assistant.ask("解释一下 000001.SZ")
        self.assertEqual(out["mode"], "llm")
        self.assertEqual(out["tool_results"][0]["tool"], "factor_snapshot")
        self.assertIn("综合因子分", out["answer"])

    def test_llm_error_falls_back_to_deterministic(self):
        class BrokenClient:
            def complete(self, messages, tools=None, tool_choice=None):
                return LLMResponse(error_code="PROVIDER_UNREACHABLE",
                                   error_message="offline")

        assistant = QuantAssistant(self.market, "csi300_full", self.as_of, llm=BrokenClient())
        out = assistant.ask("000001.SZ 因子分")
        self.assertTrue(out["degraded"])
        self.assertEqual(out["mode"], "deterministic")

    def test_followup_uses_previous_history_for_symbol(self):
        assistant = QuantAssistant(self.market, "csi300_full", self.as_of)
        first = assistant.ask("000001.SZ 的因子分怎么样", as_of=self.as_of)
        second = assistant.ask("那它现在怎么样？", history=first["history"])
        self.assertEqual(second["tool_results"][0]["tool"], "factor_snapshot")
        self.assertEqual(second["tool_results"][0]["args"]["symbol"], "000001.SZ")
        self.assertIn("综合因子分", second["answer"])

    def test_dashboard_contains_quant_workbench(self):
        html = dash.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="page-quantask"', html)
        self.assertIn("async function askQuant()", html)
        self.assertIn('量化问答（只读）', html)

    def test_dashboard_ask_route(self):
        with patch.object(dash, "_get_market", return_value=self.market), \
             patch.object(dash, "build_llm_client", return_value=MockLLMClient()):
            r = dash.app.test_client().post("/api/quant/ask", json={
                "dataset": "csi300_full",
                "as_of": self.as_of,
                "question": "000001.SZ 的因子分怎么样",
            })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(r.get_json()["result"]["tool_results"][0]["tool"], "factor_snapshot")


if __name__ == "__main__":
    unittest.main()
