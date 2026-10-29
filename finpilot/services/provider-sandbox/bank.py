"""独立银行沙箱（端口 58101，provider_id = ``bank_sandbox_v1``）。

与 ``services/api``（主银行实现，PostgreSQL）**完全分离**：本服务是另一套进程、另一个状态库
（``BANK_SANDBOX_STATE``，默认 ``G:/比赛/finpilot/.state/bank_sandbox.sqlite3``，stdlib sqlite3，
不使用 SQLAlchemy）。用途是验证跨系统异步执行、迟到与乱序回执、幂等与故障恢复，而不是同库调用。

端点
    POST   /bank/transfers                            受理转账（稳定业务键幂等）
    GET    /bank/transfers/{stable_operation_ref}     权威状态查询
    POST   /bank/transfers/{stable_operation_ref}/cancel
    POST   /bank/advance                              推进状态并（可重复）发出回调
    POST   /bank/faults、GET /bank/faults、DELETE /bank/faults
    GET    /bank/health
    只读巡检：GET /bank/operations、GET /bank/events/{ref}、GET /bank/deliveries

对外语义（与主实现对接时以此为准）
- 业务终局一律 HTTP 200 + ``status`` 字段；HTTP 非 2xx 只表示技术性结果
  （422 参数、409 同键异参、404 未知键、503 不可用/查询失败、504 应答丢失）。
- ``POST /bank/transfers`` 只受理，不主动投递回调；受理事件先落库为 pending，
  由 ``POST /bank/advance {"emit_callback": true}`` 投递（可 ``duplicate``、可乱序、可迟到重投）。
  这样调用方可以精确模拟“外部已受理但应答丢失 / 迟到成功 / 重复回调”。
- 回调载荷里的 ``operation_id`` 就是**稳定业务键**（``stable_operation_ref``），另有同值字段
  ``stable_operation_ref`` 便于阅读；``owner_user_id`` 仅供对账展示，接收方不得用它覆盖本地归属。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

try:  # 与主实现保持技术独立，同时兼容“目录在 sys.path 上”和“包方式”两种导入
    import faults as faults_mod
    import signing
    import store as store_mod
except ImportError:  # pragma: no cover - 包方式导入的回退
    from . import faults as faults_mod  # type: ignore
    from . import signing  # type: ignore
    from . import store as store_mod  # type: ignore

PROVIDER_ID = "bank_sandbox_v1"
SERVICE_NAME = "FinPilot 银行沙箱（独立业务提供方）"
SERVICE_PORT = 58101
STATE_ENV = store_mod.BANK_STATE_ENV
DEFAULT_STATE = store_mod.DEFAULT_BANK_STATE
PREFIX = "pb"

MAX_DUPLICATE = 20
MAX_AMOUNT_MINOR = 10**12
SUBMIT_FIELDS = {
    "stable_operation_ref",
    "owner_user_id",
    "payee_ref",
    "amount_minor",
    "fee_minor",
    "currency",
    "memo",
    "accept_as_pending",
}
ADVANCE_FIELDS = {
    "stable_operation_ref",
    "settle",
    "fail",
    "emit_callback",
    "duplicate",
    "replay_event_version",
    "emit_order",
    "mark_pending",
    "failure_code",
    "reason",
}

SAFE_MESSAGES = {
    "VALIDATION_ERROR": "请求字段不合法，未产生任何业务效果。",
    "IDEMPOTENCY_CONFLICT": "同一稳定业务键已存在且参数不同，已拒绝重复提交。",
    "NOT_FOUND": "该稳定业务键在提供方不存在。",
    "PROVIDER_UNAVAILABLE": "提供方当前不可用，未产生任何业务效果。",
    "PROVIDER_QUERY_FAILED": "提供方查询暂时失败，业务状态未改变，请用同一业务键重查。",
    "PROVIDER_TIMEOUT": "请求结果尚未确认，请用同一稳定业务键查询原操作。",
    "PROVIDER_REJECTED": "提供方明确拒绝该笔转账。",
    "FAULT_CONFIG_ERROR": "故障注入配置不合法。",
}
RETRY_MODES = {
    "VALIDATION_ERROR": "fix_request",
    "IDEMPOTENCY_CONFLICT": "new_stable_key_or_query",
    "NOT_FOUND": "check_stable_key",
    "PROVIDER_UNAVAILABLE": "retry_later",
    "PROVIDER_QUERY_FAILED": "query_same_operation",
    "PROVIDER_TIMEOUT": "query_same_operation",
    "PROVIDER_REJECTED": "stop_or_replan",
    "FAULT_CONFIG_ERROR": "fix_request",
}
STATUS_FOR_CODE = {
    "VALIDATION_ERROR": "invalid",
    "IDEMPOTENCY_CONFLICT": "conflict",
    "NOT_FOUND": "not_found",
    "PROVIDER_UNAVAILABLE": "unavailable",
    "PROVIDER_QUERY_FAILED": "query_failed",
    "PROVIDER_TIMEOUT": "unknown",
    "PROVIDER_REJECTED": "failed",
    "FAULT_CONFIG_ERROR": "invalid",
}
HTTP_FOR_CODE = {
    "VALIDATION_ERROR": 422,
    "IDEMPOTENCY_CONFLICT": 409,
    "NOT_FOUND": 404,
    "PROVIDER_UNAVAILABLE": 503,
    "PROVIDER_QUERY_FAILED": 503,
    "PROVIDER_TIMEOUT": 504,
    "FAULT_CONFIG_ERROR": 422,
}


# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------


def error_body(code: str, *, details: Any = None, message: str | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider_id": PROVIDER_ID,
        "status": STATUS_FOR_CODE.get(code, "error"),
        "failure_code": code,
        "error": {
            "code": code,
            "safe_message": message or SAFE_MESSAGES.get(code, "提供方返回结构化错误。"),
            "retry_mode": RETRY_MODES.get(code, "none"),
        },
        "observed_at": store_mod.now_iso(),
    }
    if details:
        body["error"]["details"] = details
    body.update(extra)
    return body


def error_response(code: str, *, details: Any = None, message: str | None = None, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=HTTP_FOR_CODE.get(code, 400), content=error_body(code, details=details, message=message, **extra))


# ---------------------------------------------------------------------------
# 字段校验
# ---------------------------------------------------------------------------


@dataclass
class FieldErrors:
    items: list[dict[str, Any]]

    def add(self, field: str, message: str, value: Any = None) -> None:
        self.items.append({"field": field, "message": message, "value": value})

    def __bool__(self) -> bool:
        return bool(self.items)


def _reject_unknown(body: Mapping[str, Any], allowed: set[str], errors: FieldErrors, *, where: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        errors.add("unknown_fields", f"{where} 不接受这些字段", unknown)


def _take_str(
    body: Mapping[str, Any],
    field: str,
    errors: FieldErrors,
    *,
    required: bool = True,
    max_len: int = 96,
    default: str = "",
) -> str:
    if field not in body or body[field] is None:
        if required:
            errors.add(field, "必填字段缺失")
        return default
    value = body[field]
    if not isinstance(value, str):
        errors.add(field, "必须是字符串", value)
        return default
    value = value.strip()
    if required and not value:
        errors.add(field, "不能为空字符串")
    if len(value) > max_len:
        errors.add(field, f"长度不得超过 {max_len}", len(value))
    return value


def _take_int(
    body: Mapping[str, Any],
    field: str,
    errors: FieldErrors,
    *,
    required: bool = False,
    default: int = 0,
    minimum: int = 0,
    maximum: int = MAX_AMOUNT_MINOR,
) -> int:
    if field not in body or body[field] is None:
        if required:
            errors.add(field, "必填字段缺失")
        return default
    value = body[field]
    if isinstance(value, bool) or not isinstance(value, int):
        errors.add(field, "必须是整数（分）", value)
        return default
    if value < minimum or value > maximum:
        errors.add(field, f"必须在 [{minimum}, {maximum}] 之间", value)
        return default
    return int(value)


def _take_bool(body: Mapping[str, Any], field: str, errors: FieldErrors, *, default: bool = False) -> bool:
    if field not in body or body[field] is None:
        return default
    value = body[field]
    if not isinstance(value, bool):
        errors.add(field, "必须是布尔值", value)
        return default
    return value


def validate_transfer(body: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors = FieldErrors([])
    if not isinstance(body, Mapping):
        return None, [{"field": "body", "message": "请求体必须是 JSON 对象", "value": type(body).__name__}]
    _reject_unknown(body, SUBMIT_FIELDS, errors, where="POST /bank/transfers")
    payload = {
        "stable_operation_ref": _take_str(body, "stable_operation_ref", errors, max_len=96),
        "owner_user_id": _take_str(body, "owner_user_id", errors, max_len=64),
        "payee_ref": _take_str(body, "payee_ref", errors, max_len=96),
        "amount_minor": _take_int(body, "amount_minor", errors, required=True, minimum=1, maximum=MAX_AMOUNT_MINOR),
        "fee_minor": _take_int(body, "fee_minor", errors, default=0, minimum=0, maximum=MAX_AMOUNT_MINOR),
        "currency": _take_str(body, "currency", errors, required=False, max_len=3, default="CNY") or "CNY",
        "memo": _take_str(body, "memo", errors, required=False, max_len=200, default=""),
        "accept_as_pending": _take_bool(body, "accept_as_pending", errors, default=False),
    }
    if payload["currency"].upper() != "CNY":
        errors.add("currency", "本沙箱币种固定 CNY", payload["currency"])
    payload["currency"] = payload["currency"].upper()
    if errors:
        return None, errors.items
    return payload, []


def validate_advance(body: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors = FieldErrors([])
    if body is None:
        body = {}
    if not isinstance(body, Mapping):
        return None, [{"field": "body", "message": "请求体必须是 JSON 对象", "value": type(body).__name__}]
    _reject_unknown(body, ADVANCE_FIELDS, errors, where="POST /bank/advance")
    key = _take_str(body, "stable_operation_ref", errors, required=False, max_len=96)
    order = _take_str(body, "emit_order", errors, required=False, max_len=4, default="asc") or "asc"
    if order not in ("asc", "desc"):
        errors.add("emit_order", "只能是 asc 或 desc", order)
    replay = body.get("replay_event_version")
    replay_version: int | None = None
    if replay is not None:
        replay_version = _take_int(body, "replay_event_version", errors, minimum=1, maximum=10**6)
        if replay_version and not key:
            errors.add("replay_event_version", "重投历史版本必须指定 stable_operation_ref")
    payload = {
        "stable_operation_ref": key or None,
        "settle": _take_bool(body, "settle", errors),
        "fail": _take_bool(body, "fail", errors),
        "emit_callback": _take_bool(body, "emit_callback", errors),
        "duplicate": _take_int(body, "duplicate", errors, default=0, minimum=0, maximum=MAX_DUPLICATE),
        "replay_event_version": replay_version,
        "emit_order": order,
        "mark_pending": _take_bool(body, "mark_pending", errors),
        "failure_code": _take_str(body, "failure_code", errors, required=False, max_len=48),
        "reason": _take_str(body, "reason", errors, required=False, max_len=200),
    }
    if payload["settle"] and payload["fail"]:
        errors.add("settle", "settle 与 fail 不能同时为真", True)
    if errors:
        return None, errors.items
    return payload, []


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


def operation_view(op: Mapping[str, Any], effects: list[dict[str, Any]]) -> dict[str, Any]:
    request = op.get("request", {}) or {}
    data = op.get("data", {}) or {}
    status = str(op["status"])
    return {
        "provider_id": op["provider_id"],
        "stable_operation_ref": op["stable_operation_ref"],
        "operation_id": op["stable_operation_ref"],
        "provider_ref": op["provider_ref"],
        "status": status,
        "settled": bool(op["settled"]),
        "settled_at": op["settled_at"],
        "accepted_at": op["accepted_at"],
        "cancelled_at": op["cancelled_at"],
        "failure_code": op["failure_code"],
        "reason": op["reason"],
        "amount_minor": int(request.get("amount_minor", 0)),
        "fee_minor": int(request.get("fee_minor", 0)),
        "currency": request.get("currency", "CNY"),
        "owner_user_id": request.get("owner_user_id", ""),
        "payee_ref": request.get("payee_ref", ""),
        "memo": request.get("memo", ""),
        "last_event_version": int(op["last_event_version"]),
        "effects_applied": len(effects),
        "effect_kinds": [e["effect_kind"] for e in effects],
        "effects": effects,
        "cancellable": status in ("accepted", "pending") and not bool(op["settled"]),
        "terminal": status in ("succeeded", "failed", "cancelled"),
        "created_at": op["created_at"],
        "updated_at": op["updated_at"],
        "observed_at": store_mod.now_iso(),
        **({"raw": data} if data else {}),
    }


def event_extras(op: Mapping[str, Any], **overrides: Any) -> dict[str, Any]:
    request = op.get("request", {}) or {}
    extras = {
        "amount_minor": int(request.get("amount_minor", 0)),
        "fee_minor": int(request.get("fee_minor", 0)),
        "currency": request.get("currency", "CNY"),
        "owner_user_id": request.get("owner_user_id", ""),
        "payee_ref": request.get("payee_ref", ""),
        "failure_code": op.get("failure_code"),
        "reason": op.get("reason", ""),
        "settled": bool(op.get("settled")),
        "settled_at": op.get("settled_at"),
    }
    extras.update(overrides)
    return extras


def event_public(event: Mapping[str, Any]) -> dict[str, Any]:
    """对外暴露的事件：载荷 + 版本 + 哈希（签名头见投递日志）。"""
    return {
        "event_id": event["event_id"],
        "stable_operation_ref": event["stable_operation_ref"],
        "provider_ref": event["provider_ref"],
        "event_version": int(event["event_version"]),
        "event_type": event["event_type"],
        "status": event["status"],
        "occurred_at": event["occurred_at"],
        "payload_hash": event["payload_hash"],
        "delivery_state": event["delivery_state"],
        "delivery_attempts": int(event["delivery_attempts"]),
        "payload": event["payload"],
    }


def delivery_public(delivery: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "delivery_id": delivery["delivery_id"],
        "event_id": delivery["event_id"],
        "stable_operation_ref": delivery["stable_operation_ref"],
        "provider_ref": delivery["provider_ref"],
        "event_version": delivery["event_version"],
        "kind": delivery["kind"],
        "attempt": delivery["attempt"],
        "target_url": delivery["target_url"],
        "delivered": delivery["delivered"],
        "http_status": delivery["http_status"],
        "error": delivery["error"],
        "headers": delivery["headers"],
        "payload": delivery["payload"],
        "created_at": delivery["created_at"],
    }


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


def create_app(
    *,
    state_path: str | Path | None = None,
    sandbox_store: "store_mod.SandboxStore | None" = None,
    delay_cap_ms: int | None = None,
) -> FastAPI:
    """构造银行沙箱应用；测试可传入独立的 ``state_path`` 保证互不污染。"""
    sandbox = sandbox_store or store_mod.SandboxStore(
        Path(state_path) if state_path is not None else store_mod.default_state_path(STATE_ENV, DEFAULT_STATE),
        provider_id=PROVIDER_ID,
        prefix=PREFIX,
    )
    sandbox.initialize()
    injector = faults_mod.FaultInjector(sandbox, delay_cap_ms=delay_cap_ms)

    app = FastAPI(title=SERVICE_NAME, version="1.0.0", docs_url="/bank/docs", openapi_url="/bank/openapi.json")
    app.state.sandbox_store = sandbox
    app.state.faults = injector
    app.state.provider_id = PROVIDER_ID

    # -- 统一的参数错误形状 ------------------------------------------------

    @app.exception_handler(RequestValidationError)
    def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:  # pragma: no cover
        return error_response("VALIDATION_ERROR", details=exc.errors())

    @app.exception_handler(store_mod.IdempotencyConflict)
    def _conflict_handler(request: Request, exc: store_mod.IdempotencyConflict) -> JSONResponse:
        return error_response(
            "IDEMPOTENCY_CONFLICT",
            details={"stable_operation_ref": exc.stable_operation_ref},
            stable_operation_ref=exc.stable_operation_ref,
        )

    @app.exception_handler(store_mod.OperationNotFound)
    def _not_found_handler(request: Request, exc: store_mod.OperationNotFound) -> JSONResponse:
        return error_response("NOT_FOUND", details={"stable_operation_ref": exc.stable_operation_ref})

    def gate(*, query: bool) -> JSONResponse | None:
        """故障闸门：unavailable 对所有业务端点生效；查询失败只影响查询。"""
        if injector.is_unavailable():
            return error_response("PROVIDER_UNAVAILABLE")
        if query and injector.take_query_failure():
            return error_response("PROVIDER_QUERY_FAILED")
        return None

    def dropped(trace: Mapping[str, Any]) -> JSONResponse:
        """应答丢失：业务已受理，但客户端只能看到超时语义。"""
        return error_response("PROVIDER_TIMEOUT", **dict(trace))

    # -- 受理 --------------------------------------------------------------

    @app.post("/bank/transfers")
    def submit_transfer(payload: Any = Body(default=None)) -> JSONResponse:
        blocked = gate(query=False)
        if blocked is not None:
            return blocked
        data, errors = validate_transfer(payload)
        if data is None:
            return error_response("VALIDATION_ERROR", details=errors)
        key = data["stable_operation_ref"]
        with sandbox.write() as conn:
            existing = sandbox.get_operation(conn, key)
            if existing is not None:
                # 幂等：同键同参只返回既有受理结果，绝不产生第二次业务效果；同键异参拒绝
                sandbox.assert_same_request(existing, data)
                view = operation_view(existing, sandbox.effects(conn, key))
            elif injector.take_reject(conn):
                # 可验证的业务拒绝：落库为终态 failed，重复提交返回同一结果
                op = sandbox.create_operation(
                    conn,
                    stable_operation_ref=key,
                    request=data,
                    status="failed",
                    failure_code="PROVIDER_REJECTED",
                    reason="provider_rejected",
                )
                sandbox.record_event(
                    conn, operation=op, status="failed", event_type="transfer.rejected",
                    extra=event_extras(op, reason="provider_rejected"),
                )
                view = operation_view(sandbox.require_operation(conn, key), [])
            else:
                status = "pending" if data["accept_as_pending"] else "accepted"
                op = sandbox.create_operation(
                    conn,
                    stable_operation_ref=key,
                    request=data,
                    status=status,
                    accepted_at=store_mod.now_iso(),
                )
                sandbox.record_event(
                    conn, operation=op, status=status, event_type=f"transfer.{status}", extra=event_extras(op)
                )
                view = operation_view(sandbox.require_operation(conn, key), [])
        injector.wait()
        if injector.drops_response():
            return dropped({"stable_operation_ref": key, "provider_ref": view["provider_ref"]})
        return JSONResponse(status_code=200, content=view)

    # -- 查询 --------------------------------------------------------------

    @app.get("/bank/transfers/{stable_operation_ref}")
    def query_transfer(stable_operation_ref: str) -> JSONResponse:
        blocked = gate(query=True)
        if blocked is not None:
            return blocked
        injector.wait()
        with sandbox.read() as conn:
            op = sandbox.get_operation(conn, stable_operation_ref)
            if op is None:
                return error_response("NOT_FOUND", details={"stable_operation_ref": stable_operation_ref})
            effects = sandbox.effects(conn, stable_operation_ref)
            return JSONResponse(status_code=200, content=operation_view(op, effects))

    # -- 撤单 --------------------------------------------------------------

    @app.post("/bank/transfers/{stable_operation_ref}/cancel")
    def cancel_transfer(stable_operation_ref: str) -> JSONResponse:
        blocked = gate(query=False)
        if blocked is not None:
            return blocked
        with sandbox.write() as conn:
            op = sandbox.get_operation(conn, stable_operation_ref)
            if op is None:
                return error_response("NOT_FOUND", details={"stable_operation_ref": stable_operation_ref})
            status = str(op["status"])
            if status == "cancelled":
                return JSONResponse(
                    status_code=200,
                    content={
                        "provider_id": PROVIDER_ID,
                        "stable_operation_ref": stable_operation_ref,
                        "provider_ref": op["provider_ref"],
                        "status": "cancelled",
                        "reason": "already_cancelled",
                        "already_cancelled": True,
                        "operation_status": "cancelled",
                        "cancelled_at": op["cancelled_at"],
                        "observed_at": store_mod.now_iso(),
                    },
                )
            if bool(op["settled"]) or status in ("succeeded", "failed"):
                # 撤单失败绝不把原操作改成失败/取消：状态保持原样
                reason = "already_settled" if bool(op["settled"]) else f"terminal_status_{status}"
                return JSONResponse(
                    status_code=200,
                    content={
                        "provider_id": PROVIDER_ID,
                        "stable_operation_ref": stable_operation_ref,
                        "provider_ref": op["provider_ref"],
                        "status": "cancel_rejected",
                        "reason": reason,
                        "operation_status": status,
                        "settled": bool(op["settled"]),
                        "settled_at": op["settled_at"],
                        "observed_at": store_mod.now_iso(),
                    },
                )
            sandbox.apply_effect(
                conn, stable_operation_ref, "cancel_accepted", detail={"from_status": status}
            )
            updated = sandbox.update_operation(
                conn,
                stable_operation_ref,
                status="cancelled",
                cancelled_at=store_mod.now_iso(),
                reason="cancelled_before_settlement",
            )
            sandbox.record_event(
                conn,
                operation=updated,
                status="cancelled",
                event_type="transfer.cancelled",
                extra=event_extras(updated, reason="cancelled_before_settlement"),
            )
            view = operation_view(sandbox.require_operation(conn, stable_operation_ref), sandbox.effects(conn, stable_operation_ref))
        injector.wait()
        if injector.drops_response():
            return dropped({"stable_operation_ref": stable_operation_ref, "provider_ref": view["provider_ref"]})
        return JSONResponse(
            status_code=200,
            content={
                "provider_id": PROVIDER_ID,
                "stable_operation_ref": stable_operation_ref,
                "provider_ref": view["provider_ref"],
                "status": "cancelled",
                "reason": "cancelled_before_settlement",
                "operation_status": "cancelled",
                "cancelled_at": view["cancelled_at"],
                "cancellable": False,
                "last_event_version": view["last_event_version"],
                "observed_at": store_mod.now_iso(),
            },
        )

    # -- 推进与回调 --------------------------------------------------------

    @app.post("/bank/advance")
    def advance(payload: Any = Body(default=None)) -> JSONResponse:
        blocked = gate(query=False)
        if blocked is not None:
            return blocked
        config, errors = validate_advance(payload)
        if config is None:
            return error_response("VALIDATION_ERROR", details=errors)
        advanced: list[dict[str, Any]] = []
        event_payloads: list[dict[str, Any]] = []
        delivery_views: list[dict[str, Any]] = []
        duplicates_sent = 0
        target_url = ""
        with sandbox.write() as conn:
            key = config["stable_operation_ref"]
            if key is not None:
                op = sandbox.get_operation(conn, key)
                if op is None:
                    return error_response("NOT_FOUND", details={"stable_operation_ref": key})
                targets = [op]
            else:
                targets = sandbox.list_operations(conn, statuses=("accepted", "pending"))

            for op in targets:
                key_i = str(op["stable_operation_ref"])
                from_status = str(op["status"])
                applied = False
                skip_reason: str | None = None
                to_status = from_status
                if config["settle"]:
                    if from_status not in ("accepted", "pending"):
                        skip_reason = f"not_settleable_{from_status}"
                    else:
                        settled_now = sandbox.mark_settled(
                            conn, key_i, status="succeeded", reason="provider_settled"
                        )
                        if not settled_now:
                            skip_reason = "already_settled"
                        else:
                            # 业务效果只登记一次：唯一约束保证重复推进不会二次结算
                            sandbox.apply_effect(
                                conn,
                                key_i,
                                "bank_transfer_settle",
                                amount_minor=int(op["request"].get("amount_minor", 0)),
                                detail={
                                    "fee_minor": int(op["request"].get("fee_minor", 0)),
                                    "currency": op["request"].get("currency", "CNY"),
                                },
                            )
                            op = sandbox.require_operation(conn, key_i)
                            sandbox.record_event(
                                conn,
                                operation=op,
                                status="succeeded",
                                event_type="transfer.succeeded",
                                extra=event_extras(op, reason="provider_settled"),
                            )
                            applied = True
                            to_status = "succeeded"
                elif config["fail"]:
                    if from_status not in ("accepted", "pending"):
                        skip_reason = f"not_failable_{from_status}"
                    else:
                        code = config["failure_code"] or "PROVIDER_REJECTED"
                        reason = config["reason"] or "provider_failed"
                        op = sandbox.update_operation(
                            conn, key_i, status="failed", failure_code=code, reason=reason
                        )
                        sandbox.record_event(
                            conn,
                            operation=op,
                            status="failed",
                            event_type="transfer.failed",
                            extra=event_extras(op, failure_code=code, reason=reason),
                        )
                        applied = True
                        to_status = "failed"
                elif config["mark_pending"]:
                    if from_status != "accepted":
                        skip_reason = f"not_pendable_{from_status}"
                    else:
                        op = sandbox.update_operation(conn, key_i, status="pending", reason="awaiting_provider")
                        sandbox.record_event(
                            conn, operation=op, status="pending", event_type="transfer.pending",
                            extra=event_extras(op, reason="awaiting_provider"),
                        )
                        applied = True
                        to_status = "pending"
                else:
                    skip_reason = "no_state_change_requested"

                advanced.append(
                    {
                        "stable_operation_ref": key_i,
                        "provider_ref": op["provider_ref"],
                        "from_status": from_status,
                        "to_status": to_status,
                        "applied": applied,
                        "skip_reason": skip_reason,
                        "settled": bool(op["settled"]),
                        "settled_at": op["settled_at"],
                        "last_event_version": int(op["last_event_version"]),
                    }
                )

            if config["emit_callback"]:
                keys = [key] if key is not None else [str(op["stable_operation_ref"]) for op in targets]
                single = len(keys) == 1
                for op_key in keys:
                    emitted = sandbox.emit_pending(
                        conn,
                        stable_operation_ref=op_key,
                        duplicate=int(config["duplicate"]) if single else 0,
                        replay_event_version=config["replay_event_version"] if single else None,
                        descending=config["emit_order"] == "desc",
                    )
                    event_payloads.extend(event_public(e) for e in emitted["events"])
                    delivery_views.extend(delivery_public(d) for d in emitted["deliveries"])
                    duplicates_sent += int(emitted["duplicates_sent"])
                    target_url = str(emitted["target_url"])

        injector.wait()
        body = {
            "provider_id": PROVIDER_ID,
            "status": "ok",
            "advanced": advanced,
            "events": event_payloads,
            "deliveries": delivery_views,
            "duplicates_sent": duplicates_sent,
            "target_url": target_url,
            "observed_at": store_mod.now_iso(),
        }
        if injector.drops_response():
            return dropped({"advanced_count": len(advanced)})
        return JSONResponse(status_code=200, content=body)

    # -- 故障注入 ----------------------------------------------------------

    @app.post("/bank/faults")
    def set_faults(payload: Any = Body(default=None)) -> JSONResponse:
        try:
            config = injector.update(payload if isinstance(payload, Mapping) else {})
        except faults_mod.FaultConfigError as exc:
            return error_response("FAULT_CONFIG_ERROR", details=exc.details or None, message=str(exc))
        return JSONResponse(status_code=200, content={"provider_id": PROVIDER_ID, "status": "ok", "faults": config})

    @app.get("/bank/faults")
    def get_faults() -> JSONResponse:
        return JSONResponse(status_code=200, content={"provider_id": PROVIDER_ID, "status": "ok", "faults": injector.current()})

    @app.delete("/bank/faults")
    def clear_faults() -> JSONResponse:
        return JSONResponse(status_code=200, content={"provider_id": PROVIDER_ID, "status": "ok", "faults": injector.clear()})

    # -- 巡检与健康 --------------------------------------------------------

    @app.get("/bank/operations")
    def list_operations() -> JSONResponse:
        blocked = gate(query=True)
        if blocked is not None:
            return blocked
        with sandbox.read() as conn:
            ops = sandbox.list_operations(conn)
            items = [operation_view(op, sandbox.effects(conn, str(op["stable_operation_ref"]))) for op in ops]
            counts = sandbox.counts(conn)
        return JSONResponse(
            status_code=200,
            content={"provider_id": PROVIDER_ID, "status": "ok", "counts": counts, "operations": items},
        )

    @app.get("/bank/events/{stable_operation_ref}")
    def list_events(stable_operation_ref: str) -> JSONResponse:
        blocked = gate(query=True)
        if blocked is not None:
            return blocked
        with sandbox.read() as conn:
            op = sandbox.get_operation(conn, stable_operation_ref)
            if op is None:
                return error_response("NOT_FOUND", details={"stable_operation_ref": stable_operation_ref})
            events = sandbox.events_for(conn, stable_operation_ref)
        return JSONResponse(
            status_code=200,
            content={
                "provider_id": PROVIDER_ID,
                "stable_operation_ref": stable_operation_ref,
                "provider_ref": op["provider_ref"],
                "events": [event_public(e) for e in events],
            },
        )

    @app.get("/bank/deliveries")
    def list_deliveries(stable_operation_ref: str | None = None, limit: int = 100) -> JSONResponse:
        blocked = gate(query=True)
        if blocked is not None:
            return blocked
        with sandbox.read() as conn:
            rows = sandbox.deliveries(conn, stable_operation_ref=stable_operation_ref, limit=max(1, min(limit, 500)))
        return JSONResponse(
            status_code=200,
            content={
                "provider_id": PROVIDER_ID,
                "status": "ok",
                "callback_secret_source": "PROVIDER_CALLBACK_SECRET",
                "base_url": signing.callback_base_url(),
                "deliveries": [delivery_public(d) for d in rows],
            },
        )

    @app.get("/bank/health")
    def health() -> JSONResponse:
        """健康检查始终可用（不可用故障下也一样），便于熔断器与运维探测。"""
        stats = sandbox.stats()
        return JSONResponse(
            status_code=200,
            content={
                "provider_id": PROVIDER_ID,
                "service": SERVICE_NAME,
                "status": "ok",
                "port": SERVICE_PORT,
                "state": stats,
                "faults": injector.current(),
                "callback_base_url": signing.callback_base_url(),
                "callback_path": f"/internal/provider-events/{PROVIDER_ID}",
                "now": store_mod.now_iso(),
            },
        )

    return app


def default_store() -> "store_mod.SandboxStore":
    return store_mod.SandboxStore(
        store_mod.default_state_path(STATE_ENV, DEFAULT_STATE), provider_id=PROVIDER_ID, prefix=PREFIX
    ).initialize()


_APP: FastAPI | None = None


def get_app() -> FastAPI:
    """模块级单例应用；**导入本模块不产生任何文件系统副作用**。

    uvicorn ``bank:app``、``__init__.load_app("bank")`` 与 ``from bank import app`` 都会经
    PEP 562 的 ``__getattr__`` 走这里，因此状态库只在真正启动时创建。
    """
    global _APP
    if _APP is None:
        _APP = create_app()
    return _APP


def __getattr__(name: str) -> Any:
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":  # pragma: no cover - 手动启动
    import uvicorn

    uvicorn.run(get_app(), host="127.0.0.1", port=SERVICE_PORT)


__all__ = [
    "ADVANCE_FIELDS",
    "DEFAULT_STATE",
    "PROVIDER_ID",
    "SERVICE_PORT",
    "SUBMIT_FIELDS",
    "app",
    "create_app",
    "default_store",
    "error_body",
    "error_response",
    "get_app",
    "operation_view",
    "validate_advance",
    "validate_transfer",
]