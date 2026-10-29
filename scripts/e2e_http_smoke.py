#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FinPilot 六场景真实 HTTP 端到端验收（S1–S6）。

只走真实 HTTP（requests），不使用 FastAPI TestClient；登录后所有业务请求都带
``Authorization: Bearer``。脚本不打印 token / 密码 / TOTP 种子。

断言口径
--------
* 一律用**前后差值 / 相对关系**（账户余额差值、operation 集合、卡申请条数差值、
  单任务内某类 operation 的计数），不写死会随演示库漂移的绝对值。
* 单条用例失败不中断，继续跑完；结束时打印表格并写 JSON 证据。
* 退出码：全部用例 PASS → 0；存在 FAIL → 1。

覆盖矩阵（每场景 5 类基础用例，另加场景特有补充）
-----------------------------------------------
1. 正常路径：构造可执行请求 → ``awaiting_authorization`` 则确认计划（RED 再完成 TOTP
   强验证）→ 等待异步操作落定 → 断言终态、operation、回执/可核验结果。
2. 缺信息：故意省略关键字段 → 断言 ``clarifying`` + ``clarifications`` 非空，
   且**没有产生任何 operation**、账户余额零变化。
3. 否定/条件/冲突：断言停在 ``clarifying``/``blocked``，且没有任何资金效果。
4. 重复提交：同一确认请求发两次 → 断言只产生一个业务效果（operation 集合不增长、
   申请条数不增长、余额不再变化），第二次被拒绝或再次要求强验证。
5. 越权/跨用户：用 ``user_demo_02`` 访问 ``user_demo_01`` 的 task/operation/receipt
   → 断言 403/404 且响应体不泄露归属与内容。

刻意保留的三点口径说明（不是放宽断言，而是按实现语义精确断言）
-------------------------------------------------------------
* **只读场景没有提供方回执**：``bill.report`` / ``wealth.compare`` / ``card.status`` /
  ``subscription.list`` 是 GREEN/YELLOW 的只读查询，不会产生 provider receipt。
  这类用例把 ``operation.domain_result`` 的 ``data`` 作为可核验结果，并单独记一条
  SKIP 说明；有资金/业务效果的场景（S1 转账、S3 申购、S6 商户预订）仍强制要求
  ``receipt`` 或 ``provider_ref``。
* **RED 计划第二次确认**：重复确认时可能返回一个新的 ``challenge_required``（需要重新
  TOTP 的安全门槛），而不是冲突错误。断言接受"拒绝 **或** 重新强验证"，但"不得新增
  operation / 不得产生第二个业务效果"是硬断言。
* **GREEN 计划确认**：``confirm`` 对 GREEN 计划是文档化的空操作（"纯查询计划不需要额外
  确认"），因此 S2 的重复提交用例断言"零新增 operation + 零余额变化"，而不是断言冲突码。

用法::

    python scripts/e2e_http_smoke.py                     # 全量六场景
    python scripts/e2e_http_smoke.py --scene S1          # 只跑 S1
    python scripts/e2e_http_smoke.py --scene S1,S4       # 只跑 S1、S4
    python scripts/e2e_http_smoke.py --out evidence/e2e_http_run.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
API_SRC = REPO_ROOT / "finpilot" / "services" / "api" / "src"

# 允许直接运行：未设置 PYTHONPATH 时补上 API 源码目录（只读复用工程内的种子常量与 TOTP 算法）
try:
    from finpilot.kernel.identity import totp_at
    from finpilot.seed import DEMO_PASSWORD, DEMO_TOTP_SECRET
except ModuleNotFoundError:  # pragma: no cover - 需要工程内置包
    sys.path.insert(0, str(API_SRC))
    from finpilot.kernel.identity import totp_at
    from finpilot.seed import DEMO_PASSWORD, DEMO_TOTP_SECRET

SHANGHAI = timezone(timedelta(hours=8))
OWNER = "user_demo_01"
OTHER = "user_demo_02"

DEFAULT_BASE_URL = "http://127.0.0.1:58000"
SCENES = ("S1", "S2", "S3", "S4", "S5", "S6")

BUSY_OP_STATES = ("dispatching", "pending", "unknown")
TERMINAL_TASK_STATES = ("succeeded", "partially_succeeded", "failed", "cancelled")
TOTP_STEP_SECONDS = 30
# 冲突类错误：重复提交必须被其中一种方式拒绝
CONFLICT_CODES = {
    "VERSION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "POLICY_DENIED",
    "GRANT_INVALID",
    "GRANT_REQUIRED",
    "GRANT_SCOPE_VIOLATION",
    "NOT_REVERSIBLE",
    "CANCEL_NOT_EFFECTIVE",
    "CHALLENGE_FAILED",
}


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1 for ch in str(text))


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _display_width(text))


def _truncate(text: str, width: int) -> str:
    text = str(text)
    if _display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _display_width(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(value, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Case:
    """一条验收用例：请求轨迹 + 断言集合 + 关键字段。"""

    def __init__(self, scene: str, case_id: str, title: str, message: str | None = None):
        self.scene = scene
        self.case_id = case_id
        self.title = title
        self.message = message or ""
        self.requests: list[dict] = []
        self.assertions: list[dict] = []
        self.fields: dict = {}
        self.artifacts: dict = {}
        self.notes: list[str] = []
        self.http_status: int | None = None
        self.task_status: str | None = None
        self.error_code: str | None = None
        self.result = "PASS"
        self.started = time.perf_counter()
        self.elapsed_ms = 0.0

    # -- 记录 --------------------------------------------------------------
    def add_request(self, method: str, path: str, http: int | None, ms: float,
                    note: str = "", error: str | None = None) -> None:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "http": http,
                "elapsed_ms": round(ms, 1),
                "note": note,
                "error": error,
            }
        )
        if http is not None and self.http_status is None:
            self.http_status = http

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.assertions.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    def skip(self, name: str, detail: str) -> None:
        self.assertions.append({"name": name, "ok": None, "detail": str(detail)})

    def note(self, text: str) -> None:
        self.notes.append(text)

    def field(self, key: str, value) -> None:
        self.fields[key] = value

    # -- 收尾 --------------------------------------------------------------
    def finish(self) -> None:
        self.elapsed_ms = round((time.perf_counter() - self.started) * 1000, 1)
        failed = [a for a in self.assertions if a["ok"] is False]
        self.result = "FAIL" if failed else "PASS"

    @property
    def failures(self) -> list[dict]:
        return [a for a in self.assertions if a["ok"] is False]

    def as_dict(self) -> dict:
        return {
            "scene": self.scene,
            "case": self.case_id,
            "title": self.title,
            "message": self.message,
            "result": self.result,
            "http_status": self.http_status,
            "task_status": self.task_status,
            "error_code": self.error_code,
            "key_fields": self.fields,
            "assertions": self.assertions,
            "failed_assertions": [a["name"] for a in self.failures],
            "skipped_assertions": [a["name"] for a in self.assertions if a["ok"] is None],
            "requests": self.requests,
            "elapsed_ms": self.elapsed_ms,
            "notes": self.notes,
        }


class ApiFailure(RuntimeError):
    """请求层失败（连接错误/超时），单条用例记 FAIL 但不中断全量。"""


# --------------------------------------------------------------------------------------
# HTTP 客户端
# --------------------------------------------------------------------------------------
class Http:
    def __init__(self, base_url: str, *, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
        case: Case | None = None,
        note: str = "",
        record: bool = True,
    ) -> tuple[int, dict, float]:
        url = self.base_url + path
        started = time.perf_counter()
        try:
            response = self.session.request(
                method, url, json=body, headers=headers, timeout=timeout or self.timeout
            )
        except requests.RequestException as exc:  # 网络层失败
            elapsed = (time.perf_counter() - started) * 1000
            if case is not None and record:
                case.add_request(method, path, None, elapsed, note, error=type(exc).__name__)
            raise ApiFailure(f"{method} {path} 请求失败：{type(exc).__name__}") from exc
        elapsed = (time.perf_counter() - started) * 1000
        try:
            payload = response.json()
        except ValueError:
            payload = {"_unparsed_body": response.text[:200]}
        if not isinstance(payload, dict):
            payload = {"_unexpected_payload": payload}
        error_code = (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None
        if case is not None and record:
            case.add_request(method, path, response.status_code, elapsed, note, error=error_code)
        return response.status_code, payload, elapsed


def envelope_ok(payload: dict) -> bool:
    return payload.get("status") == "ok" and payload.get("error") is None


def error_code(payload: dict) -> str | None:
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) else None


def data_of(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------------------
# 验收运行器
# --------------------------------------------------------------------------------------
class Runner:
    def __init__(self, base_url: str, scene_filter: list[str] | None = None):
        self.http = Http(base_url)
        self.base_url = base_url.rstrip("/")
        self.scene_filter = scene_filter
        self.cases: list[Case] = []
        self.h1: dict = {}
        self.h2: dict = {}
        self.artifacts: dict = {}
        self.preflight_error: str | None = None
        self._last_counter: int | None = None
        self.started_at = _now_utc()

    # -- 基础请求 ----------------------------------------------------------
    def login(self, user_id: str) -> dict:
        code, payload, _ = self.http.call(
            "POST",
            "/v1/auth/sessions",
            body={"user_id": user_id, "password": DEMO_PASSWORD, "channel": "app"},
            timeout=30,
        )
        if code != 200 or not envelope_ok(payload):
            raise ApiFailure(f"登录失败 user={user_id} http={code} code={error_code(payload)}")
        token = data_of(payload).get("token")
        if not token:
            raise ApiFailure(f"登录响应缺少 token user={user_id}")
        return {"Authorization": f"Bearer {token}"}

    def _create(self, case: Case, message: str, *, headers: dict | None = None, channel: str = "app"):
        code, payload, _ = self.http.call(
            "POST",
            "/v1/tasks",
            body={"message": message, "channel": channel},
            headers=headers or self.h1,
            timeout=120,
            case=case,
            note=message,
        )
        return code, payload, data_of(payload)

    def _task(self, case: Case, task_id: str, headers: dict | None = None, *, record: bool = True) -> dict:
        code, payload, _ = self.http.call(
            "GET", f"/v1/tasks/{task_id}", headers=headers or self.h1, timeout=45, case=case,
            note=f"task {task_id}", record=record,
        )
        return data_of(payload)

    def _settle(self, case: Case, task_id: str, headers: dict | None = None, *, timeout: float = 15.0) -> dict:
        """等待异步操作落定（提供方回执由 outbox worker 异步处理）。"""
        headers = headers or self.h1
        deadline = time.time() + timeout
        polls = 0
        data: dict = {}
        while True:
            code, payload, _ = self.http.call(
                "GET", f"/v1/tasks/{task_id}", headers=headers, timeout=45, record=False
            )
            data = data_of(payload)
            ops = data.get("operations") or []
            busy = any(op.get("status") in BUSY_OP_STATES for op in ops) or data.get("status") in ("ready", "running")
            polls += 1
            if not busy or time.time() >= deadline:
                break
            time.sleep(1.5)
        code, payload, _ = self.http.call(
            "GET", f"/v1/tasks/{task_id}", headers=headers, timeout=45, case=case,
            note=f"轮询 {polls} 次后读取任务",
        )
        data = data_of(payload)
        data["_polls"] = polls
        case.field("task_status", data.get("status"))
        return data

    def _confirm(self, case: Case, created: dict, *, headers: dict | None = None, note: str = "confirm"):
        plan = created.get("plan") or {}
        body = {"scope_hash": plan.get("scope_hash"), "expected_revision": created.get("revision")}
        return self.http.call(
            "POST", f"/v1/plans/{plan.get('plan_id')}/confirm", body=body,
            headers=headers or self.h1, timeout=240, case=case, note=note,
        )

    def _verify_totp(self, case: Case, challenge_id: str, *, headers: dict | None = None):
        """完成 TOTP 强验证。

        服务端对同一时间步的一次性口令做重放防护（10 分钟回看），所以同一进程里连续两次
        强验证必须落在不同的 30 秒窗口；这里按真实验证器的行为等待下一个窗口再取码。
        """
        headers = headers or self.h1
        last_payload: dict = {}
        for attempt in range(1, 4):
            code_text = self._totp_code()
            http_status, payload, _ = self.http.call(
                "POST", f"/v1/auth/challenges/{challenge_id}/verify", body={"factor_response": code_text},
                headers=headers, timeout=240, case=case, note=f"TOTP 强验证（第 {attempt} 次）",
            )
            last_payload = payload
            if envelope_ok(payload) or error_code(payload) != "CHALLENGE_FAILED":
                return http_status, payload
            case.note(
                f"第 {attempt} 次 TOTP 被拒（CHALLENGE_FAILED）：同一时间步的口令不可复用，"
                "等待下一个口令窗口后重试"
            )
            # 强制下一次取码落在新的时间步
            self._last_counter = int(_now_utc().timestamp()) // TOTP_STEP_SECONDS
        return 403, last_payload

    def _totp_code(self) -> str:
        now = _now_utc()
        counter = int(now.timestamp()) // TOTP_STEP_SECONDS
        if self._last_counter is not None and counter <= self._last_counter:
            wait = (self._last_counter + 1) * TOTP_STEP_SECONDS - now.timestamp() + 0.8
            if wait > 0:
                time.sleep(wait)
            now = _now_utc()
        code_text, used_counter = totp_at(DEMO_TOTP_SECRET, now)
        self._last_counter = used_counter
        return code_text

    def _authorize(self, case: Case, created: dict, *, headers: dict | None = None, timeout: float = 15.0) -> dict:
        """确认计划（必要时完成 TOTP），再等待异步落定。返回执行轨迹与终态任务。"""
        headers = headers or self.h1
        plan = created.get("plan") or {}
        trace: dict = {
            "plan_id": plan.get("plan_id"),
            "scope_hash": plan.get("scope_hash"),
            "required_level": plan.get("required_level"),
            "challenge_required": False,
            "confirm_http": None,
            "confirm_error": None,
            "verify_ok": None,
        }
        confirm_code, confirm_payload, _ = self._confirm(case, created, headers=headers, note="确认计划")
        trace["confirm_http"] = confirm_code
        trace["confirm_error"] = error_code(confirm_payload)
        confirm_data = data_of(confirm_payload)
        if envelope_ok(confirm_payload) and confirm_data.get("status") == "challenge_required":
            trace["challenge_required"] = True
            challenge_id = confirm_data.get("challenge_id")
            if challenge_id:
                verify_code, verify_payload = self._verify_totp(case, challenge_id, headers=headers)
                trace["verify_http"] = verify_code
                trace["verify_ok"] = envelope_ok(verify_payload)
                trace["verify_error"] = error_code(verify_payload)
                trace["verify_execution"] = _short(data_of(verify_payload).get("execution") or {})
            else:
                trace["verify_ok"] = False
                trace["verify_error"] = "challenge_id_missing"
        trace["task"] = self._settle(case, created.get("task_id"), headers=headers, timeout=timeout)
        return trace

    def _op_view(self, case: Case, operation_id: str, *, headers: dict | None = None, record: bool = True) -> dict:
        code, payload, _ = self.http.call(
            "GET", f"/v1/operations/{operation_id}", headers=headers or self.h1, timeout=45,
            case=case, note=f"operation {operation_id}", record=record,
        )
        return data_of(payload)

    def _cards(self, *, headers: dict | None = None, case: Case | None = None, note: str = "") -> dict:
        code, payload, _ = self.http.call("GET", "/v1/cards", headers=headers or self.h1, timeout=45, case=case, note=note)
        return data_of(payload)

    def _ledger(self, *, headers: dict | None = None, case: Case | None = None, note: str = "") -> dict:
        """账户余额快照：只取差值用，记录在用例中便于复核。"""
        code, payload, _ = self.http.call("GET", "/v1/accounts", headers=headers or self.h1, timeout=45, case=case, note=note)
        data = data_of(payload)
        snapshot = {}
        for account in data.get("accounts") or []:
            snapshot[account.get("account_id")] = {
                "posted": account.get("posted_balance_minor"),
                "spendable": account.get("spendable_minor"),
                "reserved": account.get("reserved_minor"),
            }
        return snapshot

    def _operations_total(self, *, headers: dict | None = None) -> int:
        code, payload, _ = self.http.call("GET", "/v1/operations?limit=200", headers=headers or self.h1, timeout=45)
        return len(data_of(payload).get("items") or [])

    # -- 通用断言 ----------------------------------------------------------
    def _assert_no_effect(self, case: Case, before: dict, *, note: str = "本用例前后") -> None:
        after = self._ledger(case=case, note=f"余额快照（{note}）")
        deltas = {}
        for account_id, snapshot in before.items():
            after_posted = (after.get(account_id) or {}).get("posted")
            deltas[account_id] = (
                None if (after_posted is None or snapshot.get("posted") is None) else after_posted - snapshot["posted"]
            )
        case.field("balance_delta_minor", deltas)
        case.check(
            "账户余额零变化（无资金效果）",
            all(value == 0 for value in deltas.values() if value is not None),
            f"delta={deltas}",
        )

    def _assert_no_operations(self, case: Case, task: dict, baseline_total: int | None) -> None:
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("任务上没有产生任何 operation", len(ops) == 0, f"operations={[op.get('tool') for op in ops]}")
        if baseline_total is not None:
            total = self._operations_total()
            case.field("global_operation_delta", total - baseline_total)
            case.check(
                "全局 operation 计数未增长",
                total == baseline_total,
                f"before={baseline_total}, after={total}",
            )

    def _assert_blocked_or_clarifying(self, case: Case, task: dict) -> None:
        status = task.get("status")
        case.field("task_status", status)
        case.check(
            "任务停在 clarifying/blocked（未进入可执行或已执行状态）",
            status in ("clarifying", "blocked"),
            f"实际 status={status}, blocked_reason={task.get('blocked_reason')}",
        )

    def _assert_no_effect_operation(self, case: Case, task: dict) -> None:
        """资金/业务效果类 operation 不得出现（含失败态也算产生过写入意图）。"""
        ops = task.get("operations") or []
        effects = [op for op in ops if op.get("tool") in ("transfer.submit", "merchant.book", "wealth.purchase")]
        case.field("effect_operations", [op.get("tool") for op in effects])
        case.check("没有资金类 operation", not effects, f"effects={[op.get('tool') for op in effects]}")

    def _assert_conflict_no_effect(self, case: Case, task: dict, before: dict) -> None:
        """否定/条件/冲突用例：不得产生资金效果（只读查询类 operation 不算资金效果）。"""
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.field("operation_tools", [op.get("tool") for op in ops])
        self._assert_no_effect_operation(case, task)
        self._assert_no_effect(case, before, note="否定/冲突用例")

    def _evidence_of_effect(self, case: Case, op_view: dict) -> None:
        receipt = op_view.get("receipt") or {}
        provider_ref = op_view.get("provider_order_ref")
        case.field("operation_status", op_view.get("status"))
        case.field("operation_error_code", op_view.get("error_code"))
        case.field("provider_order_ref", provider_ref)
        case.field("receipt_id", receipt.get("receipt_id"))
        case.check(
            "资金/业务效果有回执（receipt 或 provider_ref）",
            bool(receipt.get("receipt_id")) or bool(provider_ref),
            f"receipt={bool(receipt.get('receipt_id'))}, provider_ref={provider_ref}, "
            f"op_status={op_view.get('status')}, error_code={op_view.get('error_code')}",
        )

    def _evidence_of_read(self, case: Case, op_view: dict) -> None:
        domain_result = op_view.get("domain_result") if isinstance(op_view.get("domain_result"), dict) else {}
        payload = domain_result.get("data") if isinstance(domain_result.get("data"), dict) else {}
        # 领域处理器的返回有两种形态：{type,data,...} 包装结构，或（订阅查询）平铺结构
        shape = "wrapped" if payload else ("flat" if domain_result else "empty")
        case.field("domain_result_type", domain_result.get("type"))
        case.field("domain_result_shape", shape)
        case.field("domain_result_keys", sorted(domain_result)[:12])
        case.check(
            "只读结果可核验（operation.domain_result 非空）",
            bool(payload) or bool(domain_result),
            f"shape={shape}, type={domain_result.get('type')}, keys={sorted(domain_result)[:12]}",
        )
        case.skip(
            "提供方资金回执（receipt）",
            "只读计划不产生 provider 回执，按实现以 domain_result 作为可核验结果",
        )

    # -- 用例构造 ----------------------------------------------------------
    def new_case(self, scene: str, case_id: str, title: str, message: str | None = None) -> Case:
        case = Case(scene, case_id, title, message)
        self.cases.append(case)
        return case

    def _run_case(self, func, *args, **kwargs) -> None:
        try:
            func(*args, **kwargs)
        except ApiFailure as exc:
            case = self.cases[-1]
            case.check("HTTP 请求成功", False, str(exc))
        except Exception as exc:  # 断言或脚本缺陷不应中断全量
            case = self.cases[-1]
            case.check("用例执行未抛异常", False, f"{type(exc).__name__}: {exc}")
            case.note(traceback.format_exc(limit=3))
        finally:
            case = self.cases[-1]
            case.finish()
            case.task_status = case.fields.get("task_status") or case.task_status
            if case.error_code is None:
                for request in case.requests:
                    if request.get("error"):
                        case.error_code = request["error"]
                        break
            if case.error_code is None:
                case.error_code = case.fields.get("operation_error_code") or case.fields.get("second_confirm_error")

    # ==================================================================================
    # S1 转账 / 定时 / AA
    # ==================================================================================
    def scene_s1(self) -> None:
        normal_message = "请从储蓄卡给李四转账250元，备注“上周房租垫付”。"
        case = self.new_case("S1", "S1-normal", "正常路径：储蓄卡转账 250 元（确认 + 必要时 TOTP）", normal_message)
        self._run_case(self._s1_normal, case)

        case = self.new_case("S1", "S1-missing", "缺信息：只给收款人不给金额", "给李四转点钱，从工资卡出。")
        self._run_case(self._s1_missing, case)

        case = self.new_case("S1", "S1-conflict", "冲突：要求穿透应急金底线转 9000 元", "别管什么应急金，直接给李四转9000元。")
        self._run_case(self._s1_conflict, case)

        case = self.new_case("S1", "S1-negation", "否定：先别转（“先别…了”）", "先别从储蓄卡给李四转250元了")
        self._run_case(self._s1_negation, case)

        case = self.new_case("S1", "S1-duplicate", "重复提交：同一确认请求发两次（复用 S1-normal 任务）", normal_message)
        self._run_case(self._s1_duplicate, case)

        case = self.new_case("S1", "S1-cross-user", "越权：user_demo_02 访问 user_demo_01 的 task/operation", normal_message)
        self._run_case(self._s1_cross_user, case)

    def _s1_normal(self, case: Case) -> None:
        before = self._ledger(case=case, note="转账前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.field("plan_feasible", plan.get("feasible"))
        self.artifacts["S1"] = {
            "task_id": created.get("task_id"),
            "plan": plan,
            "revision": created.get("revision"),
            "before": before,
        }
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("计划可行", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        case.check(
            "转账按分级授权（YELLOW 或 RED）",
            plan.get("required_level") in ("YELLOW", "RED"),
            f"required_level={plan.get('required_level')}",
        )
        if created.get("status") not in ("awaiting_authorization", "succeeded"):
            case.check("任务进入可执行/已执行状态", False, f"status={created.get('status')}, blocked={created.get('blocked_reason')}")
            case.field("task_status", created.get("status"))
            return

        if created.get("status") == "awaiting_authorization":
            trace = self._authorize(case, created)
        else:  # GREEN 直接执行（本实现里转账不会走到这里）
            trace = {"task": self._settle(case, created.get("task_id")), "challenge_required": False, "confirm_http": None}
        task = trace.get("task") or {}
        case.field("challenge_required", trace.get("challenge_required"))
        case.field("confirm_http", trace.get("confirm_http"))
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.field("operation_statuses", [op.get("status") for op in ops])
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}, result={_short(task.get('result') or {})}")
        transfers = [op for op in ops if op.get("tool") == "transfer.submit"]
        case.check("恰好一个转账 operation", len(transfers) == 1, f"tools={[op.get('tool') for op in ops]}")
        case.check(
            "转账 operation 状态 succeeded",
            bool(transfers) and all(op.get("status") == "succeeded" for op in transfers),
            f"statuses={[op.get('status') for op in transfers]}",
        )
        if transfers:
            op_view = self._op_view(case, transfers[0]["operation_id"])
            self._evidence_of_effect(case, op_view)
            case.field("operation_amount_minor", op_view.get("amount_minor"))
            case.check("转账金额 25000 分", op_view.get("amount_minor") == 25000, f"amount_minor={op_view.get('amount_minor')}")
            if op_view.get("receipt"):
                self.artifacts["S1"]["receipt_id"] = (op_view.get("receipt") or {}).get("receipt_id")
            self.artifacts["S1"]["operation_id"] = transfers[0]["operation_id"]

        after = self._ledger(case=case, note="转账后余额")
        delta = (after.get("account_savings", {}).get("posted") or 0) - (before.get("account_savings", {}).get("posted") or 0)
        case.field("savings_delta_minor", delta)
        case.check("储蓄卡余额恰好减少 25000 分（只发生一次）", delta == -25000, f"delta={delta}")
        self.artifacts["S1"]["task"] = task

    def _s1_missing(self, case: Case) -> None:
        before = self._ledger(case=case, note="缺信息用例前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.field("clarification_fields", [c.get("field") for c in task.get("clarifications") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务状态 clarifying", task.get("status") == "clarifying", f"status={task.get('status')}")
        case.check("clarifications 非空", bool(task.get("clarifications")), f"fields={[c.get('field') for c in task.get('clarifications') or []]}")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect_operation(case, task)
        self._assert_no_effect(case, before, note="缺信息用例")

    def _s1_conflict(self, case: Case) -> None:
        before = self._ledger(case=case, note="冲突用例前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("task_status", created.get("status"))
        case.field("plan_feasible", plan.get("feasible"))
        case.field("infeasible_reasons", plan.get("infeasible_reasons"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        task = self._task(case, created.get("task_id") or "")
        self._assert_conflict_no_effect(case, task, before)

    def _s1_negation(self, case: Case) -> None:
        before = self._ledger(case=case, note="否定用例前余额")
        code, payload, created = self._create(case, case.message)
        case.field("task_id", created.get("task_id"))
        case.field("task_status", created.get("status"))
        case.field("compiled_goals", (created.get("goal") or {}).get("goals"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        task = self._task(case, created.get("task_id") or "")
        self._assert_conflict_no_effect(case, task, before)

    def _s1_duplicate(self, case: Case) -> None:
        artifact = self.artifacts.get("S1") or {}
        if not artifact.get("task_id"):
            case.skip("重复提交依赖 S1-normal 的任务", "S1-normal 未产出可复用任务")
            return
        case.field("task_id", artifact.get("task_id"))
        before_task = self._task(case, artifact["task_id"])
        ops_before = {op.get("operation_id") for op in (before_task.get("operations") or [])}
        balances_before = self._ledger(case=case, note="重复确认前余额")
        created = {"task_id": artifact["task_id"], "plan": artifact.get("plan") or {}, "revision": artifact.get("revision")}
        code, payload, _ = self._confirm(case, created, note="第二次确认（重复提交）")
        second_error = error_code(payload)
        second_data = data_of(payload)
        re_challenge = envelope_ok(payload) and second_data.get("status") == "challenge_required"
        rejected = (not envelope_ok(payload)) and second_error in CONFLICT_CODES
        case.field("second_confirm_http", code)
        case.field("second_confirm_error", second_error)
        case.field("second_confirm_rechallenge", re_challenge)
        case.check(
            "第二次确认被拒绝（冲突类错误码）或重新要求强验证",
            bool(rejected or re_challenge),
            f"http={code}, code={second_error}, data_status={second_data.get('status')}",
        )
        if not rejected:
            case.note(
                "重复确认未返回冲突错误码，而是新的 TOTP 挑战或空操作；已单独断言“不得新增 operation / 不得二次扣款”"
            )
        task_after = self._settle(case, artifact["task_id"], timeout=4.0)
        ops_after = {op.get("operation_id") for op in (task_after.get("operations") or [])}
        case.field("operation_count_after", len(ops_after))
        case.check("重复确认没有新增 operation", ops_after == ops_before, f"before={sorted(ops_before)}, after={sorted(ops_after)}")
        transfers = [op for op in (task_after.get("operations") or []) if op.get("tool") == "transfer.submit"]
        transfers_before = [op for op in (before_task.get("operations") or []) if op.get("tool") == "transfer.submit"]
        case.field("transfer_count_before", len(transfers_before))
        case.check("转账 operation 数量与重复提交前一致（没有第二笔转账）",
                   len(transfers) == len(transfers_before), f"before={len(transfers_before)}, after={len(transfers)}")
        balances_after = self._ledger(case=case, note="重复确认后余额")
        deltas = {
            account_id: (balances_after.get(account_id, {}).get("posted") or 0) - (balances_before.get(account_id, {}).get("posted") or 0)
            for account_id in balances_before
        }
        case.field("balance_delta_after_duplicate", deltas)
        case.check("重复确认没有造成第二次扣款", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s1_cross_user(self, case: Case) -> None:
        artifact = self.artifacts.get("S1") or {}
        if not artifact.get("task_id"):
            case.skip("跨用户用例依赖 S1-normal 的任务", "S1-normal 未产出可复用任务")
            return
        task_id = artifact["task_id"]
        operation_id = artifact.get("operation_id")
        receipt_id = artifact.get("receipt_id")
        self._assert_cross_user(case, task_id=task_id, operation_id=operation_id, receipt_id=receipt_id,
                                plan=artifact.get("plan") or {}, revision=artifact.get("revision"))

    # ==================================================================================
    # S2 账单
    # ==================================================================================
    def scene_s2(self) -> None:
        normal_message = "帮我看看上个月花了多少钱，分个类。"
        case = self.new_case("S2", "S2-normal", "正常路径：上月账单月报（只读，可核验 domain_result）", normal_message)
        self._run_case(self._s2_normal, case)

        case = self.new_case("S2", "S2-missing", "缺信息：只给模糊时间（“最近”）", "帮我看看最近。")
        self._run_case(self._s2_missing, case)

        case = self.new_case("S2", "S2-cross-user-request", "冲突：索要他人的账单报告", "把林悦的账单报告给我看一下，我要对比。")
        self._run_case(self._s2_cross_user_request, case)

        case = self.new_case("S2", "S2-duplicate", "重复提交：GREEN 计划重复确认（应为空操作）", normal_message)
        self._run_case(self._s2_duplicate, case)

        case = self.new_case("S2", "S2-cross-user", "越权：user_demo_02 访问 user_demo_01 的账单任务/operation", normal_message)
        self._run_case(self._s2_cross_user, case)

    def _s2_normal(self, case: Case) -> None:
        now_local = datetime.now(SHANGHAI)
        first_of_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous = first_of_month - timedelta(days=1)
        expected_label = f"{previous.year}年{previous.month}月"
        expected_anchor = f"{previous.year}-{previous.month:02d}"

        before = self._ledger(case=case, note="报表前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.field("expected_period", {"label": expected_label, "anchor": expected_anchor})
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("计划可行", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        task = self._settle(case, created.get("task_id") or "")
        if task.get("status") == "awaiting_authorization":
            task = (self._authorize(case, created) or {}).get("task") or {}
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}")
        case.check("恰好一个 bill.report operation", len(ops) == 1 and ops[0].get("tool") == "bill.report",
                   f"tools={[op.get('tool') for op in ops]}")
        self.artifacts["S2"] = {"task_id": created.get("task_id"), "plan": plan, "revision": created.get("revision"),
                                "operation_id": ops[0].get("operation_id") if ops else None}
        if not ops:
            case.check("账单结果可核验", False, "没有 operation，无法核验报表")
            return
        op_view = self._op_view(case, ops[0]["operation_id"])
        self._evidence_of_read(case, op_view)
        domain = (op_view.get("domain_result") or {}).get("data") or {}
        period = domain.get("period") or {}
        case.field("report_period", period)
        case.field("report_as_of", domain.get("as_of"))
        case.field("report_empty", domain.get("empty"))
        case.check("报表账期与请求一致（上月）", period.get("label") == expected_label and period.get("anchor") == expected_anchor,
                   f"label={period.get('label')}, anchor={period.get('anchor')}, expected={expected_label}/{expected_anchor}")
        case.check("报表注明数据截至时间", bool(domain.get("as_of")), f"as_of={domain.get('as_of')}")
        case.check("账期边界口径明确", period.get("boundary") == "start_closed_end_open", f"boundary={period.get('boundary')}")
        categories = ((domain.get("consumption") or {}).get("categories")) or []
        case.check("按分类汇总返回分类明细", isinstance(categories, list) and len(categories) > 0, f"categories={len(categories)}")
        if domain.get("empty"):
            warning_codes = [item.get("code") for item in domain.get("warnings") or []]
            case.check("空账期如实标注（不编造金额）", "empty_period" in warning_codes, f"warnings={warning_codes}")
        after = self._ledger(case=case, note="报表后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("只读报表没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s2_missing(self, case: Case) -> None:
        before = self._ledger(case=case, note="缺信息前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.field("clarification_fields", [c.get("field") for c in task.get("clarifications") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务状态 clarifying", task.get("status") == "clarifying", f"status={task.get('status')}")
        case.check("clarifications 非空", bool(task.get("clarifications")), f"fields={[c.get('field') for c in task.get('clarifications') or []]}")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect(case, before, note="缺信息用例")

    def _s2_cross_user_request(self, case: Case) -> None:
        before = self._ledger(case=case, note="索要他人账单前余额")
        code, payload, created = self._create(case, case.message)
        case.field("task_id", created.get("task_id"))
        case.field("task_status", created.get("status"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        task = self._task(case, created.get("task_id") or "")
        self._assert_conflict_no_effect(case, task, before)
        # 即便被当作自身请求执行，也不得返回他人标识
        ops = task.get("operations") or []
        blob = json.dumps({"goal": task.get("goal"), "result": task.get("result")}, ensure_ascii=False)
        if ops:
            op_view = self._op_view(case, ops[0]["operation_id"])
            blob += json.dumps(op_view.get("domain_result"), ensure_ascii=False)
        leaked = [token for token in ("林悦", "user_demo_02", "user_demo_03") if token in blob]
        case.field("leaked_identifiers", leaked)
        case.check("响应未包含他人身份标识或数据", not leaked, f"leaked={leaked}")

    def _s2_duplicate(self, case: Case) -> None:
        artifact = self.artifacts.get("S2") or {}
        if not artifact.get("task_id"):
            case.skip("重复提交依赖 S2-normal 的任务", "S2-normal 未产出可复用任务")
            return
        case.field("task_id", artifact["task_id"])
        before_task = self._task(case, artifact["task_id"])
        ops_before = {op.get("operation_id") for op in (before_task.get("operations") or [])}
        balances_before = self._ledger(case=case, note="重复确认前余额")
        created = {"task_id": artifact["task_id"], "plan": artifact.get("plan") or {}, "revision": artifact.get("revision")}
        code1, payload1, _ = self._confirm(case, created, note="确认 #1")
        code2, payload2, _ = self._confirm(case, created, note="确认 #2（重复提交）")
        data1, data2 = data_of(payload1), data_of(payload2)
        case.field("confirm1_http", code1)
        case.field("confirm2_http", code2)
        case.field("confirm2_level", data2.get("level"))
        case.check(
            "GREEN 计划两次确认都是空操作（不重复执行报表）",
            envelope_ok(payload1) and envelope_ok(payload2) and data1.get("level") == "GREEN" and data2.get("level") == "GREEN",
            f"http1={code1}, http2={code2}, level1={data1.get('level')}, level2={data2.get('level')}, note={_short(data2.get('note'))}",
        )
        case.note("GREEN 计划无需确认，confirm 是文档化空操作；因此以“零新增 operation、零余额变化”作为重复提交断言")
        task_after = self._settle(case, artifact["task_id"], timeout=3.0)
        ops_after = {op.get("operation_id") for op in (task_after.get("operations") or [])}
        case.field("operation_count_after", len(ops_after))
        case.check("重复确认没有新增 operation", ops_after == ops_before, f"before={sorted(ops_before)}, after={sorted(ops_after)}")
        balances_after = self._ledger(case=case, note="重复确认后余额")
        deltas = {k: (balances_after.get(k, {}).get("posted") or 0) - (balances_before.get(k, {}).get("posted") or 0) for k in balances_before}
        case.field("balance_delta_after_duplicate", deltas)
        case.check("重复确认没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s2_cross_user(self, case: Case) -> None:
        artifact = self.artifacts.get("S2") or {}
        if not artifact.get("task_id"):
            case.skip("跨用户用例依赖 S2-normal 的任务", "S2-normal 未产出可复用任务")
            return
        self._assert_cross_user(case, task_id=artifact["task_id"], operation_id=artifact.get("operation_id"),
                                receipt_id=None, plan=artifact.get("plan") or {}, revision=artifact.get("revision"))

    # ==================================================================================
    # S3 理财
    # ==================================================================================
    def scene_s3(self) -> None:
        case = self.new_case("S3", "S3-normal", "正常路径：产品比较（只读，含预算）", "比较理财产品，预算1000元")
        self._run_case(self._s3_normal, case)

        case = self.new_case("S3", "S3-assessment", "风险测评：问卷提交出具测评，对话请求给出问卷指引",
                             "我要为子女教育金做风险测评，问卷至少覆盖投资经验、损失承受能力、风险态度、期限、流动性和资金用途。")
        self._run_case(self._s3_assessment, case)

        case = self.new_case("S3", "S3-purchase", "申购：储蓄卡申购活期宝 100 元（RED 强验证）", "用储蓄卡申购活期宝100元")
        self._run_case(self._s3_purchase, case)

        case = self.new_case("S3", "S3-missing", "缺信息：不给产品与金额", "我想买点理财，产品你随便挑。")
        self._run_case(self._s3_missing, case)

        case = self.new_case("S3", "S3-conflict", "冲突：自述 R2 却要直接买 R4 产品", "我的等级是R2，但我要买成长精选权益4号（R4），直接买。")
        self._run_case(self._s3_conflict, case)

        case = self.new_case("S3", "S3-duplicate", "重复提交：同一份问卷答案提交两次", "风险测评确认重复提交")
        self._run_case(self._s3_duplicate, case)

        case = self.new_case("S3", "S3-cross-user", "越权：user_demo_02 访问 user_demo_01 的理财任务/operation", "比较理财产品，预算1000元")
        self._run_case(self._s3_cross_user, case)

    def _s3_normal(self, case: Case) -> None:
        before = self._ledger(case=case, note="比较前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("计划可行", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        task = self._settle(case, created.get("task_id") or "")
        if task.get("status") == "awaiting_authorization":
            task = (self._authorize(case, created) or {}).get("task") or {}
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}")
        case.check("恰好一个 wealth.compare operation", len(ops) == 1 and ops[0].get("tool") == "wealth.compare",
                   f"tools={[op.get('tool') for op in ops]}")
        self.artifacts["S3"] = {"task_id": created.get("task_id"), "plan": plan, "revision": created.get("revision"),
                                "operation_id": ops[0].get("operation_id") if ops else None}
        if not ops:
            case.check("比较结果可核验", False, "没有 operation，无法核验比较结果")
            return
        op_view = self._op_view(case, ops[0]["operation_id"])
        self._evidence_of_read(case, op_view)
        domain = (op_view.get("domain_result") or {}).get("data") or {}
        products = domain.get("products") or []
        case.field("compared_products", len(products))
        case.check("返回产品比较明细", len(products) > 0, f"products={len(products)}")
        eligible = domain.get("eligible_count")
        excluded = domain.get("excluded_count")
        case.check("可投/不可投计数与明细一致", isinstance(eligible, int) and isinstance(excluded, int) and eligible + excluded == len(products),
                   f"eligible={eligible}, excluded={excluded}, products={len(products)}")
        after = self._ledger(case=case, note="比较后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("只读比较没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s3_assessment(self, case: Case) -> None:
        """风险测评是问卷式写入：逐题作答 → 服务端评分；对话请求只能给出问卷指引。

        2026-10-29 起后端把"测评"从可执行工具改为问卷入口：
        GET /v1/wealth/questionnaire 取题，POST /v1/wealth/assessment 提交；
        对话里说"帮我做测评"会得到 ASSESSMENT_REQUIRES_QUESTIONNAIRE 的阻断与指引，
        不再生成 answers={} 的注定失败计划。
        """
        before = self._ledger(case=case, note="测评前余额")

        code, payload, _ = self.http.call(
            "GET", "/v1/wealth/questionnaire", headers=self.h1, timeout=30, case=case, note="取风险测评问卷"
        )
        questionnaire = data_of(payload)
        questions = questionnaire.get("questions") or []
        case.field("questionnaire_version", questionnaire.get("questionnaire_version"))
        case.field("question_count", len(questions))
        case.check(
            "问卷可获取且含 6 道题",
            code == 200 and envelope_ok(payload) and len(questions) == 6,
            f"http={code}, questions={len(questions)}, code={error_code(payload)}",
        )
        if len(questions) != 6:
            return

        answers = {item["question_id"]: item["options"][2]["option_id"] for item in questions}
        code, payload, _ = self.http.call(
            "POST", "/v1/wealth/assessment", body={"answers": answers}, headers=self.h1, timeout=30,
            case=case, note="提交问卷答案",
        )
        submitted = data_of(payload).get("data") or {}
        level = submitted.get("risk_level")
        case.field("risk_level", level)
        case.field("score", submitted.get("score"))
        case.field("expires_at", submitted.get("expires_at"))
        case.check(
            "提交问卷后出具有效测评（等级 + 有效期）",
            code == 200 and envelope_ok(payload) and level in {"R1", "R2", "R3", "R4", "R5"} and bool(submitted.get("expires_at")),
            f"http={code}, level={level}, expires_at={submitted.get('expires_at')}, code={error_code(payload)}",
        )
        profile_id = submitted.get("profile_id")
        case.field("profile_id", profile_id)

        code2, payload2, _ = self.http.call(
            "POST", "/v1/wealth/assessment", body={"answers": answers}, headers=self.h1, timeout=30,
            case=case, note="重复提交同一份答案",
        )
        again = data_of(payload2).get("data") or {}
        case.check(
            "同一份答案重复提交幂等（同一条测评）",
            code2 == 200 and again.get("profile_id") == profile_id,
            f"http={code2}, first={profile_id}, second={again.get('profile_id')}",
        )

        code3, payload3, _ = self.http.call(
            "GET", "/v1/wealth/assessment", headers=self.h1, timeout=30, case=case, note="读回测评档案"
        )
        view = data_of(payload3)
        case.check(
            "测评档案可读回且等级一致",
            code3 == 200 and bool(view) and view.get("level") == level,
            f"http={code3}, level={view.get('level') if isinstance(view, dict) else view}",
        )

        # 对话请求：必须被明确阻断并指向问卷，而不是生成空答案计划
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("chat_task_id", created.get("task_id"))
        case.field("chat_task_status", created.get("status"))
        case.field("chat_infeasible_reasons", plan.get("infeasible_reasons"))
        reasons = plan.get("infeasible_reasons") or []
        case.check(
            "对话请求停在 blocked 并给出问卷指引",
            code == 200 and created.get("status") == "blocked" and "ASSESSMENT_REQUIRES_QUESTIONNAIRE" in reasons,
            f"http={code}, status={created.get('status')}, reasons={reasons}",
        )
        alternatives = [item.get("action") for item in (plan.get("alternatives") or []) if isinstance(item, dict)]
        case.check(
            "指引包含提交问卷的动作",
            "submit_risk_questionnaire" in alternatives,
            f"alternatives={alternatives}",
        )
        task = self._task(case, created.get("task_id") or "")
        ops = task.get("operations") or []
        case.field("chat_operation_count", len(ops))
        case.check(
            "没有产生任何 operation（更不得留下 dispatching 的孤儿操作）",
            not ops,
            f"operations={[(op.get('tool'), op.get('status')) for op in ops]}",
        )

        after = self._ledger(case=case, note="测评后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("风险测评没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

        self.artifacts["S3-assessment"] = {"profile_id": profile_id, "answers": answers, "level": level}
        after = self._ledger(case=case, note="测评后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("风险测评没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s3_purchase(self, case: Case) -> None:
        before = self._ledger(case=case, note="申购前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.field("plan_feasible", plan.get("feasible"))
        case.field("infeasible_reasons", plan.get("infeasible_reasons"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("申购计划可行（产品可解析、适当性可判定）", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        if not plan.get("feasible"):
            task = self._task(case, created.get("task_id") or "")
            case.field("task_status", task.get("status"))
            case.check("不可行时任务为 blocked（未产生写入）", task.get("status") == "blocked", f"status={task.get('status')}")
            after = self._ledger(case=case, note="申购不可行后余额")
            deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
            case.field("balance_delta_minor", deltas)
            case.check("申购不可行时没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")
            return
        if created.get("status") != "awaiting_authorization":
            case.field("task_status", created.get("status"))
            case.check("任务进入待确认状态", False, f"status={created.get('status')}")
            return
        trace = self._authorize(case, created)
        task = trace.get("task") or {}
        case.field("challenge_required", trace.get("challenge_required"))
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("申购任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}")
        purchases = [op for op in ops if op.get("tool") == "wealth.purchase"]
        case.check("恰好一个 wealth.purchase operation", len(purchases) == 1, f"tools={[op.get('tool') for op in ops]}")
        if purchases:
            op_view = self._op_view(case, purchases[0]["operation_id"])
            # 理财申购是内部记账（wealth_order），不是外部提供方效果，因此不要求 provider 回执；
            # 但仍必须有可核验的订单与资金变化。
            order_code, order_payload, _ = self.http.call(
                "GET", "/v1/wealth/orders", headers=self.h1, timeout=30, case=case, note="申购后理财订单"
            )
            orders = data_of(order_payload).get("orders") or []
            matched = [o for o in orders if o.get("operation_id") == purchases[0]["operation_id"]]
            case.field("matched_order", matched[0] if matched else None)
            case.check(
                "产生可核验的理财订单（内部记账，无提供方回执）",
                order_code == 200 and bool(matched) and matched[0].get("product_id") == "product_huoli",
                f"http={order_code}, matched={matched[:1] if matched else []}",
            )
            expected = (op_view.get("amount_minor") or 0) + (op_view.get("fee_minor") or 0)
            after = self._ledger(case=case, note="申购后余额")
            delta = (after.get("account_savings", {}).get("posted") or 0) - (before.get("account_savings", {}).get("posted") or 0)
            case.field("expected_delta_minor", -expected)
            case.field("savings_delta_minor", delta)
            case.check("储蓄卡按申购金额+费用只扣一次", delta == -expected, f"delta={delta}, expected={-expected}")

    def _s3_missing(self, case: Case) -> None:
        before = self._ledger(case=case, note="缺信息前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.field("clarification_fields", [c.get("field") for c in task.get("clarifications") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务状态 clarifying", task.get("status") == "clarifying", f"status={task.get('status')}")
        case.check("clarifications 非空", bool(task.get("clarifications")), f"fields={[c.get('field') for c in task.get('clarifications') or []]}")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect(case, before, note="缺信息用例")

    def _s3_conflict(self, case: Case) -> None:
        before = self._ledger(case=case, note="冲突用例前余额")
        code, payload, created = self._create(case, case.message)
        case.field("task_id", created.get("task_id"))
        case.field("task_status", created.get("status"))
        plan = created.get("plan") or {}
        case.field("plan_feasible", plan.get("feasible"))
        case.field("infeasible_reasons", plan.get("infeasible_reasons"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        task = self._task(case, created.get("task_id") or "")
        self._assert_conflict_no_effect(case, task, before)

    def _s3_duplicate(self, case: Case) -> None:
        """重复提交同一份问卷答案：同一条测评、不新增 operation、无资金效果。"""
        artifact = self.artifacts.get("S3-assessment") or {}
        if not artifact.get("answers"):
            case.skip("重复提交依赖 S3-assessment 的问卷答案", "S3-assessment 未完成问卷提交")
            return
        ops_before = self._operations_total()
        balances_before = self._ledger(case=case, note="重复提交前余额")
        code, payload, _ = self.http.call(
            "POST", "/v1/wealth/assessment", body={"answers": artifact["answers"]}, headers=self.h1, timeout=30,
            case=case, note="再次提交同一份问卷答案",
        )
        again = data_of(payload).get("data") or {}
        case.field("second_profile_id", again.get("profile_id"))
        case.field("http", code)
        case.check(
            "重复提交返回同一条测评（幂等）",
            code == 200 and envelope_ok(payload) and again.get("profile_id") == artifact.get("profile_id"),
            f"http={code}, first={artifact.get('profile_id')}, second={again.get('profile_id')}, code={error_code(payload)}",
        )
        ops_after = self._operations_total()
        case.field("operations_total_before", ops_before)
        case.field("operations_total_after", ops_after)
        case.check("重复提交没有新增 operation", ops_after == ops_before, f"before={ops_before}, after={ops_after}")
        balances_after = self._ledger(case=case, note="重复提交后余额")
        deltas = {k: (balances_after.get(k, {}).get("posted") or 0) - (balances_before.get(k, {}).get("posted") or 0) for k in balances_before}
        case.field("balance_delta_after_duplicate", deltas)
        case.check("重复提交没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s3_cross_user(self, case: Case) -> None:
        artifact = self.artifacts.get("S3") or {}
        if not artifact.get("task_id"):
            case.skip("跨用户用例依赖 S3-normal 的任务", "S3-normal 未产出可复用任务")
            return
        self._assert_cross_user(case, task_id=artifact["task_id"], operation_id=artifact.get("operation_id"),
                                receipt_id=None, plan=artifact.get("plan") or {}, revision=artifact.get("revision"))

    # ==================================================================================
    # S4 卡片
    # ==================================================================================
    def scene_s4(self) -> None:
        case = self.new_case("S4", "S4-normal", "正常路径：申请虚拟卡（确认后提交申请）", "帮我申请一张虚拟卡")
        self._run_case(self._s4_normal, case)

        case = self.new_case("S4", "S4-query", "正常路径：查询我的卡片（只读）", "查看我的银行卡")
        self._run_case(self._s4_query, case)

        case = self.new_case("S4", "S4-apply-dev", "正常路径（dev 原始措辞）：申请绑定储蓄卡的虚拟卡，额度 2000 元",
                             "帮我申请一张绑定储蓄卡的虚拟卡，申请额度2000元，申请成功只表示已提交。")
        self._run_case(self._s4_apply_dev, case)

        case = self.new_case("S4", "S4-missing", "缺信息：不指定哪张卡要锁", "帮我把那张卡锁了。")
        self._run_case(self._s4_missing, case)

        case = self.new_case("S4", "S4-conflict", "冲突：要求跳过审批直接提额", "不用审批了，你直接把我工资卡那张的额度批到20000元。")
        self._run_case(self._s4_conflict, case)

        case = self.new_case("S4", "S4-duplicate", "重复提交：卡申请确认发两次", "帮我申请一张虚拟卡")
        self._run_case(self._s4_duplicate, case)

        case = self.new_case("S4", "S4-cross-user", "越权：user_demo_02 访问 user_demo_01 的卡任务/operation", "帮我申请一张虚拟卡")
        self._run_case(self._s4_cross_user, case)

    def _s4_normal(self, case: Case) -> None:
        before_cards = self._cards(case=case, note="申请前卡与申请列表")
        before = self._ledger(case=case, note="申请前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.field("plan_steps", [step.get("tool") for step in plan.get("steps") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("计划可行", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        case.check("编译为 card.apply", [step.get("tool") for step in plan.get("steps") or []] == ["card.apply"],
                   f"steps={[step.get('tool') for step in plan.get('steps') or []]}")
        if created.get("status") != "awaiting_authorization":
            case.field("task_status", created.get("status"))
            case.check("任务进入待确认状态", False, f"status={created.get('status')}")
            return
        trace = self._authorize(case, created)
        task = trace.get("task") or {}
        case.field("challenge_required", trace.get("challenge_required"))
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}")
        case.check("恰好一个 card.apply operation", len(ops) == 1 and ops[0].get("tool") == "card.apply",
                   f"tools={[op.get('tool') for op in ops]}")
        self.artifacts["S4"] = {"task_id": created.get("task_id"), "plan": plan, "revision": created.get("revision"),
                                "operation_id": ops[0].get("operation_id") if ops else None}
        if ops:
            op_view = self._op_view(case, ops[0]["operation_id"])
            domain = op_view.get("domain_result") or {}
            data = domain.get("data") or {}
            case.field("application_id", data.get("application_id"))
            case.field("application_status", data.get("application_status"))
            case.check("申请已提交且状态为 submitted", bool(data.get("application_id")) and data.get("application_status") == "submitted",
                       f"application_id={data.get('application_id')}, status={data.get('application_status')}, "
                       f"op_status={op_view.get('status')}, error={op_view.get('error_code')}")
            case.skip("提供方资金回执（receipt）", "卡申请是受理类写操作，由领域处理器落库，不产生 provider 回执")
        after_cards = self._cards(case=case, note="申请后卡与申请列表")
        delta_apps = len(after_cards.get("applications") or []) - len(before_cards.get("applications") or [])
        case.field("applications_delta", delta_apps)
        case.check("卡申请条数恰好 +1（只提交一次）", delta_apps == 1, f"delta={delta_apps}")
        after = self._ledger(case=case, note="申请后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("卡申请没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s4_query(self, case: Case) -> None:
        before = self._ledger(case=case, note="查询前余额")
        code, payload, created = self._create(case, case.message)
        case.field("task_id", created.get("task_id"))
        task = self._settle(case, created.get("task_id") or "")
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}")
        case.check("恰好一个 card.status operation", len(ops) == 1 and ops[0].get("tool") == "card.status",
                   f"tools={[op.get('tool') for op in ops]}")
        if ops:
            op_view = self._op_view(case, ops[0]["operation_id"])
            self._evidence_of_read(case, op_view)
            domain = (op_view.get("domain_result") or {}).get("data") or {}
            cards = domain.get("cards") or []
            case.field("cards_returned", len(cards))
            case.check("返回本人卡片列表", len(cards) > 0, f"cards={len(cards)}")
        after = self._ledger(case=case, note="查询后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("只读查询没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s4_apply_dev(self, case: Case) -> None:
        before_cards = self._cards(case=case, note="申请前卡与申请列表")
        before = self._ledger(case=case, note="申请前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        steps = [step.get("tool") for step in plan.get("steps") or []]
        case.field("task_id", created.get("task_id"))
        case.field("plan_steps", steps)
        case.field("task_status", created.get("status"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("dev 原始措辞应编译为 card.apply", steps == ["card.apply"], f"steps={steps}")
        task = created
        if created.get("status") == "awaiting_authorization":
            trace = self._authorize(case, created, timeout=8.0)
            task = trace.get("task") or {}
        elif created.get("status") not in TERMINAL_TASK_STATES:
            task = self._settle(case, created.get("task_id") or "", timeout=8.0)
        else:
            task = self._task(case, created.get("task_id") or "")
        case.field("task_status", task.get("status"))
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}, result={_short(task.get('result') or {})}")
        after_cards = self._cards(case=case, note="申请后卡与申请列表")
        delta_apps = len(after_cards.get("applications") or []) - len(before_cards.get("applications") or [])
        case.field("applications_delta", delta_apps)
        case.check("卡申请条数恰好 +1", delta_apps == 1, f"delta={delta_apps}")
        after = self._ledger(case=case, note="申请后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("申请没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s4_missing(self, case: Case) -> None:
        before_cards = self._cards(case=case, note="缺信息前卡与申请列表")
        before = self._ledger(case=case, note="缺信息前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.field("clarification_fields", [c.get("field") for c in task.get("clarifications") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务状态 clarifying", task.get("status") == "clarifying", f"status={task.get('status')}")
        case.check("clarifications 非空", bool(task.get("clarifications")), f"fields={[c.get('field') for c in task.get('clarifications') or []]}")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect(case, before, note="缺信息用例")
        after_cards = self._cards(case=case, note="缺信息后卡与申请列表")
        controls_before = sum(len(card.get("controls") or []) for card in before_cards.get("items") or [])
        controls_after = sum(len(card.get("controls") or []) for card in after_cards.get("items") or [])
        case.field("card_controls_delta", controls_after - controls_before)
        case.check("卡片控制项未被改动", controls_after == controls_before, f"before={controls_before}, after={controls_after}")

    def _s4_conflict(self, case: Case) -> None:
        before_cards = self._cards(case=case, note="冲突用例前卡与申请列表")
        before = self._ledger(case=case, note="冲突用例前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        case.field("task_id", created.get("task_id"))
        case.field("task_status", created.get("status"))
        case.field("compiled_goals", (created.get("goal") or {}).get("goals"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        task = self._task(case, created.get("task_id") or "")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect(case, before, note="冲突用例")
        after_cards = self._cards(case=case, note="冲突用例后卡与申请列表")
        limits_before = {card.get("card_id"): card.get("credit_limit_minor") for card in before_cards.get("items") or []}
        limits_after = {card.get("card_id"): card.get("credit_limit_minor") for card in after_cards.get("items") or []}
        case.field("credit_limit_change", {"before": limits_before, "after": limits_after})
        case.check("卡片额度未被改动", limits_before == limits_after, f"before={limits_before}, after={limits_after}")
        case.check("未新增卡申请", len(after_cards.get("applications") or []) == len(before_cards.get("applications") or []),
                   f"before={len(before_cards.get('applications') or [])}, after={len(after_cards.get('applications') or [])}")

    def _s4_duplicate(self, case: Case) -> None:
        artifact = self.artifacts.get("S4") or {}
        if not artifact.get("task_id"):
            case.skip("重复提交依赖 S4-normal 的任务", "S4-normal 未产出可复用任务")
            return
        case.field("task_id", artifact["task_id"])
        before_task = self._task(case, artifact["task_id"])
        ops_before = {op.get("operation_id") for op in (before_task.get("operations") or [])}
        cards_before = self._cards(case=case, note="重复确认前卡与申请列表")
        created = {"task_id": artifact["task_id"], "plan": artifact.get("plan") or {}, "revision": artifact.get("revision")}
        code, payload, _ = self._confirm(case, created, note="第二次确认（重复提交）")
        second_error = error_code(payload)
        second_data = data_of(payload)
        re_challenge = envelope_ok(payload) and second_data.get("status") == "challenge_required"
        rejected = (not envelope_ok(payload)) and second_error in CONFLICT_CODES
        case.field("second_confirm_http", code)
        case.field("second_confirm_error", second_error)
        case.check("第二次确认被拒绝（冲突类错误码）或重新要求强验证", bool(rejected or re_challenge),
                   f"http={code}, code={second_error}, data_status={second_data.get('status')}")
        task_after = self._settle(case, artifact["task_id"], timeout=3.0)
        ops_after = {op.get("operation_id") for op in (task_after.get("operations") or [])}
        case.field("operation_count_after", len(ops_after))
        case.check("重复确认没有新增 operation", ops_after == ops_before, f"before={sorted(ops_before)}, after={sorted(ops_after)}")
        cards_after = self._cards(case=case, note="重复确认后卡与申请列表")
        delta_apps = len(cards_after.get("applications") or []) - len(cards_before.get("applications") or [])
        case.field("applications_delta_after_duplicate", delta_apps)
        case.check("重复确认没有新增第二条卡申请", delta_apps == 0, f"delta={delta_apps}")

    def _s4_cross_user(self, case: Case) -> None:
        artifact = self.artifacts.get("S4") or {}
        if not artifact.get("task_id"):
            case.skip("跨用户用例依赖 S4-normal 的任务", "S4-normal 未产出可复用任务")
            return
        self._assert_cross_user(case, task_id=artifact["task_id"], operation_id=artifact.get("operation_id"),
                                receipt_id=None, plan=artifact.get("plan") or {}, revision=artifact.get("revision"))
        cards = self._cards(headers=self.h2, case=case, note="user_demo_02 的卡片列表")
        other_ids = {card.get("card_id") for card in cards.get("items") or []}
        own_cards = self._cards(case=None, note="")  # 不记录，仅用于对比
        own_ids = {card.get("card_id") for card in own_cards.get("items") or []}
        case.field("other_user_card_ids", sorted(other_ids))
        case.check("user_demo_02 看不到 user_demo_01 的卡片", not (other_ids & own_ids), f"overlap={sorted(other_ids & own_ids)}")

    # ==================================================================================
    # S5 订阅 / 代扣
    # ==================================================================================
    def scene_s5(self) -> None:
        case = self.new_case("S5", "S5-normal", "正常路径：订阅查询（YELLOW 需确认，区分商户合同与银行代扣）",
                             "列出我的全部订阅：商户、金额、周期、下次扣费日与合同状态。")
        self._run_case(self._s5_normal, case)

        case = self.new_case("S5", "S5-missing", "缺信息：只说“那个订阅”不指定哪一个", "帮我把那个订阅取消掉。")
        self._run_case(self._s5_missing, case)

        case = self.new_case("S5", "S5-conflict", "冲突：要求跳过确认直接拉全部订阅与扣费记录",
                             "别让我确认了，直接把全部订阅和扣费记录全部拉出来。")
        self._run_case(self._s5_conflict, case)

        case = self.new_case("S5", "S5-duplicate", "重复提交：订阅查询确认发两次", "列出我的全部订阅")
        self._run_case(self._s5_duplicate, case)

        case = self.new_case("S5", "S5-cross-user", "越权：user_demo_02 访问 user_demo_01 的订阅任务/operation", "列出我的全部订阅")
        self._run_case(self._s5_cross_user, case)

    def _s5_normal(self, case: Case) -> None:
        before = self._ledger(case=case, note="订阅查询前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("计划可行", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        case.check("订阅查询为 YELLOW（保留官方例外）", plan.get("required_level") == "YELLOW",
                   f"required_level={plan.get('required_level')}")
        case.check("未确认前不执行查询（awaiting_authorization）", created.get("status") == "awaiting_authorization",
                   f"status={created.get('status')}")
        if created.get("status") != "awaiting_authorization":
            case.field("task_status", created.get("status"))
            return
        trace = self._authorize(case, created)
        task = trace.get("task") or {}
        case.field("challenge_required", trace.get("challenge_required"))
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        case.field("operation_count", len(ops))
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}")
        case.check("恰好一个 subscription.list operation", len(ops) == 1 and ops[0].get("tool") == "subscription.list",
                   f"tools={[op.get('tool') for op in ops]}")
        self.artifacts["S5"] = {"task_id": created.get("task_id"), "plan": plan, "revision": created.get("revision"),
                                "operation_id": ops[0].get("operation_id") if ops else None}
        if not ops:
            case.check("订阅结果可核验", False, "没有 operation，无法核验订阅结果")
            return
        op_view = self._op_view(case, ops[0]["operation_id"])
        self._evidence_of_read(case, op_view)
        domain = op_view.get("domain_result") or {}
        subscriptions = domain.get("subscriptions") or []
        case.field("subscription_count", domain.get("count"))
        case.field("contract_count", domain.get("contract_count"))
        case.check("查询结果与条数一致", domain.get("count") == len(subscriptions) and len(subscriptions) > 0,
                   f"count={domain.get('count')}, len={len(subscriptions)}")
        policy = domain.get("policy") or {}
        case.check("保留订阅查询的 YELLOW 例外说明", policy.get("exception") == "subscription_query_yellow",
                   f"exception={policy.get('exception')}")
        contract_ok = all((item.get("source") or {}).get("data_source") for item in subscriptions)
        mandate_ok = all(isinstance(item.get("mandate"), dict) and item["mandate"].get("scope") == "bank_debit" for item in subscriptions)
        case.check("区分商户合同（source.data_source）与银行代扣授权（mandate.scope=bank_debit）",
                   contract_ok and mandate_ok,
                   f"contract_ok={contract_ok}, mandate_ok={mandate_ok}")
        detail_fields = all(item.get("amount") and item.get("period") and item.get("next_charge_at") and item.get("status")
                            for item in subscriptions)
        case.check("每个订阅含金额/周期/下次扣费日/合同状态", detail_fields,
                   f"sample={_short(subscriptions[0] if subscriptions else {})}")
        after = self._ledger(case=case, note="订阅查询后余额")
        deltas = {k: (after.get(k, {}).get("posted") or 0) - (before.get(k, {}).get("posted") or 0) for k in before}
        case.field("balance_delta_minor", deltas)
        case.check("订阅查询没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s5_missing(self, case: Case) -> None:
        before = self._ledger(case=case, note="缺信息前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.field("compiled_goals", (created.get("goal") or {}).get("goals"))
        case.field("clarification_fields", [c.get("field") for c in task.get("clarifications") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务状态 clarifying", task.get("status") == "clarifying", f"status={task.get('status')}")
        case.check("clarifications 非空", bool(task.get("clarifications")), f"fields={[c.get('field') for c in task.get('clarifications') or []]}")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect(case, before, note="缺信息用例")

    def _s5_conflict(self, case: Case) -> None:
        before = self._ledger(case=case, note="冲突用例前余额")
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        case.check("未确认前不得执行订阅查询", (task.get("operations") or []) == [],
                   f"operations={[op.get('tool') for op in task.get('operations') or []]}")
        self._assert_conflict_no_effect(case, task, before)

    def _s5_duplicate(self, case: Case) -> None:
        artifact = self.artifacts.get("S5") or {}
        if not artifact.get("task_id"):
            case.skip("重复提交依赖 S5-normal 的任务", "S5-normal 未产出可复用任务")
            return
        case.field("task_id", artifact["task_id"])
        before_task = self._task(case, artifact["task_id"])
        ops_before = {op.get("operation_id") for op in (before_task.get("operations") or [])}
        balances_before = self._ledger(case=case, note="重复确认前余额")
        created = {"task_id": artifact["task_id"], "plan": artifact.get("plan") or {}, "revision": artifact.get("revision")}
        code, payload, _ = self._confirm(case, created, note="第二次确认（重复提交）")
        second_error = error_code(payload)
        second_data = data_of(payload)
        re_challenge = envelope_ok(payload) and second_data.get("status") == "challenge_required"
        rejected = (not envelope_ok(payload)) and second_error in CONFLICT_CODES
        case.field("second_confirm_http", code)
        case.field("second_confirm_error", second_error)
        case.check("第二次确认被拒绝（冲突类错误码）或重新要求强验证", bool(rejected or re_challenge),
                   f"http={code}, code={second_error}, data_status={second_data.get('status')}")
        task_after = self._settle(case, artifact["task_id"], timeout=3.0)
        ops_after = {op.get("operation_id") for op in (task_after.get("operations") or [])}
        case.field("operation_count_after", len(ops_after))
        case.check("重复确认没有新增 operation", ops_after == ops_before, f"before={sorted(ops_before)}, after={sorted(ops_after)}")
        lists = [op for op in (task_after.get("operations") or []) if op.get("tool") == "subscription.list"]
        lists_before = [op for op in (before_task.get("operations") or []) if op.get("tool") == "subscription.list"]
        case.field("subscription_list_count_before", len(lists_before))
        case.check("订阅查询 operation 数量与重复提交前一致", len(lists) == len(lists_before),
                   f"before={len(lists_before)}, after={len(lists)}")
        balances_after = self._ledger(case=case, note="重复确认后余额")
        deltas = {k: (balances_after.get(k, {}).get("posted") or 0) - (balances_before.get(k, {}).get("posted") or 0) for k in balances_before}
        case.field("balance_delta_after_duplicate", deltas)
        case.check("重复确认没有资金效果", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s5_cross_user(self, case: Case) -> None:
        artifact = self.artifacts.get("S5") or {}
        if not artifact.get("task_id"):
            case.skip("跨用户用例依赖 S5-normal 的任务", "S5-normal 未产出可复用任务")
            return
        self._assert_cross_user(case, task_id=artifact["task_id"], operation_id=artifact.get("operation_id"),
                                receipt_id=None, plan=artifact.get("plan") or {}, revision=artifact.get("revision"))
        other_code, other_payload, _ = self.http.call(
            "GET", "/v1/subscriptions", headers=self.h2, timeout=45, case=case, note="user_demo_02 的订阅入口",
        )
        other_items = data_of(other_payload).get("items") or []
        case.field("other_user_subscription_items", len(other_items))
        case.check("user_demo_02 的订阅入口不返回他人订阅", other_items == [], f"items={len(other_items)}")

    # ==================================================================================
    # S6 跨场景（活动预算 + 多商户 + 日期）
    # ==================================================================================
    def scene_s6(self) -> None:
        case = self.new_case("S6", "S6-normal", "正常路径：生日鲜花+蛋糕（多商户、含费用、指定日期，RED 强验证）",
                             "帮我订生日鲜花和蛋糕，预算800元，这周六送到，用储蓄卡付款。")
        self._run_case(self._s6_normal, case)

        case = self.new_case("S6", "S6-missing", "缺信息：不给预算与日期", "下周朋友生日，帮我安排好鲜花和蛋糕。")
        self._run_case(self._s6_missing, case)

        case = self.new_case("S6", "S6-conflict", "冲突：要从应急金里拿钱办活动", "预算不够就从8000元应急金里拿1500元，先办活动要紧。")
        self._run_case(self._s6_conflict, case)

        case = self.new_case("S6", "S6-duplicate", "重复提交：生日计划确认发两次", "帮我订生日鲜花和蛋糕，预算800元，这周六送到，用储蓄卡付款。")
        self._run_case(self._s6_duplicate, case)

        case = self.new_case("S6", "S6-cross-user", "越权：user_demo_02 访问 user_demo_01 的跨场景任务/operation",
                             "帮我订生日鲜花和蛋糕，预算800元，这周六送到，用储蓄卡付款。")
        self._run_case(self._s6_cross_user, case)

    def _s6_normal(self, case: Case) -> None:
        before = self._ledger(case=case, note="预订前余额")
        code, payload, created = self._create(case, case.message)
        plan = created.get("plan") or {}
        goal = created.get("goal") or {}
        hard = goal.get("hard_constraints") or {}
        steps = plan.get("steps") or []
        book_steps = [step for step in steps if step.get("tool") == "merchant.book"]
        merchants = {step.get("typed_args", {}).get("merchant_id") for step in book_steps}
        total = sum(int(step.get("typed_args", {}).get("amount_minor") or 0) + int(step.get("typed_args", {}).get("fee_minor") or 0)
                    for step in book_steps)
        case.field("task_id", created.get("task_id"))
        case.field("plan_id", plan.get("plan_id"))
        case.field("scope_hash", plan.get("scope_hash"))
        case.field("required_level", plan.get("required_level"))
        case.field("budget_minor", hard.get("total_budget_minor"))
        case.field("delivery_deadline", hard.get("delivery_deadline"))
        case.field("merchant_count", len(merchants))
        case.field("planned_total_minor", total)
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("计划可行", bool(plan.get("feasible")), f"infeasible={plan.get('infeasible_reasons')}")
        case.check("多商户：至少两个不同商户的预订步骤", len(merchants) >= 2, f"merchants={sorted(m for m in merchants if m)}")
        case.check("预算含费用：计划总额不超过 80000 分",
                   hard.get("total_budget_minor") == 80000 and total <= 80000,
                   f"budget={hard.get('total_budget_minor')}, total={total}")
        case.check("日期：目标含明确交付截止时间", bool(hard.get("delivery_deadline")), f"deadline={hard.get('delivery_deadline')}")
        case.check("付费商户预订按 RED 处理", plan.get("required_level") == "RED", f"required_level={plan.get('required_level')}")
        if created.get("status") != "awaiting_authorization":
            case.field("task_status", created.get("status"))
            case.check("任务进入待确认状态", False, f"status={created.get('status')}")
            return
        trace = self._authorize(case, created)
        task = trace.get("task") or {}
        case.field("challenge_required", trace.get("challenge_required"))
        case.field("task_status", task.get("status"))
        ops = task.get("operations") or []
        books = [op for op in ops if op.get("tool") == "merchant.book"]
        case.field("operation_count", len(ops))
        case.field("book_operation_count", len(books))
        case.field("operation_statuses", [(op.get("tool"), op.get("status")) for op in ops])
        case.check("任务终态 succeeded", task.get("status") == "succeeded", f"status={task.get('status')}, result={_short(task.get('result') or {})}")
        case.check("正好两笔商户预订 operation", len(books) == 2, f"count={len(books)}")
        case.check("两笔预订均成功", bool(books) and all(op.get("status") == "succeeded" for op in books),
                   f"statuses={[op.get('status') for op in books]}")
        if books:
            self.artifacts["S6"] = {"task_id": created.get("task_id"), "plan": plan, "revision": created.get("revision"),
                                    "operation_id": books[0].get("operation_id")}
            refs = set()
            captured = 0
            for book in books:
                op_view = self._op_view(case, book["operation_id"])
                refs.add(op_view.get("provider_order_ref"))
                captured += int(op_view.get("amount_minor") or 0) + int(op_view.get("fee_minor") or 0)
                self._evidence_of_effect(case, op_view)
            case.field("provider_refs", sorted(ref for ref in refs if ref))
            case.check("两笔预订有各自可核验的提供方引用", len([ref for ref in refs if ref]) == 2,
                       f"refs={[ref for ref in refs if ref]}, op_statuses={[op.get('status') for op in books]}")
            after = self._ledger(case=case, note="预订后余额")
            delta = (after.get("account_savings", {}).get("posted") or 0) - (before.get("account_savings", {}).get("posted") or 0)
            case.field("expected_delta_minor", -captured)
            case.field("savings_delta_minor", delta)
            case.check("储蓄卡按实际捕获金额只扣一次（含费用）", captured > 0 and delta == -captured,
                       f"delta={delta}, expected={-captured}")
        else:
            self.artifacts["S6"] = {"task_id": created.get("task_id"), "plan": plan, "revision": created.get("revision")}

    def _s6_missing(self, case: Case) -> None:
        before = self._ledger(case=case, note="缺信息前余额")
        baseline_total = self._operations_total()
        code, payload, created = self._create(case, case.message)
        task = self._task(case, created.get("task_id") or "")
        case.field("task_id", created.get("task_id"))
        case.field("task_status", task.get("status"))
        case.field("clarification_fields", [c.get("field") for c in task.get("clarifications") or []])
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        case.check("任务状态 clarifying", task.get("status") == "clarifying", f"status={task.get('status')}")
        case.check("clarifications 非空", bool(task.get("clarifications")), f"fields={[c.get('field') for c in task.get('clarifications') or []]}")
        self._assert_no_operations(case, task, baseline_total)
        self._assert_no_effect_operation(case, task)
        self._assert_no_effect(case, before, note="缺信息用例")

    def _s6_conflict(self, case: Case) -> None:
        before = self._ledger(case=case, note="冲突用例前余额")
        code, payload, created = self._create(case, case.message)
        case.field("task_id", created.get("task_id"))
        case.field("task_status", created.get("status"))
        plan = created.get("plan") or {}
        case.field("plan_feasible", plan.get("feasible"))
        case.field("infeasible_reasons", plan.get("infeasible_reasons"))
        case.check("创建任务 HTTP 200 且信封 ok", code == 200 and envelope_ok(payload), f"http={code}, code={error_code(payload)}")
        self._assert_blocked_or_clarifying(case, created)
        task = self._task(case, created.get("task_id") or "")
        self._assert_conflict_no_effect(case, task, before)

    def _s6_duplicate(self, case: Case) -> None:
        artifact = self.artifacts.get("S6") or {}
        if not artifact.get("task_id"):
            case.skip("重复提交依赖 S6-normal 的任务", "S6-normal 未产出可复用任务")
            return
        case.field("task_id", artifact["task_id"])
        before_task = self._task(case, artifact["task_id"])
        ops_before = {op.get("operation_id") for op in (before_task.get("operations") or [])}
        balances_before = self._ledger(case=case, note="重复确认前余额")
        created = {"task_id": artifact["task_id"], "plan": artifact.get("plan") or {}, "revision": artifact.get("revision")}
        code, payload, _ = self._confirm(case, created, note="第二次确认（重复提交）")
        second_error = error_code(payload)
        second_data = data_of(payload)
        re_challenge = envelope_ok(payload) and second_data.get("status") == "challenge_required"
        rejected = (not envelope_ok(payload)) and second_error in CONFLICT_CODES
        case.field("second_confirm_http", code)
        case.field("second_confirm_error", second_error)
        case.field("second_confirm_rechallenge", re_challenge)
        case.check("第二次确认被拒绝（冲突类错误码）或重新要求强验证", bool(rejected or re_challenge),
                   f"http={code}, code={second_error}, data_status={second_data.get('status')}")
        task_after = self._settle(case, artifact["task_id"], timeout=4.0)
        ops_after = {op.get("operation_id") for op in (task_after.get("operations") or [])}
        case.field("operation_count_after", len(ops_after))
        case.check("重复确认没有新增 operation", ops_after == ops_before, f"before={sorted(ops_before)}, after={sorted(ops_after)}")
        books = [op for op in (task_after.get("operations") or []) if op.get("tool") == "merchant.book"]
        books_before = [op for op in (before_task.get("operations") or []) if op.get("tool") == "merchant.book"]
        case.field("book_operation_count_after", len(books))
        case.check("商户预订 operation 数量与重复提交前一致（未重复下单）",
                   len(books) == len(books_before), f"before={len(books_before)}, after={len(books)}")
        balances_after = self._ledger(case=case, note="重复确认后余额")
        deltas = {k: (balances_after.get(k, {}).get("posted") or 0) - (balances_before.get(k, {}).get("posted") or 0) for k in balances_before}
        case.field("balance_delta_after_duplicate", deltas)
        case.check("重复确认没有二次扣款", all(value == 0 for value in deltas.values()), f"delta={deltas}")

    def _s6_cross_user(self, case: Case) -> None:
        artifact = self.artifacts.get("S6") or {}
        if not artifact.get("task_id"):
            case.skip("跨用户用例依赖 S6-normal 的任务", "S6-normal 未产出可复用任务")
            return
        self._assert_cross_user(case, task_id=artifact["task_id"], operation_id=artifact.get("operation_id"),
                                receipt_id=None, plan=artifact.get("plan") or {}, revision=artifact.get("revision"))

    # ==================================================================================
    # 通用：跨用户隔离
    # ==================================================================================
    def _assert_cross_user(self, case: Case, *, task_id: str, operation_id: str | None,
                           receipt_id: str | None, plan: dict, revision) -> None:
        case.field("owner_task_id", task_id)
        code, payload, _ = self.http.call(
            "GET", f"/v1/tasks/{task_id}", headers=self.h2, timeout=45, case=case,
            note="user_demo_02 读取 user_demo_01 的任务",
        )
        body_text = json.dumps(payload, ensure_ascii=False)
        case.check("跨用户读取任务被拒绝（403/404）", code in (403, 404), f"http={code}, code={error_code(payload)}")
        case.check("任务拒绝原因属于归属校验", error_code(payload) in ("OBJECT_NOT_ACCESSIBLE", "FORBIDDEN"),
                   f"code={error_code(payload)}")
        case.check("任务错误响应不泄露归属与内容", OWNER not in body_text and "Lisi" not in body_text,
                   f"len={len(body_text)}")

        if operation_id:
            code, payload, _ = self.http.call(
                "GET", f"/v1/operations/{operation_id}", headers=self.h2, timeout=45, case=case,
                note="user_demo_02 读取 user_demo_01 的 operation",
            )
            body_text = json.dumps(payload, ensure_ascii=False)
            case.check("跨用户读取 operation 被拒绝（403/404）", code in (403, 404), f"http={code}, code={error_code(payload)}")
            case.check("operation 错误响应不泄露归属", OWNER not in body_text, f"len={len(body_text)}")
        else:
            case.skip("跨用户读取 operation", "本用例未产出 operation")

        if receipt_id:
            code, payload, _ = self.http.call(
                "GET", f"/v1/receipts/{receipt_id}", headers=self.h2, timeout=45, case=case,
                note="user_demo_02 读取 user_demo_01 的回执",
            )
            case.check("跨用户读取回执被拒绝（403/404）", code in (403, 404), f"http={code}, code={error_code(payload)}")
        else:
            case.skip("跨用户读取回执（receipt）", "本次运行未产生任何 receipt（提供方写入未完成），无法验证回执级隔离")

        plan_id = (plan or {}).get("plan_id")
        if plan_id:
            body = {"scope_hash": (plan or {}).get("scope_hash"), "expected_revision": revision}
            code, payload, _ = self.http.call(
                "POST", f"/v1/plans/{plan_id}/confirm", body=body, headers=self.h2, timeout=60, case=case,
                note="user_demo_02 确认 user_demo_01 的计划",
            )
            case.check("跨用户确认计划被拒绝（403/404/409）", code in (403, 404, 409), f"http={code}, code={error_code(payload)}")
        else:
            case.skip("跨用户确认计划", "没有可复用的计划")

        code, payload, _ = self.http.call("GET", "/v1/tasks", headers=self.h2, timeout=45, case=case, note="user_demo_02 的任务列表")
        other_items = data_of(payload).get("items") or []
        other_ids = {item.get("task_id") for item in other_items}
        case.field("other_user_task_count", len(other_items))
        case.check("user_demo_02 的任务列表不含 user_demo_01 的任务", task_id not in other_ids, f"leaked={task_id in other_ids}")

        code, payload, _ = self.http.call("GET", "/v1/accounts", headers=self.h2, timeout=45, case=case, note="user_demo_02 的账户")
        accounts = data_of(payload).get("accounts") or []
        account_ids = {item.get("account_id") for item in accounts}
        case.field("other_user_account_ids", sorted(account_ids))
        case.check("user_demo_02 看不到 user_demo_01 的账户", "account_savings" not in account_ids and "account_salary" not in account_ids,
                   f"accounts={sorted(account_ids)}")

    # ==================================================================================
    # 主流程
    # ==================================================================================
    def preflight(self) -> None:
        code, payload, _ = self.http.call("GET", "/healthz", timeout=20)
        ok = code == 200 and payload.get("status") in ("ok", "degraded")
        self.artifacts["healthz"] = {
            "http": code,
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "database_ok": (payload.get("database") or {}).get("ok"),
            "adapters": (payload.get("adapters") or {}).get("registered"),
        }
        if not ok:
            self.preflight_error = f"/healthz 不可用: http={code}, status={payload.get('status')}"
            return
        try:
            self.h1 = self.login(OWNER)
            self.h2 = self.login(OTHER)
        except ApiFailure as exc:
            self.preflight_error = str(exc)

    def run(self) -> int:
        self.preflight()
        if self.preflight_error:
            case = self.new_case("PRE", "PREFLIGHT", "环境预检（服务可达、登录成功）")
            case.check("服务可用且两个演示用户可登录", False, self.preflight_error)
            case.finish()
            self.print_summary()
            return 1

        scenes = [scene for scene in SCENES if not self.scene_filter or scene in self.scene_filter]
        for scene in scenes:
            handler = getattr(self, f"scene_{scene.lower()}", None)
            if handler is None:
                continue
            handler()

        self.print_summary()
        return 0 if not any(case.result == "FAIL" for case in self.cases) else 1

    # -- 输出 --------------------------------------------------------------
    def summary(self) -> dict:
        passed = [case.case_id for case in self.cases if case.result == "PASS"]
        failed = [case.case_id for case in self.cases if case.result == "FAIL"]
        assertions = [a for case in self.cases for a in case.assertions]
        return {
            "total_cases": len(self.cases),
            "passed": len(passed),
            "failed": len(failed),
            "passed_cases": passed,
            "failed_cases": failed,
            "assertions_total": len(assertions),
            "assertions_passed": sum(1 for a in assertions if a["ok"] is True),
            "assertions_failed": sum(1 for a in assertions if a["ok"] is False),
            "assertions_skipped": sum(1 for a in assertions if a["ok"] is None),
        }

    def print_summary(self) -> None:
        summary = self.summary()
        scenes = [scene for scene in SCENES if not self.scene_filter or scene in self.scene_filter]
        print("")
        print("=" * 132)
        print(f"FinPilot 真实 HTTP 端到端验收 · base_url={self.base_url} · scenes={','.join(scenes)}")
        print("=" * 132)
        widths = (6, 26, 6, 16, 14, 12)
        header = (_pad("场景", widths[0]) + _pad("用例", widths[1]) + _pad("结果", widths[2])
                  + _pad("任务状态", widths[3]) + _pad("HTTP", widths[4]) + _pad("耗时ms", widths[5]) + "失败原因")
        print(header)
        print("-" * 132)
        for case in self.cases:
            reason = ""
            failures = case.failures
            if failures:
                reason = "; ".join(f"{item['name']}（{_truncate(item['detail'], 90)}）" for item in failures[:2])
                if len(failures) > 2:
                    reason += f" 等 {len(failures)} 条"
            print(
                _pad(case.scene, widths[0])
                + _pad(_truncate(case.case_id, widths[1] - 1), widths[1])
                + _pad(case.result, widths[2])
                + _pad(_truncate(case.task_status or "-", widths[3] - 1), widths[3])
                + _pad(str(case.http_status if case.http_status is not None else "-"), widths[4])
                + _pad(f"{case.elapsed_ms:.0f}", widths[5])
                + reason
            )
        print("-" * 132)
        print(
            f"PASS {summary['passed']} / FAIL {summary['failed']} / 共 {summary['total_cases']} 条用例 ；"
            f"断言 {summary['assertions_passed']} 通过 / {summary['assertions_failed']} 失败 / {summary['assertions_skipped']} 跳过"
        )
        if summary["failed_cases"]:
            print("FAIL 用例：" + "、".join(summary["failed_cases"]))
        print("=" * 132)

    def evidence(self) -> dict:
        return {
            "schema_version": "1.0",
            "kind": "finpilot.e2e_http_smoke",
            "generated_at": _iso(_now_utc()),
            "started_at": _iso(self.started_at),
            "base_url": self.base_url,
            "scene_filter": self.scene_filter,
            "owner_user": OWNER,
            "other_user": OTHER,
            "preflight": self.artifacts.get("healthz"),
            "preflight_error": self.preflight_error,
            "summary": self.summary(),
            "cases": [case.as_dict() for case in self.cases],
            "notes": [
                "断言全部使用前后差值与相对关系，不依赖演示库中的绝对余额。",
                "只读场景以 operation.domain_result 作为可核验结果；资金/业务效果场景要求 receipt 或 provider_ref。",
                "GREEN 计划 confirm 为文档化空操作，S2 重复提交用例断言零新增 operation + 零余额变化。",
                "RED 计划重复确认可能返回新的 TOTP 挑战而非冲突码，硬断言仍是“不得新增 operation/不得二次扣款”。",
            ],
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FinPilot 六场景真实 HTTP 端到端验收（只读断言 + 证据 JSON）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API 基础地址")
    parser.add_argument("--out", default=None, help="证据 JSON 输出路径（默认 evidence/e2e_http_<UTC时间戳>.json）")
    parser.add_argument("--scene", default=None, help="只跑某场景，可逗号分隔，如 S1 或 S1,S4")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass
    args = parse_args(argv)
    scene_filter = None
    if args.scene:
        scene_filter = [item.strip().upper() for item in str(args.scene).split(",") if item.strip()]
        unknown = [item for item in scene_filter if item not in SCENES]
        if unknown:
            print(f"未知场景：{unknown}；可选：{list(SCENES)}", file=sys.stderr)
            return 2

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = REPO_ROOT / out_path
    else:
        stamp = _now_utc().strftime("%Y%m%dT%H%M%SZ")
        out_path = REPO_ROOT / "evidence" / f"e2e_http_{stamp}.json"

    runner = Runner(args.base_url, scene_filter)
    exit_code = runner.run()
    evidence = runner.evidence()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"证据写入失败：{exc}", file=sys.stderr)
        return 1
    print(f"证据 JSON：{out_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())