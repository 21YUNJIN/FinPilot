"""Small provider-agnostic LLM adapter for StockQuant.

The adapter uses only the standard library.  It supports:

* ``mock``: deterministic local client, safe for tests and offline use;
* ``openai_compatible``: any endpoint exposing the OpenAI chat-completions
  shape (OpenAI, local vLLM/Ollama gateway, etc.).

The API key is read from an environment variable and never written to logs or
returned in responses.  If a real provider is configured but the key is absent,
the assistant falls back to the deterministic local path.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


class LLMConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "mock"
    model: str = "mock"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "STOCKQUANT_LLM_API_KEY"
    timeout_seconds: float = 30.0
    max_tokens: int = 900
    temperature: float = 0.0
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            provider=os.getenv("STOCKQUANT_LLM_PROVIDER", "mock").strip().lower(),
            model=os.getenv("STOCKQUANT_LLM_MODEL", "mock").strip(),
            base_url=os.getenv("STOCKQUANT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key_env=os.getenv("STOCKQUANT_LLM_API_KEY_ENV", "STOCKQUANT_LLM_API_KEY"),
            timeout_seconds=float(os.getenv("STOCKQUANT_LLM_TIMEOUT_SECONDS", "30")),
            max_tokens=int(os.getenv("STOCKQUANT_LLM_MAX_TOKENS", "900")),
            temperature=float(os.getenv("STOCKQUANT_LLM_TEMPERATURE", "0")),
            input_cost_per_million=float(os.getenv("STOCKQUANT_LLM_INPUT_COST_PER_MTOK", "0")),
            output_cost_per_million=float(os.getenv("STOCKQUANT_LLM_OUTPUT_COST_PER_MTOK", "0")),
        )

    def api_key(self) -> Optional[str]:
        return os.getenv(self.api_key_env) or os.getenv("OPENAI_API_KEY")


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> Dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


def estimate_cost(config: LLMConfig, usage: Dict[str, int]) -> Optional[float]:
    """Estimate cost from token usage; return None when no prices are configured."""
    if not config.input_cost_per_million and not config.output_cost_per_million:
        return None
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    return (prompt * config.input_cost_per_million +
            completion * config.output_cost_per_million) / 1_000_000.0


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    provider: str = "mock"
    model: str = "mock"
    duration_ms: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    estimated_cost_usd: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


class LLMClient:
    def complete(self, messages: Sequence[Dict[str, Any]],
                 tools: Optional[Sequence[Dict[str, Any]]] = None,
                 tool_choice: Optional[str] = None) -> LLMResponse:
        raise NotImplementedError


class MockLLMClient(LLMClient):
    """Offline client.  A scripted responder can be injected for tests."""

    def __init__(self, responder: Optional[Callable[[Sequence[Dict[str, Any]]], LLMResponse]] = None) -> None:
        self.responder = responder

    def complete(self, messages, tools=None, tool_choice=None) -> LLMResponse:
        started = time.perf_counter()
        if self.responder is not None:
            response = self.responder(messages)
            response.provider = "mock"
            response.model = "mock"
            response.duration_ms = int((time.perf_counter() - started) * 1000)
            return response
        return LLMResponse(
            text="mock LLM: use the deterministic StockQuant query path",
            provider="mock",
            model="mock",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


class OpenAICompatibleClient(LLMClient):
    def __init__(self, config: LLMConfig) -> None:
        key = config.api_key()
        if not key:
            raise LLMConfigurationError(f"missing API key in ${config.api_key_env}")
        if not config.model or config.model == "mock":
            raise LLMConfigurationError("STOCKQUANT_LLM_MODEL is required for openai_compatible")
        self.config = config
        self._key = key

    def complete(self, messages, tools=None, tool_choice=None) -> LLMResponse:
        started = time.perf_counter()
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = tool_choice or "auto"
        req = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return LLMResponse(provider="openai_compatible", model=self.config.model,
                               duration_ms=int((time.perf_counter() - started) * 1000),
                               error_code=f"HTTP_{exc.code}", error_message="provider returned an error")
        except urllib.error.URLError:
            return LLMResponse(provider="openai_compatible", model=self.config.model,
                               duration_ms=int((time.perf_counter() - started) * 1000),
                               error_code="PROVIDER_UNREACHABLE", error_message="provider unreachable")
        except (ValueError, TimeoutError):
            return LLMResponse(provider="openai_compatible", model=self.config.model,
                               duration_ms=int((time.perf_counter() - started) * 1000),
                               error_code="INVALID_PROVIDER_RESPONSE", error_message="provider returned invalid JSON")
        try:
            message = raw["choices"][0]["message"]
            text = str(message.get("content") or "")
            raw_usage = raw.get("usage") or {}
            usage = {
                "prompt_tokens": int(raw_usage.get("prompt_tokens") or 0),
                "completion_tokens": int(raw_usage.get("completion_tokens") or 0),
                "total_tokens": int(raw_usage.get("total_tokens") or 0),
            }
            if not usage["total_tokens"]:
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            cost = estimate_cost(self.config, usage)
            calls = []
            for item in message.get("tool_calls") or []:
                fn = item.get("function") or {}
                args = fn.get("arguments") or "{}"
                try:
                    parsed = json.loads(args) if isinstance(args, str) else dict(args)
                except Exception:
                    parsed = {}
                calls.append(ToolCall(
                    call_id=str(item.get("id") or f"call-{len(calls)}"),
                    name=str(fn.get("name") or ""),
                    arguments=parsed,
                ))
        except Exception:
            return LLMResponse(provider="openai_compatible", model=self.config.model,
                               duration_ms=int((time.perf_counter() - started) * 1000),
                               error_code="INVALID_PROVIDER_RESPONSE", error_message="missing choices/message")
        return LLMResponse(
            text=text, tool_calls=calls, provider="openai_compatible", model=self.config.model,
            duration_ms=int((time.perf_counter() - started) * 1000),
            usage=usage, estimated_cost_usd=cost,
        )


class LLMCallLogger:
    """Append-only metadata log: no prompts, no tool payloads, no API key."""

    def __init__(self, path: str = "data/state/llm_calls.jsonl") -> None:
        self.path = Path(path)

    def record(self, response: LLMResponse, *, question: str,
               tool_names: Sequence[str] = ()) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest()[:16],
            "provider": response.provider,
            "model": response.model,
            "duration_ms": response.duration_ms,
            "error_code": response.error_code,
            "tool_names": list(tool_names),
            "usage": response.usage,
            "estimated_cost_usd": response.estimated_cost_usd,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_llm_client(config: Optional[LLMConfig] = None) -> LLMClient:
    cfg = config or LLMConfig.from_env()
    if cfg.provider == "mock":
        return MockLLMClient()
    if cfg.provider == "openai_compatible":
        try:
            return OpenAICompatibleClient(cfg)
        except LLMConfigurationError:
            return MockLLMClient()
    raise LLMConfigurationError(f"unknown LLM provider: {cfg.provider}")


__all__ = ["LLMConfig", "LLMResponse", "ToolCall", "LLMClient",
           "MockLLMClient", "OpenAICompatibleClient", "build_llm_client",
           "LLMCallLogger", "LLMConfigurationError", "estimate_cost"]
