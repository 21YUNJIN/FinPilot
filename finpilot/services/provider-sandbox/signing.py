"""独立提供方沙箱：回调签名、验签与投递（V5 第 16.2 节 / API-04）。

本模块刻意不依赖 ``services/api`` 的任何代码：提供方与银行主实现是两套进程、两套状态。
协议（与主实现对接时以此为准，见 README.md）：

签名串
    ``signing_string = f"{timestamp}{nonce}{canonical_json(payload)}"``
    其中 ``timestamp`` 是 Unix 秒（UTC，十进制整数字符串，固定 10 位），
    ``nonce`` 形如 ``n1_<32 位十六进制>``（固定 35 字符），
    ``canonical_json`` 为键排序、无多余空格、UTF-8、``ensure_ascii=False`` 的规范 JSON。
    两段前缀宽度固定，因此无分隔符拼接不存在歧义。

签名
    ``hex(HMAC-SHA256(key=PROVIDER_CALLBACK_SECRET, msg=signing_string.encode("utf-8")))``

头
    ``X-FinPilot-Signature``：小写十六进制摘要（可带 ``sha256=`` 前缀，验签时两种都接受）
    ``X-FinPilot-Timestamp``：Unix 秒（UTC）
    ``X-FinPilot-Nonce``：一次性随机串
    另有 ``X-FinPilot-Provider`` / ``X-FinPilot-Event-Id`` / ``X-FinPilot-Event-Version`` /
    ``X-FinPilot-Delivery`` / ``X-FinPilot-Signature-Alg`` 便于对账，不参与签名。

载荷哈希
    ``payload["payload_hash"] = sha256_hex(canonical_json(payload 去掉 payload_hash 键))``
    签名覆盖“包含 payload_hash 的完整载荷”，验签时先按此重建哈希再校验签名。

回调地址
    ``{PROVIDER_CALLBACK_BASE_URL}/internal/provider-events/{provider_id}``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

DEFAULT_SECRET = "dev_only_provider_callback_secret"
# 与主实现 ``finpilot.config.Settings.provider_callback_base_url`` 的默认值保持一致，
# 使两侧不配置环境变量时也能直接对接（主实现 API 默认监听 58000）。
DEFAULT_BASE_URL = "http://127.0.0.1:58000"
DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_WINDOW_SECONDS = 300

SIGNATURE_HEADER = "X-FinPilot-Signature"
TIMESTAMP_HEADER = "X-FinPilot-Timestamp"
NONCE_HEADER = "X-FinPilot-Nonce"
PROVIDER_HEADER = "X-FinPilot-Provider"
EVENT_ID_HEADER = "X-FinPilot-Event-Id"
EVENT_VERSION_HEADER = "X-FinPilot-Event-Version"
DELIVERY_HEADER = "X-FinPilot-Delivery"
ALG_HEADER = "X-FinPilot-Signature-Alg"
SIGNATURE_ALG = "HMAC-SHA256"

# 验签失败原因码（对外可安全展示，不含密钥信息）
REASON_MISSING_HEADER = "MISSING_SIGNATURE_HEADER"
REASON_BAD_TIMESTAMP = "MALFORMED_TIMESTAMP"
REASON_SIGNATURE = "SIGNATURE_MISMATCH"
REASON_OUT_OF_WINDOW = "TIMESTAMP_OUT_OF_WINDOW"
REASON_PAYLOAD_HASH = "PAYLOAD_HASH_MISMATCH"
REASON_NONCE_REPLAY = "NONCE_REPLAY"
REASON_BAD_PAYLOAD = "MALFORMED_PAYLOAD"


# ---------------------------------------------------------------------------
# 规范 JSON / 摘要
# ---------------------------------------------------------------------------


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """规范 JSON：键排序、紧凑分隔符、时间统一 UTC。签名与哈希必须走同一函数。"""
    return json.dumps(_normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def payload_digest(payload: Mapping[str, Any]) -> str:
    """载荷哈希：去掉 ``payload_hash`` 键后对规范 JSON 取 SHA-256。"""
    body = {k: v for k, v in payload.items() if k != "payload_hash"}
    return sha256_hex(canonical_json(body))


def canonical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """返回带正确 ``payload_hash`` 的规范化载荷副本。"""
    body = {str(k): v for k, v in payload.items() if k != "payload_hash"}
    body["payload_hash"] = payload_digest(body)
    return body


# ---------------------------------------------------------------------------
# 密钥、地址与配置
# ---------------------------------------------------------------------------


def callback_secret() -> str:
    return os.environ.get("PROVIDER_CALLBACK_SECRET") or DEFAULT_SECRET


def callback_base_url() -> str:
    raw = os.environ.get("PROVIDER_CALLBACK_BASE_URL")
    return (raw if raw is not None else DEFAULT_BASE_URL).rstrip("/")


def callback_url(provider_id: str, base_url: str | None = None) -> str:
    base = (base_url if base_url is not None else callback_base_url()).rstrip("/")
    return f"{base}/internal/provider-events/{provider_id}"


def callback_timeout_seconds() -> float:
    try:
        return float(os.environ.get("PROVIDER_CALLBACK_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:  # pragma: no cover - 环境变量写错时退回默认值
        return DEFAULT_TIMEOUT_SECONDS


def callback_delivery_disabled() -> bool:
    return (os.environ.get("PROVIDER_CALLBACK_DISABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def unix_timestamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return str(int(moment.timestamp()))


def new_nonce() -> str:
    return f"n1_{secrets.token_hex(16)}"


# ---------------------------------------------------------------------------
# 签名与头部
# ---------------------------------------------------------------------------


def signing_string(timestamp: str, nonce: str, payload: Mapping[str, Any]) -> str:
    return f"{timestamp}{nonce}{canonical_json(payload)}"


def sign(secret: str, timestamp: str, nonce: str, payload: Mapping[str, Any]) -> str:
    message = signing_string(timestamp, nonce, payload).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SignedCallback:
    """一次签名结果：载荷（含 payload_hash）、头、签名串。"""

    payload: dict[str, Any]
    headers: dict[str, str]
    signature: str
    timestamp: str
    nonce: str
    signing_string: str


def sign_headers(
    payload: Mapping[str, Any],
    *,
    provider_id: str,
    secret: str | None = None,
    timestamp: str | None = None,
    nonce: str | None = None,
    delivery_kind: str = "primary",
) -> dict[str, str]:
    """为载荷生成回调头（不含网络 IO）。"""
    return signed_callback(
        payload,
        provider_id=provider_id,
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
        delivery_kind=delivery_kind,
    ).headers


def signed_callback(
    payload: Mapping[str, Any],
    *,
    provider_id: str,
    secret: str | None = None,
    timestamp: str | None = None,
    nonce: str | None = None,
    delivery_kind: str = "primary",
    now: datetime | None = None,
) -> SignedCallback:
    secret_value = secret if secret is not None else callback_secret()
    ts = timestamp or unix_timestamp(now)
    nc = nonce or new_nonce()
    body = canonical_payload(payload)
    signature = sign(secret_value, ts, nc, body)
    headers = {
        SIGNATURE_HEADER: signature,
        TIMESTAMP_HEADER: ts,
        NONCE_HEADER: nc,
        PROVIDER_HEADER: provider_id,
        ALG_HEADER: SIGNATURE_ALG,
        DELIVERY_HEADER: delivery_kind,
    }
    if body.get("event_id") is not None:
        headers[EVENT_ID_HEADER] = str(body["event_id"])
    if body.get("event_version") is not None:
        headers[EVENT_VERSION_HEADER] = str(body["event_version"])
    return SignedCallback(
        payload=body,
        headers=headers,
        signature=signature,
        timestamp=ts,
        nonce=nc,
        signing_string=signing_string(ts, nc, body),
    )


# ---------------------------------------------------------------------------
# 验签（主实现侧/契约测试侧可复用同一实现，保证协议一致）
# ---------------------------------------------------------------------------


class NonceGuard:
    """一次性 nonce 登记表：重复或乱序重放的 nonce 一律拒绝。

    生产实现应换成带 TTL 的持久化去重表；此处提供线程安全的进程内版本，
    TTL 只用于限制内存占用，不改变“同一 nonce 只能成功一次”的语义。
    """

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def check_and_record(self, nonce: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        self._prune(moment)
        if nonce in self._seen:
            return False
        self._seen[nonce] = moment
        return True

    def _prune(self, moment: float) -> None:
        if not self._seen:
            return
        deadline = moment - self.ttl_seconds
        stale = [k for k, ts in self._seen.items() if ts < deadline]
        for key in stale:
            self._seen.pop(key, None)

    def forget(self, nonce: str) -> None:
        self._seen.pop(nonce, None)

    def __len__(self) -> int:  # pragma: no cover - 便于调试
        return len(self._seen)


@dataclass
class VerificationResult:
    ok: bool
    reason: str = ""
    provider_id: str = ""
    timestamp: int | None = None
    nonce: str = ""
    event_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # 允许 ``if result:`` 直接判断
        return self.ok


def verify_callback(
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    *,
    secret: str | None = None,
    provider_id: str | None = None,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    nonce_guard: NonceGuard | None = None,
    enforce_payload_hash: bool = True,
) -> VerificationResult:
    """校验回调：签名 → 时间窗 → 载荷哈希 → nonce 一次性。

    顺序说明：签名覆盖时间戳与载荷，因此头部被篡改（含只改时间戳）会先以
    ``SIGNATURE_MISMATCH`` 失败；只有签名正确但时间已过期时才报
    ``TIMESTAMP_OUT_OF_WINDOW``。两种都是拒绝，原因码区分便于对账。
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}
    signature = lowered.get(SIGNATURE_HEADER.lower())
    timestamp = lowered.get(TIMESTAMP_HEADER.lower())
    nonce = lowered.get(NONCE_HEADER.lower())
    if not signature or not timestamp or not nonce:
        return VerificationResult(False, REASON_MISSING_HEADER)
    header_provider = str(lowered.get(PROVIDER_HEADER.lower()) or "")
    if provider_id is not None and header_provider and header_provider != provider_id:
        return VerificationResult(False, "PROVIDER_MISMATCH", provider_id=header_provider)

    if not isinstance(payload, Mapping):
        return VerificationResult(False, REASON_BAD_PAYLOAD)
    body = {str(k): v for k, v in payload.items()}

    secret_value = secret if secret is not None else callback_secret()
    expected = sign(secret_value, str(timestamp), str(nonce), body)
    provided = str(signature).strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1]
    if not hmac.compare_digest(expected, provided.lower()):
        return VerificationResult(False, REASON_SIGNATURE, provider_id=header_provider, nonce=str(nonce))

    try:
        ts_value = int(str(timestamp))
    except ValueError:
        return VerificationResult(False, REASON_BAD_TIMESTAMP, provider_id=header_provider, nonce=str(nonce))
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    skew = abs(int(moment.timestamp()) - ts_value)
    if skew > int(window_seconds):
        return VerificationResult(
            False,
            REASON_OUT_OF_WINDOW,
            provider_id=header_provider,
            timestamp=ts_value,
            nonce=str(nonce),
            detail={"skew_seconds": skew, "window_seconds": int(window_seconds)},
        )

    if enforce_payload_hash:
        declared = body.get("payload_hash")
        actual = payload_digest(body)
        if not declared or not hmac.compare_digest(str(declared), actual):
            return VerificationResult(
                False,
                REASON_PAYLOAD_HASH,
                provider_id=header_provider,
                timestamp=ts_value,
                nonce=str(nonce),
                detail={"declared": declared, "computed": actual},
            )

    if nonce_guard is not None and not nonce_guard.check_and_record(str(nonce)):
        return VerificationResult(
            False,
            REASON_NONCE_REPLAY,
            provider_id=header_provider,
            timestamp=ts_value,
            nonce=str(nonce),
        )

    return VerificationResult(
        True,
        "",
        provider_id=header_provider or (provider_id or ""),
        timestamp=ts_value,
        nonce=str(nonce),
        event_id=str(body.get("event_id") or ""),
    )


# ---------------------------------------------------------------------------
# 投递（默认真实 HTTP；测试可注入传输层）
# ---------------------------------------------------------------------------

Transport = Callable[..., Mapping[str, Any]]

_TRANSPORT: Transport | None = None


def set_callback_transport(transport: Transport | None) -> None:
    """注入传输层（测试用）。``None`` 恢复真实 httpx 投递。"""
    global _TRANSPORT
    _TRANSPORT = transport


def deliver_callback(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """投递一次回调。任何失败都只返回结果，不抛出——提供方状态永远以本地库为准。"""
    if callback_delivery_disabled():
        return {"delivered": False, "http_status": None, "error": "DELIVERY_DISABLED", "target_url": url}
    budget = timeout if timeout is not None else callback_timeout_seconds()
    if _TRANSPORT is not None:
        try:
            result = dict(_TRANSPORT(url=url, headers=dict(headers), payload=dict(payload), timeout=budget))
        except Exception as exc:  # 注入层异常同样视为投递失败
            return {"delivered": False, "http_status": None, "error": f"{type(exc).__name__}: {exc}", "target_url": url}
        result.setdefault("target_url", url)
        result.setdefault("http_status", None)
        result.setdefault("error", None)
        return result
    try:  # pragma: no cover - 真实网络分支由集成环境覆盖
        import httpx

        response = httpx.post(url, json=dict(payload), headers=dict(headers), timeout=budget)
        ok = 200 <= response.status_code < 300
        body = response.text[:400] if not ok else ""
        return {
            "delivered": ok,
            "http_status": response.status_code,
            "error": None if ok else f"HTTP_{response.status_code}",
            "response_body": body,
            "target_url": url,
        }
    except Exception as exc:
        return {"delivered": False, "http_status": None, "error": f"{type(exc).__name__}: {exc}", "target_url": url}