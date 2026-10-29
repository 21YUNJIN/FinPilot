"""FinPilot-side read-only client for the S3 wealth-risk service.

Drop-in target in the FinPilot repo (C03):
    services/api/src/finpilot/adapters/stockquant/http_client.py

Design rules, taken from BANKAGENT_SRS_V5 §15 and the 2026-10-28 review:

* the client never imports StockQuant and never touches its data directory;
* every response is checked against the request: schema version, status,
  method version and the per-product ``source_hash`` recomputed locally;
* a missing service is an explicit dependency failure, not a silent zero;
* nothing here can trade, transfer, or change a suitability level.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urljoin

import requests

SCHEMA_MAJOR = "1"
PINNED_METHOD_VERSION = "sq-wealth-risk/1.0.0"


class QuantUnavailable(RuntimeError):
    """The service is down, timed out, or rejected the call."""


class QuantContractError(RuntimeError):
    """The service answered, but the answer cannot be trusted."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def nav_source_hash(points: Iterable[Mapping[str, Any]]) -> str:
    rows = [
        {"date": str(p["date"]).strip(), "value": round(float(p["value"]), 6)}
        for p in points
    ]
    rows.sort(key=lambda row: row["date"])
    return hashlib.sha256(_canonical(rows).encode("utf-8")).hexdigest()


@dataclass
class QuantCall:
    request_id: str
    data: Dict[str, Any]
    provenance: Dict[str, Any]
    latency_ms: float
    raw: Dict[str, Any]


class StockQuantRiskClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 3.0,
        pinned_method_version: Optional[str] = PINNED_METHOD_VERSION,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout
        self.pinned_method_version = pinned_method_version
        self.session = session or requests.Session()

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "StockQuantRiskClient":
        base = os.environ.get("STOCKQUANT_SERVICE_URL", "http://127.0.0.1:8770")
        token = os.environ.get("STOCKQUANT_SERVICE_TOKEN", "")
        if not token:
            raise QuantUnavailable("STOCKQUANT_SERVICE_TOKEN 未设置：不允许匿名调用量化服务")
        return cls(base, token)

    # ------------------------------------------------------------------
    def _post(self, path: str, payload: Mapping[str, Any]) -> QuantCall:
        request_id = f"fp_{uuid.uuid4().hex}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        }
        started = time.monotonic()
        try:
            response = self.session.post(
                urljoin(self.base_url, path.lstrip("/")),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise QuantUnavailable(f"量化服务不可达: {type(exc).__name__}") from exc
        latency_ms = (time.monotonic() - started) * 1000.0
        try:
            body = response.json()
        except ValueError as exc:
            raise QuantContractError("量化服务返回非 JSON") from exc
        if not isinstance(body, dict) or "status" not in body:
            raise QuantContractError("量化服务返回缺少信封字段")
        if body.get("request_id") != request_id:
            raise QuantContractError("响应 request_id 与请求不一致，可能是串包或代理缓存")
        if not 200 <= response.status_code < 300 or body.get("status") != "ok":
            error = body.get("error") or {}
            if not isinstance(error, dict):
                raise QuantContractError("量化服务错误字段格式无效")
            raise QuantUnavailable(f"{error.get('code')}: {error.get('safe_message')}")
        if str(body.get("schema_version", "")).split(".")[0] != SCHEMA_MAJOR:
            raise QuantContractError(f"不支持的 schema_version: {body.get('schema_version')}")
        provenance = body.get("provenance") or {}
        if not isinstance(provenance, dict) or not isinstance(body.get("data"), dict):
            raise QuantContractError("量化服务数据或来源格式无效")
        method = provenance.get("method_version")
        if self.pinned_method_version and method != self.pinned_method_version:
            raise QuantContractError(f"方法版本不匹配: {method}")
        return QuantCall(
            request_id=body.get("request_id", ""),
            data=body.get("data") or {},
            provenance=provenance,
            latency_ms=latency_ms,
            raw=body,
        )

    # ------------------------------------------------------------------
    def risk_report(self, products: Sequence[Mapping[str, Any]], *, as_of: str) -> QuantCall:
        call = self._post("/v1/quant/risk-report", {"as_of": as_of, "products": list(products)})
        by_id = {str(p.get("product_id")): p for p in products}
        reports = call.data.get("products")
        failures = call.data.get("failures")
        if (not isinstance(reports, list) or not isinstance(failures, list)
                or any(not isinstance(item, dict) for item in reports + failures)
                or len(by_id) != len(products)):
            raise QuantContractError("产品结果或失败列表格式无效")
        for report in reports:
            pid = str(report.get("product_id"))
            source = by_id.get(pid)
            if source is None:
                raise QuantContractError(f"服务返回了未请求的产品: {pid}")
            expected = nav_source_hash(source.get("nav") or [])
            provenance = report.get("provenance")
            if not isinstance(provenance, dict):
                raise QuantContractError("产品来源格式无效")
            actual = provenance.get("source_hash")
            if expected != actual:
                raise QuantContractError(f"{pid} 的净值哈希与服务端不一致，拒绝使用该结果")
        returned = [str(r.get("product_id")) for r in reports + failures]
        if len(returned) != len(set(returned)) or set(returned) != set(by_id):
            raise QuantContractError("响应产品集合不完整或重复")
        if call.data.get("as_of") != as_of:
            raise QuantContractError("响应观察日与请求不一致")
        units = {"cagr": "ratio_per_year", "annual_volatility": "ratio_per_year",
                 "max_drawdown": "ratio", "historical_cvar_95": "daily_return"}
        for report in reports:
            if report.get("provenance", {}).get("method_version") != self.pinned_method_version:
                raise QuantContractError("产品方法版本不匹配")
            if not isinstance(report.get("metrics"), dict):
                raise QuantContractError("产品指标格式无效")
            for name, unit in units.items():
                metric = report.get("metrics", {}).get(name, {})
                if not isinstance(metric, dict):
                    raise QuantContractError(f"{name} 指标格式无效")
                value = metric.get("value")
                if metric.get("unit") != unit:
                    raise QuantContractError(f"{name} 单位不匹配")
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise QuantContractError(f"{name} 不是有限数")
                if value is None and not metric.get("na_reason"):
                    raise QuantContractError(f"{name} 缺失 NA 原因")
        return call

    def rank(self, products: Sequence[Mapping[str, Any]], *, user: Mapping[str, Any], as_of: str) -> QuantCall:
        return self._post("/v1/quant/rank", {"as_of": as_of, "user": dict(user), "products": list(products)})

    def stress(
        self,
        products: Sequence[Mapping[str, Any]],
        *,
        as_of: str,
        scenarios: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> QuantCall:
        payload: Dict[str, Any] = {"as_of": as_of, "products": list(products)}
        if scenarios:
            payload["scenarios"] = list(scenarios)
        return self._post("/v1/quant/stress", payload)

    def meta(self) -> Dict[str, Any]:
        try:
            response = self.session.get(urljoin(self.base_url, "v1/meta"), timeout=self.timeout)
            return response.json()
        except requests.RequestException as exc:
            raise QuantUnavailable(f"量化服务不可达: {type(exc).__name__}") from exc


def usable_metrics(report: Mapping[str, Any]) -> List[str]:
    """Names of metrics that actually carry a value (for UI 'NA' handling)."""
    return [
        name for name, item in (report.get("metrics") or {}).items()
        if isinstance(item, Mapping) and item.get("value") is not None
    ]
