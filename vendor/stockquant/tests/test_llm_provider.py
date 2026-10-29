"""Provider parsing, usage/cost accounting and secret-safe logging."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stockquant.assistant.llm import (
    LLMCallLogger,
    LLMConfig,
    LLMResponse,
    OpenAICompatibleClient,
)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ProviderTests(unittest.TestCase):
    def test_openai_compatible_parses_usage_and_cost(self):
        cfg = LLMConfig(
            provider="openai_compatible", model="test-model",
            base_url="https://provider.example/v1", api_key_env="TEST_LLM_KEY",
            input_cost_per_million=1.0, output_cost_per_million=2.0,
        )
        payload = {
            "choices": [{"message": {"content": "ok", "tool_calls": []}}],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000,
                      "total_tokens": 1_500_000},
        }
        with patch.dict(os.environ, {"TEST_LLM_KEY": "test-secret"}):
            with patch("stockquant.assistant.llm.urllib.request.urlopen",
                       return_value=_FakeResponse(payload)):
                result = OpenAICompatibleClient(cfg).complete([{"role": "user", "content": "hi"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage["total_tokens"], 1_500_000)
        self.assertAlmostEqual(result.estimated_cost_usd, 2.0)

    def test_provider_error_is_structured_not_raised(self):
        cfg = LLMConfig(provider="openai_compatible", model="test-model",
                        base_url="https://provider.example/v1", api_key_env="TEST_LLM_KEY")
        with patch.dict(os.environ, {"TEST_LLM_KEY": "test-secret"}):
            with patch("stockquant.assistant.llm.urllib.request.urlopen",
                       return_value=_FakeResponse({"unexpected": True})):
                result = OpenAICompatibleClient(cfg).complete([{"role": "user", "content": "hi"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_PROVIDER_RESPONSE")

    def test_call_logger_does_not_store_prompt_or_key(self):
        path = Path(tempfile.mkdtemp()) / "llm.jsonl"
        logger = LLMCallLogger(str(path))
        logger.record(LLMResponse(text="answer", provider="test", model="m"),
                      question="my private question", tool_names=["factor_snapshot"])
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("my private question", text)
        self.assertNotIn("test-secret", text)
        row = json.loads(text)
        self.assertEqual(row["tool_names"], ["factor_snapshot"])


if __name__ == "__main__":
    unittest.main()
