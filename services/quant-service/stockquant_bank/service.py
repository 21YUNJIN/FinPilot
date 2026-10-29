"""HTTP boundary for the S3 wealth-risk service.

Run it as a separate process from FinPilot (different Python and dependency
tree), exactly as BANKAGENT_SRS_V5 §15 requires: the bank agent never imports
StockQuant and never gets a file handle into its data directory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import Flask, Response, jsonify, request

from .canonical import SCHEMA_VERSION, iso_now, new_request_id, sha256_json
from .errors import (
    InvalidRequest,
    PayloadTooLarge,
    QuantError,
    RateLimited,
    Unauthorized,
)
from .rank import MODE_WEIGHTS, rank_products
from .report import DISCLAIMERS, build_risk_report, engine_info
from .stress import stress_product
from .nav import validate_series

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_PRODUCTS = 50
RATE_LIMIT_PER_MINUTE = 120


class AuditLog:
    def __init__(self, path: Optional[str]) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: Dict[str, Any]) -> None:
        if not self.path:
            return
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class TokenBucket:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: Dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > 60.0:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                return False
            bucket.append(now)
            return True


def _envelope(
    *,
    request_id: str,
    status: str,
    data: Any,
    error: Any = None,
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "task_id": None,
        "operation_id": None,
        "status": status,
        "data": data,
        "error": error,
        "provenance": provenance or {
            "adapter": "stockquant_bank.wealth_risk/1",
            "observed_at": iso_now(),
        },
    }


def create_app(
    *,
    token: Optional[str] = None,
    dev_open: Optional[bool] = None,
    audit_path: Optional[str] = None,
    engine: Optional[Dict[str, Any]] = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.config["JSON_SORT_KEYS"] = False

    token = token if token is not None else os.environ.get("SQ_BANK_TOKEN", "")
    if dev_open is None:
        dev_open = os.environ.get("SQ_BANK_DEV_OPEN", "") == "1"
    if not token and not dev_open:
        raise RuntimeError(
            "SQ_BANK_TOKEN 未设置。竞赛/联调环境必须设置令牌；仅本机演示可显式设置 SQ_BANK_DEV_OPEN=1。"
        )

    audit = AuditLog(audit_path if audit_path is not None else os.environ.get("SQ_BANK_AUDIT_LOG", "var/audit.jsonl"))
    limiter = TokenBucket(RATE_LIMIT_PER_MINUTE)
    engine_meta = engine or engine_info()

    def _client_key() -> str:
        auth = request.headers.get("Authorization", "")
        raw = auth[7:] if auth.startswith("Bearer ") else request.remote_addr or "anon"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _check_auth() -> None:
        if dev_open and not token:
            return
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, token or ""):
            raise Unauthorized("缺少或错误的服务令牌")

    def _json_body() -> Dict[str, Any]:
        if request.content_length and request.content_length > MAX_BODY_BYTES:
            raise PayloadTooLarge("请求体超过 4MB 上限")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise InvalidRequest("请求体必须是 JSON 对象")
        return payload

    def _products(payload: Dict[str, Any]) -> list:
        products = payload.get("products")
        if not isinstance(products, list) or not products:
            raise InvalidRequest("products 必须是非空数组", detail={"field": "products"})
        if len(products) > MAX_PRODUCTS:
            raise InvalidRequest("单次最多 50 个产品", detail={"field": "products", "max": MAX_PRODUCTS})
        return products

    def _as_of(payload: Dict[str, Any]) -> str:
        value = payload.get("as_of")
        if not value or not isinstance(value, str):
            raise InvalidRequest("必须提供 as_of (YYYY-MM-DD)", detail={"field": "as_of"})
        return value

    def _respond(builder, status: int = 200) -> Tuple[Response, int]:
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        started = time.monotonic()
        endpoint = request.path
        try:
            _check_auth()
            if not limiter.allow(_client_key()):
                raise RateLimited("请求过于频繁，请稍后重试")
            payload = _json_body()
            data = builder(payload, request_id)
            body = _envelope(
                request_id=request_id,
                status="ok",
                data=data,
                provenance={
                    "adapter": "stockquant_bank.wealth_risk/1",
                    "observed_at": iso_now(),
                    "engine": engine_meta,
                    "method_version": engine_meta["method_version"],
                },
            )
            http_status = 200
        except QuantError as exc:
            body = _envelope(request_id=request_id, status="error", data=None, error=exc.to_payload())
            http_status = exc.http_status
        except Exception:  # never leak internals to the caller
            body = _envelope(
                request_id=request_id,
                status="error",
                data=None,
                error={"code": "INTERNAL", "safe_message": "内部计算错误", "retry_mode": "no_retry"},
            )
            http_status = 500
        body["request_id"] = request_id
        audit.write({
            "ts": iso_now(),
            "request_id": request_id,
            "endpoint": endpoint,
            "status": body["status"],
            "error_code": (body.get("error") or {}).get("code"),
            "http_status": http_status,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 2),
            "request_hash": sha256_json(request.get_json(silent=True) or {})[:32],
            "response_hash": sha256_json(body)[:32],
            "client": _client_key(),
        })
        return jsonify(body), http_status

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "engine": engine_meta["name"], "method_version": engine_meta["method_version"]})

    @app.get("/v1/meta")
    def meta():
        return jsonify(_envelope(
            request_id=new_request_id(),
            status="ok",
            data={
                "capabilities": ["quant.risk_report", "quant.rank", "quant.stress"],
                "engine": engine_meta,
                "auth": "dev_open" if (dev_open and not token) else "bearer_token",
                "limits": {
                    "max_products": MAX_PRODUCTS,
                    "max_body_bytes": MAX_BODY_BYTES,
                    "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
                    "max_nav_points_per_product": 5000,
                },
                "preference_modes": MODE_WEIGHTS,
                "guardrails": {
                    "min_returns_for_volatility": 60,
                    "min_returns_for_cvar_95": 252,
                    "trading_days_per_year": 252,
                },
                "disclaimers": DISCLAIMERS,
            },
            provenance={"adapter": "stockquant_bank.wealth_risk/1", "observed_at": iso_now(), "engine": engine_meta},
        ))

    @app.post("/v1/quant/risk-report")
    def risk_report():
        def run(payload: Dict[str, Any], _request_id: str) -> Any:
            return build_risk_report(
                _products(payload),
                as_of=_as_of(payload),
                risk_free_rate=float(payload.get("risk_free_rate", 0.015)),
            )

        return _respond(run)

    @app.post("/v1/quant/rank")
    def rank():
        def run(payload: Dict[str, Any], _request_id: str) -> Any:
            user = payload.get("user")
            if not isinstance(user, dict):
                raise InvalidRequest("user 必须是对象", detail={"field": "user"})
            return rank_products(
                _products(payload),
                user=user,
                as_of=_as_of(payload),
                risk_free_rate=float(payload.get("risk_free_rate", 0.015)),
                include_quant=bool(payload.get("include_quant", True)),
            )

        return _respond(run)

    @app.post("/v1/quant/stress")
    def stress():
        def run(payload: Dict[str, Any], _request_id: str) -> Any:
            products = _products(payload)
            as_of = _as_of(payload)
            scenarios = payload.get("scenarios")
            out = []
            for product in products:
                series = validate_series(
                    str(product.get("product_id") or product.get("id") or ""),
                    product.get("nav") or [],
                    as_of=as_of,
                )
                out.append(stress_product(series, scenarios))
            return {"as_of": as_of, "products": out, "not_a_loss_limit": True}

        return _respond(run)

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify(_envelope(
            request_id=new_request_id(), status="error", data=None,
            error={"code": "PAYLOAD_TOO_LARGE", "safe_message": "请求体超过上限", "retry_mode": "fix_request"},
        )), 413

    return app


app = None  # populated by run.py; importing this module has no side effects


def run(host: str = "127.0.0.1", port: int = 8770) -> None:
    application = create_app()
    application.run(host=host, port=port, threaded=True)