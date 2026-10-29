"""稳定 ID、规范 JSON 与参数哈希（V5 第 6.4 节）。

授权摘要与签名参数必须来自同一结构：scope_hash / parameters_hash 都由
canonical_json 生成，保证 UI 展示与后端校验同源。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .timeutil import to_utc


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def hmac_hex(key: str, value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hmac.new(key.encode("utf-8"), value, hashlib.sha256).hexdigest()


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, datetime):
        return to_utc(value).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """规范 JSON：键排序、无多余空格、时间统一 UTC。

    不参与哈希的字段（如展示用文案）必须在调用前剔除，禁止把 UI 文字纳入签名。
    """
    return json.dumps(_normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def parameters_hash(typed_args: dict[str, Any]) -> str:
    """操作参数哈希：用于精确授权绑定与幂等冲突检测。"""
    return canonical_hash(typed_args)


def scope_hash(scope: dict[str, Any]) -> str:
    """授权范围哈希：与用户在确认界面看到的摘要同源。"""
    return canonical_hash(scope)


def idempotency_request_hash(tool: str, typed_args: dict[str, Any]) -> str:
    return canonical_hash({"tool": tool, "args": typed_args})