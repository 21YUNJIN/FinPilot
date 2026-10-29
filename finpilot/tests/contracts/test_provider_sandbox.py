"""独立业务提供方沙箱契约测试（V5 SYS-06 / API-04 / S4、S6-03 / T21、T33、T54）。

本文件用 ``fastapi.testclient.TestClient`` 直接驱动两个**独立提供方应用**
（``services/provider-sandbox/bank.py``、``merchant.py``），不依赖主银行应用、不连 PostgreSQL：

- 每个用例用 ``tmp_path`` 下的独立 SQLite 状态库，互不污染；
- ``clean_db`` 覆盖 ``tests/conftest.py`` 中自动使用的 PostgreSQL 夹具（提供方沙箱与主库无关）；
- 回调走 ``signing.set_callback_transport`` 注入的 ``CallbackSink``：真实验签、按 event_id 去重、
  按 event_version 判定乱序，并把“实际生效次数”如实记录下来，用于断言“只结算一次”。

覆盖矩阵（与任务要求的 7 项一一对应）：
1. 同稳定键重复提交只产生一个 provider_ref 与一次结算效果；
2. 迟到成功事件与重复回调（event_version 乱序）不改变最终只结算一次；
3. drop_response / delay_ms 下客户端得到超时语义而提供方状态仍可查询；
4. 已结算撤单被拒绝且原状态不变；
5. 签名验证：正确签名通过、篡改载荷/时间戳、乱序（重放）nonce 失败；
6. 商户缺货、变价（不静默成交）、取消费与不可退条款；
7. untrusted_text 中的注入文本不改变结构化 amount_minor/inventory。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX_DIR = REPO_ROOT / "services" / "provider-sandbox"
if str(SANDBOX_DIR) not in sys.path:
    sys.path.insert(0, str(SANDBOX_DIR))

# import bank / merchant 本身没有任何文件系统副作用（模块级 app 是惰性的），
# 因此本文件不在导入期改写任何环境变量；回调相关变量由 autouse 夹具管理。

import bank  # noqa: E402
import merchant  # noqa: E402
import signing  # noqa: E402
import store as store_mod  # noqa: E402

pytestmark = pytest.mark.contract

DEFAULT_SECRET = "dev_only_provider_callback_secret"
# 与主实现 finpilot.config.Settings.provider_callback_base_url 默认值一致
DEFAULT_CALLBACK_BASE_URL = "http://127.0.0.1:58000"
USER_ID = "user_demo_01"


@pytest.fixture(autouse=True)
def sandbox_env(monkeypatch):
    """固定回调协议相关环境变量，测试结束后自动恢复（不污染其他工作包的测试）。"""
    monkeypatch.setenv("PROVIDER_CALLBACK_SECRET", DEFAULT_SECRET)
    monkeypatch.delenv("PROVIDER_CALLBACK_BASE_URL", raising=False)  # 走默认基址
    monkeypatch.setenv("SANDBOX_MAX_DELAY_MS", "500")
    monkeypatch.delenv("PROVIDER_CALLBACK_DISABLED", raising=False)
    yield


@pytest.fixture(autouse=True)
def clean_db():
    """覆盖 ``tests/conftest.py`` 的 PostgreSQL 夹具：提供方沙箱测试不接触主库。"""
    yield


class _RecordingTransport:
    """默认传输层：只记录并返回成功，避免测试期间对 127.0.0.1:58080 发起真实连接。"""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def __call__(self, *, url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float):
        self.received.append({"url": url, "headers": dict(headers), "payload": dict(payload)})
        return {"delivered": True, "http_status": 200, "error": None}


@pytest.fixture(autouse=True)
def default_transport():
    transport = _RecordingTransport()
    signing.set_callback_transport(transport)
    yield transport
    signing.set_callback_transport(None)


class CallbackSink:
    """模拟主银行 ``POST /internal/provider-events/{provider_id}`` 的接收侧。

    真实执行签名验证、nonce 一次性校验、event_id 去重与 event_version 乱序核验，
    并区分四种结果：``applied`` / ``deduped_event_id`` / ``ignored_stale`` / ``rejected``。
    """

    def __init__(
        self,
        *,
        secret: str | None = None,
        window_seconds: int = 300,
        enforce_nonce: bool = True,
    ) -> None:
        self.secret = secret if secret is not None else signing.callback_secret()
        self.window_seconds = window_seconds
        self.enforce_nonce = enforce_nonce
        self.nonces = signing.NonceGuard()
        self.received: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []
        self.applied_events: dict[str, dict[str, Any]] = {}
        self.latest_version: dict[str, int] = {}
        self.fail_next = 0

    def __call__(self, *, url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float):
        self.received.append(
            {"url": url, "headers": dict(headers), "payload": dict(payload), "timeout": timeout}
        )
        if self.fail_next > 0:
            self.fail_next -= 1
            return {"delivered": False, "http_status": None, "error": "ConnectionResetError: simulated"}
        verification = signing.verify_callback(
            headers,
            payload,
            secret=self.secret,
            window_seconds=self.window_seconds,
            nonce_guard=self.nonces if self.enforce_nonce else None,
        )
        event_id = str(payload.get("event_id", ""))
        key = str(payload.get("operation_id", ""))
        version = int(payload.get("event_version", 0) or 0)
        if not verification.ok:
            self._record(event_id, key, version, "rejected", verification.reason, payload)
            return {"delivered": False, "http_status": 401, "error": verification.reason}
        if event_id in self.applied_events:
            self._record(event_id, key, version, "deduped_event_id", "duplicate_event_id", payload)
            return {"delivered": True, "http_status": 200, "error": None}
        if version < self.latest_version.get(key, 0):
            self._record(event_id, key, version, "ignored_stale", "stale_event_version", payload)
            return {"delivered": True, "http_status": 200, "error": None}
        self.applied_events[event_id] = dict(payload)
        self.latest_version[key] = version
        self._record(event_id, key, version, "applied", "", payload)
        return {"delivered": True, "http_status": 200, "error": None}

    def _record(
        self, event_id: str, key: str, version: int, effect: str, reason: str, payload: Mapping[str, Any]
    ) -> None:
        self.outcomes.append(
            {
                "event_id": event_id,
                "operation_id": key,
                "event_version": version,
                "status": str(payload.get("status", "")),
                "settled": bool(payload.get("settled", False)),
                "effect": effect,
                "reason": reason,
            }
        )

    def applied(self, key: str) -> list[dict[str, Any]]:
        """接收侧真正生效的事件。"""
        return [o for o in self.outcomes if o["operation_id"] == key and o["effect"] == "applied"]

    def settlements(self, key: str) -> list[dict[str, Any]]:
        """接收侧真正生效的**结算类**事件（银行 succeeded / 商户 confirmed）。"""
        return [o for o in self.applied(key) if o["status"] in {"succeeded", "confirmed"}]

    def effects_of(self, key: str, effect: str) -> list[dict[str, Any]]:
        return [o for o in self.outcomes if o["operation_id"] == key and o["effect"] == effect]


@pytest.fixture()
def sink():
    verifier = CallbackSink()
    signing.set_callback_transport(verifier)
    yield verifier
    signing.set_callback_transport(None)


def _rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 夹具：银行 / 商户
# ---------------------------------------------------------------------------


@pytest.fixture()
def bank_app(tmp_path: Path):
    return bank.create_app(state_path=tmp_path / "bank_state.sqlite3")


@pytest.fixture()
def bank_client(bank_app):
    with TestClient(bank_app) as client:
        yield client


@pytest.fixture()
def merchant_app(tmp_path: Path):
    return merchant.create_app(state_path=tmp_path / "merchant_state.sqlite3")


@pytest.fixture()
def merchant_client(merchant_app):
    with TestClient(merchant_app) as client:
        yield client


def _transfer(key: str = "op_sandbox_0001", **overrides: Any) -> dict[str, Any]:
    body = {
        "stable_operation_ref": key,
        "owner_user_id": USER_ID,
        "payee_ref": "payee_lisi",
        "amount_minor": 30_000,
        "fee_minor": 0,
        "currency": "CNY",
        "memo": "生日鲜花",
    }
    body.update(overrides)
    return body


def _settle(client: TestClient, key: str, *, emit: bool = True, **extra: Any):
    payload = {"stable_operation_ref": key, "settle": True, "fail": False, "emit_callback": emit}
    payload.update(extra)
    return client.post("/bank/advance", json=payload)


def _order(key: str, quote_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "stable_operation_ref": key,
        "quote_id": quote_id,
        "quantity": 1,
        "customer_ref": USER_ID,
    }
    body.update(overrides)
    return body


def _quote_by_id(client: TestClient, quote_id: str) -> dict[str, Any]:
    quotes = client.get("/merchant/quotes").json()["quotes"]
    return next(q for q in quotes if q["quote_id"] == quote_id)


# ---------------------------------------------------------------------------
# 1) 同稳定键重复提交：一个 provider_ref、一次结算效果
# ---------------------------------------------------------------------------


def test_repeat_submit_returns_same_provider_ref_and_settles_once(bank_client, bank_app):
    body = _transfer()
    first = bank_client.post("/bank/transfers", json=body)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "accepted"
    assert first_body["provider_ref"].startswith("pb_")
    assert first_body["stable_operation_ref"] == body["stable_operation_ref"]
    assert first_body["operation_id"] == body["stable_operation_ref"]  # 稳定业务键

    for _ in range(3):
        again = bank_client.post("/bank/transfers", json=body)
        assert again.status_code == 200
        assert again.json()["provider_ref"] == first_body["provider_ref"]
        assert again.json()["accepted_at"] == first_body["accepted_at"]
        assert again.json()["status"] == "accepted"

    state_path = Path(bank_app.state.sandbox_store.path)
    assert len(_rows(state_path, "SELECT * FROM operations")) == 1
    assert len(_rows(state_path, "SELECT * FROM events")) == 1  # 受理事件只有一条

    # 同键异参：拒绝，且不产生第二条操作
    conflict = bank_client.post("/bank/transfers", json=_transfer(amount_minor=99_999))
    assert conflict.status_code == 409
    assert conflict.json()["failure_code"] == "IDEMPOTENCY_CONFLICT"
    assert len(_rows(state_path, "SELECT * FROM operations")) == 1

    settled = _settle(bank_client, body["stable_operation_ref"])
    assert settled.status_code == 200
    assert settled.json()["advanced"][0]["applied"] is True
    settled_at = settled.json()["advanced"][0]["settled_at"]

    # 再次推进：不得二次结算
    repeat = _settle(bank_client, body["stable_operation_ref"])
    assert repeat.json()["advanced"][0]["applied"] is False
    assert repeat.json()["advanced"][0]["skip_reason"] in {"already_settled", "not_settleable_succeeded"}

    view = bank_client.get(f"/bank/transfers/{body['stable_operation_ref']}").json()
    assert view["status"] == "succeeded"
    assert view["settled"] is True
    assert view["settled_at"] == settled_at
    assert view["effects_applied"] == 1
    assert view["effect_kinds"] == ["bank_transfer_settle"]

    effects = _rows(
        state_path,
        "SELECT effect_kind, COUNT(*) AS n FROM effects GROUP BY effect_kind",
    )
    assert effects == [{"effect_kind": "bank_transfer_settle", "n": 1}]


# ---------------------------------------------------------------------------
# 2) 迟到成功事件与重复回调（event_version 乱序）只结算一次
# ---------------------------------------------------------------------------


def test_late_success_and_duplicate_callbacks_settle_exactly_once(bank_client, bank_app, sink):
    key = "op_sandbox_late_01"
    assert bank_client.post("/bank/transfers", json=_transfer(key)).status_code == 200

    # v1 = accepted，正常投递
    first = bank_client.post("/bank/advance", json={"stable_operation_ref": key, "emit_callback": True}).json()
    assert [(e["event_version"], e["status"]) for e in first["events"]] == [(1, "accepted")]
    assert first["deliveries"][0]["delivered"] is True
    assert sink.settlements(key) == []  # accepted 不是结算事件

    # v2 = pending，正常投递（乱序对照的“较新版本”）
    pending = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "mark_pending": True, "emit_callback": True}
    ).json()
    assert [(e["event_version"], e["status"]) for e in pending["events"]] == [(2, "pending")]

    # v3 = succeeded，此刻**不投递**：模拟“外部已结算但回执迟到”
    settled = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "settle": True, "emit_callback": False}
    ).json()
    assert settled["advanced"][0]["applied"] is True
    assert settled["events"] == []
    settled_at = settled["advanced"][0]["settled_at"]

    # 迟到成功事件到达
    late_success = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "emit_callback": True}
    ).json()
    assert [(e["event_version"], e["status"]) for e in late_success["events"]] == [(3, "succeeded")]
    assert sink.applied_events[str(late_success["events"][0]["event_id"])]["status"] == "succeeded"

    # 重投历史 v1（版本更小的迟到事件）+ 3 次真重复投递（同一 event_id）
    replay = bank_client.post(
        "/bank/advance",
        json={
            "stable_operation_ref": key,
            "emit_callback": True,
            "replay_event_version": 1,
            "duplicate": 3,
        },
    ).json()
    kinds = [d["kind"] for d in replay["deliveries"]]
    assert kinds == ["late", "duplicate", "duplicate", "duplicate"]
    assert replay["duplicates_sent"] == 3
    assert {d["event_id"] for d in replay["deliveries"]} == {replay["deliveries"][0]["event_id"]}

    # 接收侧：旧版本被忽略、重复被去重，succeeded 只生效一次
    assert len(sink.settlements(key)) == 1
    assert sink.settlements(key)[0]["event_version"] == 3
    assert sink.latest_version[key] == 3
    assert len(sink.applied(key)) == 3  # accepted / pending / succeeded 各生效一次
    assert len(sink.effects_of(key, "deduped_event_id")) == 4  # 迟到重投的 v1 + 3 次重复投递
    assert not sink.effects_of(key, "rejected")

    # 提供方权威状态不变：一次结算、时间不变
    view = bank_client.get(f"/bank/transfers/{key}").json()
    assert view["status"] == "succeeded"
    assert view["settled"] is True
    assert view["settled_at"] == settled_at
    assert view["effects_applied"] == 1

    state_path = Path(bank_app.state.sandbox_store.path)
    assert len(_rows(state_path, "SELECT * FROM effects WHERE effect_kind='bank_transfer_settle'")) == 1
    versions = [r["event_version"] for r in _rows(state_path, "SELECT event_version FROM events ORDER BY event_version")]
    assert versions == [1, 2, 3]

    # 再次推进也不会改变结果
    again = _settle(bank_client, key).json()
    assert again["advanced"][0]["applied"] is False
    assert bank_client.get(f"/bank/transfers/{key}").json()["settled_at"] == settled_at


def test_out_of_order_delivery_descending_versions(bank_client, sink):
    """emit_order=desc：高版本先到、低版本后到，接收方不得按到达顺序回退状态。"""
    key = "op_sandbox_order_01"
    bank_client.post("/bank/transfers", json=_transfer(key))
    bank_client.post("/bank/advance", json={"stable_operation_ref": key, "settle": True, "emit_callback": False})
    emitted = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "emit_callback": True, "emit_order": "desc"}
    ).json()
    assert [e["event_version"] for e in emitted["events"]] == [2, 1]
    assert [o["event_version"] for o in sink.outcomes] == [2, 1]
    assert sink.settlements(key)[0]["event_version"] == 2
    assert sink.effects_of(key, "ignored_stale")[0]["event_version"] == 1


# ---------------------------------------------------------------------------
# 3) drop_response / delay_ms：客户端拿到超时语义，提供方状态仍可查询
# ---------------------------------------------------------------------------


def test_drop_response_keeps_provider_state_queryable(bank_client):
    key = "op_sandbox_timeout_01"
    faults = bank_client.post("/bank/faults", json={"delay_ms": 150, "drop_response": True})
    assert faults.status_code == 200
    assert faults.json()["faults"]["drop_response"] is True
    assert faults.json()["faults"]["effective_delay_ms"] == 150

    started = time.perf_counter()
    response = bank_client.post("/bank/transfers", json=_transfer(key))
    elapsed = time.perf_counter() - started
    assert response.status_code == 504
    body = response.json()
    assert body["status"] == "unknown"
    assert body["failure_code"] == "PROVIDER_TIMEOUT"
    assert body["error"]["retry_mode"] == "query_same_operation"
    assert body["stable_operation_ref"] == key
    assert elapsed >= 0.15  # delay_ms 真实生效（受 SANDBOX_MAX_DELAY_MS 限制）

    # 客户端拿不到应答，但提供方已受理：按原稳定键查询得到权威状态
    queried = bank_client.get(f"/bank/transfers/{key}")
    assert queried.status_code == 200
    assert queried.json()["status"] == "accepted"
    assert queried.json()["provider_ref"].startswith("pb_")

    # 清除故障后用同一稳定键重试：同一 provider_ref，不产生第二条操作
    assert bank_client.delete("/bank/faults").status_code == 200
    retried = bank_client.post("/bank/transfers", json=_transfer(key))
    assert retried.status_code == 200
    assert retried.json()["provider_ref"] == queried.json()["provider_ref"]
    assert retried.json()["status"] == "accepted"

    assert _settle(bank_client, key).json()["advanced"][0]["applied"] is True
    assert bank_client.get(f"/bank/transfers/{key}").json()["effects_applied"] == 1


def test_delay_ms_is_applied_and_clipped_to_cap(bank_client):
    assert bank_client.delete("/bank/faults").status_code == 200
    assert bank_client.post("/bank/faults", json={"delay_ms": 120}).json()["faults"]["delay_clipped"] is False

    started = time.perf_counter()
    ok = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_delay_01"))
    elapsed = time.perf_counter() - started
    assert ok.status_code == 200 and ok.json()["status"] == "accepted"
    assert elapsed >= 0.12

    # 超过上限只等待上限时长，但如实报告被截断
    clipped = bank_client.post("/bank/faults", json={"delay_ms": 99_999}).json()["faults"]
    assert clipped["delay_clipped"] is True
    assert clipped["effective_delay_ms"] == 500
    started = time.perf_counter()
    assert bank_client.post("/bank/transfers", json=_transfer("op_sandbox_delay_02")).status_code == 200
    assert time.perf_counter() - started < 2.0


def test_query_failure_does_not_change_authoritative_state(bank_client):
    key = "op_sandbox_queryfail_01"
    bank_client.post("/bank/transfers", json=_transfer(key))
    bank_client.post("/bank/faults", json={"query_fail_times": 1})

    failed = bank_client.get(f"/bank/transfers/{key}")
    assert failed.status_code == 503
    assert failed.json()["failure_code"] == "PROVIDER_QUERY_FAILED"
    assert failed.json()["error"]["retry_mode"] == "query_same_operation"

    recovered = bank_client.get(f"/bank/transfers/{key}")
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "accepted"  # 查询失败不等于业务失败
    assert bank_client.delete("/bank/faults").status_code == 200


# ---------------------------------------------------------------------------
# 4) 已结算撤单被拒绝且原状态不变
# ---------------------------------------------------------------------------


def test_cancel_after_settlement_is_rejected_without_changing_status(bank_client, bank_app):
    key = "op_sandbox_cancel_01"
    bank_client.post("/bank/transfers", json=_transfer(key))
    _settle(bank_client, key)
    before = bank_client.get(f"/bank/transfers/{key}").json()

    rejected = bank_client.post(f"/bank/transfers/{key}/cancel")
    assert rejected.status_code == 200
    payload = rejected.json()
    assert payload["status"] == "cancel_rejected"
    assert payload["reason"] == "already_settled"
    assert payload["operation_status"] == "succeeded"

    after = bank_client.get(f"/bank/transfers/{key}").json()
    assert after["status"] == "succeeded"  # 撤单失败绝不把原操作改成失败
    assert after["settled"] is True
    assert after["settled_at"] == before["settled_at"]
    assert after["effects_applied"] == before["effects_applied"] == 1
    assert after["cancelled_at"] is None

    state_path = Path(bank_app.state.sandbox_store.path)
    assert len(_rows(state_path, "SELECT * FROM effects WHERE effect_kind='bank_transfer_settle'")) == 1
    assert _rows(state_path, "SELECT status FROM operations") == [{"status": "succeeded"}]


def test_cancel_before_settlement_succeeds_and_blocks_later_settlement(bank_client, bank_app, sink):
    key = "op_sandbox_cancel_02"
    bank_client.post("/bank/transfers", json=_transfer(key))
    # 先投递受理事件，便于区分“撤单事件”与“结算事件”
    bank_client.post("/bank/advance", json={"stable_operation_ref": key, "emit_callback": True})
    assert sink.applied(key)[-1]["status"] == "accepted"

    cancelled = bank_client.post(f"/bank/transfers/{key}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["operation_status"] == "cancelled"

    # 撤单后推进结算：必须被拒绝（不得产生资金效果，也不得发出结算事件）
    advanced = _settle(bank_client, key, emit=True).json()
    assert advanced["advanced"][0]["applied"] is False
    assert advanced["advanced"][0]["skip_reason"] == "not_settleable_cancelled"
    assert [e["status"] for e in advanced["events"]] == ["cancelled"]
    assert all(e["status"] != "succeeded" for e in advanced["events"])

    view = bank_client.get(f"/bank/transfers/{key}").json()
    assert view["status"] == "cancelled"
    assert view["settled"] is False
    assert view["effect_kinds"] == ["cancel_accepted"]
    assert sink.settlements(key) == []
    assert sink.applied(key)[-1]["status"] == "cancelled"

    # 重复撤单是幂等的，不产生第二次效果
    again = bank_client.post(f"/bank/transfers/{key}/cancel").json()
    assert again["status"] == "cancelled"
    assert again["already_cancelled"] is True
    state_path = Path(bank_app.state.sandbox_store.path)
    assert len(_rows(state_path, "SELECT * FROM effects WHERE effect_kind='cancel_accepted'")) == 1

    # 失败的操作同样不能被撤单“改回去”
    fail_key = "op_sandbox_cancel_03"
    bank_client.post("/bank/transfers", json=_transfer(fail_key))
    bank_client.post(
        "/bank/advance", json={"stable_operation_ref": fail_key, "fail": True, "failure_code": "PAYEE_REJECTED"}
    )
    rejected_fail = bank_client.post(f"/bank/transfers/{fail_key}/cancel").json()
    assert rejected_fail["status"] == "cancel_rejected"
    assert rejected_fail["reason"] == "terminal_status_failed"
    assert bank_client.get(f"/bank/transfers/{fail_key}").json()["status"] == "failed"


# ---------------------------------------------------------------------------
# 5) 回调签名：对 timestamp + nonce + canonical_json(payload) 的 HMAC-SHA256
# ---------------------------------------------------------------------------


def test_callback_signature_accepts_valid_and_rejects_tampering(bank_client, sink):
    key = "op_sandbox_sign_01"
    bank_client.post("/bank/transfers", json=_transfer(key, amount_minor=12_345))
    emitted = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "settle": True, "emit_callback": True}
    ).json()
    delivery = emitted["deliveries"][0]
    headers = delivery["headers"]
    payload = delivery["payload"]

    # 正确签名：通过
    good = signing.verify_callback(headers, payload)
    assert good.ok, good.reason
    assert good.reason == ""
    assert good.event_id == payload["event_id"]

    # 独立复算：signing_string = timestamp + nonce + canonical_json(payload)
    import hashlib
    import hmac

    expected = hmac.new(
        DEFAULT_SECRET.encode("utf-8"),
        f"{headers['X-FinPilot-Timestamp']}{headers['X-FinPilot-Nonce']}{signing.canonical_json(payload)}".encode(
            "utf-8"
        ),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-FinPilot-Signature"] == expected
    assert headers["X-FinPilot-Timestamp"].isdigit()
    assert headers["X-FinPilot-Nonce"].startswith("n1_")
    assert payload["payload_hash"] == signing.payload_digest(payload)
    assert delivery["target_url"] == f"{DEFAULT_CALLBACK_BASE_URL}/internal/provider-events/{bank.PROVIDER_ID}"

    # 篡改 payload（金额）
    tampered = dict(payload)
    tampered["amount_minor"] = 1
    assert signing.verify_callback(headers, tampered).reason == signing.REASON_SIGNATURE
    # 篡改 payload 并同步改写 payload_hash：仍是签名不符
    tampered_hashed = signing.canonical_payload(tampered)
    assert signing.verify_callback(headers, tampered_hashed).reason == signing.REASON_SIGNATURE
    # 攻击者即使知道密钥、为“金额已改但哈希仍旧”的载荷重新签名，哈希校验仍独立拦截
    stale_hash_payload = {**tampered, "payload_hash": payload["payload_hash"]}
    stale_ts, stale_nonce = signing.unix_timestamp(), signing.new_nonce()
    stale_hash_headers = {
        signing.SIGNATURE_HEADER: signing.sign(DEFAULT_SECRET, stale_ts, stale_nonce, stale_hash_payload),
        signing.TIMESTAMP_HEADER: stale_ts,
        signing.NONCE_HEADER: stale_nonce,
        signing.PROVIDER_HEADER: bank.PROVIDER_ID,
    }
    assert (
        signing.verify_callback(stale_hash_headers, stale_hash_payload).reason == signing.REASON_PAYLOAD_HASH
    )

    # 篡改时间戳头
    for bad_ts in (str(int(headers["X-FinPilot-Timestamp"]) + 1), str(int(headers["X-FinPilot-Timestamp"]) - 1)):
        tampered_headers = dict(headers)
        tampered_headers["X-FinPilot-Timestamp"] = bad_ts
        assert signing.verify_callback(tampered_headers, payload).reason == signing.REASON_SIGNATURE
    # 时间戳不是整数
    assert signing.verify_callback({**headers, "X-FinPilot-Timestamp": "abc"}, payload).reason == signing.REASON_SIGNATURE

    # 时间窗外（正确签名但过期）→ 进入查询核验，不得静默丢失
    stale_headers = signing.sign_headers(
        payload, provider_id=bank.PROVIDER_ID, timestamp=str(int(time.time()) - 3600)
    )
    stale = signing.verify_callback(stale_headers, payload)
    assert stale.ok is False
    assert stale.reason == signing.REASON_OUT_OF_WINDOW
    assert stale.detail["skew_seconds"] >= 3600

    # 错误密钥
    wrong_secret = signing.sign_headers(payload, provider_id=bank.PROVIDER_ID, secret="not_the_secret")
    assert signing.verify_callback(wrong_secret, payload, secret=DEFAULT_SECRET).reason == signing.REASON_SIGNATURE

    # 缺少签名头
    assert signing.verify_callback({}, payload).reason == signing.REASON_MISSING_HEADER
    assert (
        signing.verify_callback({k: v for k, v in headers.items() if "Nonce" not in k}, payload).reason
        == signing.REASON_MISSING_HEADER
    )


def test_nonce_replay_and_out_of_order_nonce_are_rejected(bank_client):
    key = "op_sandbox_sign_02"
    bank_client.post("/bank/transfers", json=_transfer(key))
    emitted = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "settle": True, "emit_callback": True, "duplicate": 1}
    ).json()
    primary = next(d for d in emitted["deliveries"] if d["kind"] == "primary" and d["event_version"] == 2)
    duplicate = next(d for d in emitted["deliveries"] if d["kind"] == "duplicate")
    assert emitted["duplicates_sent"] == 1

    # 真重复投递使用同一 event_id，但 nonce 必须不同（否则被判为重放）
    assert primary["payload"]["event_id"] == duplicate["payload"]["event_id"]
    assert primary["headers"]["X-FinPilot-Nonce"] != duplicate["headers"]["X-FinPilot-Nonce"]

    guard = signing.NonceGuard()
    first = signing.verify_callback(primary["headers"], primary["payload"], nonce_guard=guard)
    assert first.ok
    replayed = signing.verify_callback(primary["headers"], primary["payload"], nonce_guard=guard)
    assert replayed.ok is False
    assert replayed.reason == signing.REASON_NONCE_REPLAY
    # 乱序/迟到的重复 nonce 同样失败（即便是另一条合法载荷）
    assert (
        signing.verify_callback(primary["headers"], duplicate["payload"], nonce_guard=guard).reason
        == signing.REASON_NONCE_REPLAY
    )
    # 新 nonce 可以通过
    assert signing.verify_callback(duplicate["headers"], duplicate["payload"], nonce_guard=guard).ok


def test_callback_is_recorded_when_transport_fails_and_retried_later(bank_client, sink):
    key = "op_sandbox_sign_03"
    bank_client.post("/bank/transfers", json=_transfer(key))
    sink.fail_next = 1
    failed = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "settle": True, "emit_callback": True}
    ).json()
    assert failed["deliveries"][0]["delivered"] is False
    assert "ConnectionResetError" in failed["deliveries"][0]["error"]

    # 投递失败不回滚业务状态；失败事件保持 pending，成功事件照常送达
    view = bank_client.get(f"/bank/transfers/{key}").json()
    assert view["status"] == "succeeded" and view["settled"] is True
    events = bank_client.get(f"/bank/events/{key}").json()["events"]
    assert [e["delivery_state"] for e in events] == ["pending", "delivered"]

    retried = bank_client.post(
        "/bank/advance", json={"stable_operation_ref": key, "emit_callback": True}
    ).json()
    assert [d["event_version"] for d in retried["deliveries"]] == [1]  # 只重投仍未送达的事件
    assert retried["deliveries"][0]["delivered"] is True
    assert len(sink.settlements(key)) == 1  # 结算事件仍然只生效一次

    log = bank_client.get("/bank/deliveries", params={"stable_operation_ref": key}).json()["deliveries"]
    assert [d["kind"] for d in log] == ["primary", "primary", "primary"]
    assert [d["delivered"] for d in log] == [False, True, True]


# ---------------------------------------------------------------------------
# 6) 商户：缺货、变价（不静默成交）、取消费与不可退条款
# ---------------------------------------------------------------------------


def test_merchant_preset_catalog_covers_required_samples(merchant_client):
    catalog = merchant_client.get("/merchant/quotes").json()
    assert catalog["count"] == len(merchant.QUOTE_CATALOG) == 7
    by_category: dict[str, list[dict[str, Any]]] = {}
    for quote in catalog["quotes"]:
        by_category.setdefault(quote["category"], []).append(quote)
    assert set(by_category) == {"flowers", "cake", "dining"}
    assert all(len(items) >= 2 for items in by_category.values())
    assert any(q["inventory"] == 0 for q in catalog["quotes"])
    assert any(q["refundable"] is False for q in catalog["quotes"])
    assert any(q["cancel_fee_minor"] > 0 for q in catalog["quotes"])

    filtered = merchant_client.get("/merchant/quotes", params={"merchant_id": "merchant_huayuxuan"}).json()
    assert filtered["count"] == 3
    assert all(q["merchant_id"] == "merchant_huayuxuan" for q in filtered["quotes"])
    assert merchant_client.get("/merchant/quotes", params={"category": "cake"}).json()["count"] == 2

    for quote in catalog["quotes"]:
        assert quote["observed_at"] and quote["expires_at"]
        assert quote["expires_at"] > quote["observed_at"]
        assert quote["terms_version"] == merchant.TERMS_VERSION


def test_merchant_out_of_stock_rejects_without_inventory_change(merchant_client):
    sold_out = next(
        q for q in merchant_client.get("/merchant/quotes").json()["quotes"] if q["inventory"] == 0
    )
    body = _order("book_oos_01", sold_out["quote_id"], expected_amount_minor=sold_out["amount_minor"])
    rejected = merchant_client.post("/merchant/orders", json=body)
    assert rejected.status_code == 200  # 业务终局看 status，不看 HTTP 200
    view = rejected.json()
    assert view["status"] == "rejected"
    assert view["reason"] == "out_of_stock"
    assert view["failure_code"] == "OUT_OF_STOCK"
    assert view["inventory_remaining"] == 0
    assert view["effects_applied"] == 0

    repeat = merchant_client.post("/merchant/orders", json=body).json()
    assert repeat["provider_ref"] == view["provider_ref"]
    assert repeat["status"] == "rejected"
    assert _quote_by_id(merchant_client, sold_out["quote_id"])["inventory"] == 0


def test_merchant_price_change_is_not_silently_accepted(merchant_client, merchant_app):
    original = next(
        q
        for q in merchant_client.get("/merchant/quotes").json()["quotes"]
        if q["inventory"] > 0 and q["refundable"] and q["cancel_fee_minor"] > 0
    )
    new_amount = original["amount_minor"] + 5_000
    updated = merchant_client.post(
        f"/merchant/quotes/{original['quote_id']}", json={"amount_minor": new_amount}
    ).json()
    assert updated["quote"]["amount_minor"] == new_amount

    stale = merchant_client.post(
        "/merchant/orders",
        json=_order("book_price_01", original["quote_id"], expected_amount_minor=original["amount_minor"]),
    ).json()
    assert stale["status"] == "rejected"
    assert stale["reason"] == "price_changed"
    assert stale["price_changed"] is True
    assert stale["expected_amount_minor"] == original["amount_minor"]
    assert stale["current_amount_minor"] == new_amount
    assert stale["amount_minor"] == new_amount  # 如实返回新价
    assert stale["inventory_remaining"] == original["inventory"]  # 未占用库存
    assert stale["effects_applied"] == 0

    # 库存未变、也未按新价成交
    assert _quote_by_id(merchant_client, original["quote_id"])["inventory"] == original["inventory"]
    log = merchant_client.get("/merchant/orders").json()["orders"]
    assert [o["status"] for o in log] == ["rejected"]

    # 客户端须用新价格重新确认（新的稳定业务键）后才能真正预订
    accepted = merchant_client.post(
        "/merchant/orders",
        json=_order("book_price_02", original["quote_id"], expected_amount_minor=new_amount),
    ).json()
    assert accepted["status"] == "accepted"
    assert accepted["amount_minor"] == new_amount
    assert accepted["inventory_remaining"] == original["inventory"] - 1

    # 条款版本不一致同样拒绝
    terms = merchant_client.post(
        "/merchant/orders",
        json=_order("book_price_03", original["quote_id"], expected_terms_version="terms-1999.01"),
    ).json()
    assert terms["status"] == "rejected"
    assert terms["reason"] == "terms_changed"
    state_path = Path(merchant_app.state.sandbox_store.path)
    assert _rows(state_path, "SELECT COUNT(*) AS n FROM effects WHERE effect_kind='reserve_inventory'") == [
        {"n": 1}
    ]


def test_merchant_cancel_fee_and_non_refundable_terms(merchant_client, merchant_app):
    refundable = next(
        q
        for q in merchant_client.get("/merchant/quotes").json()["quotes"]
        if q["inventory"] > 0 and q["refundable"] and q["cancel_fee_minor"] > 0
    )
    booked = merchant_client.post(
        "/merchant/orders",
        json=_order("book_cancel_01", refundable["quote_id"], expected_amount_minor=refundable["amount_minor"]),
    ).json()
    assert booked["status"] == "accepted"
    assert booked["total_minor"] == refundable["amount_minor"] + refundable["fee_minor"]
    assert booked["inventory_remaining"] == refundable["inventory"] - 1

    cancelled = merchant_client.post("/merchant/orders/book_cancel_01/cancel")
    assert cancelled.status_code == 200
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["cancel_fee_charged_minor"] == refundable["cancel_fee_minor"]
    assert payload["refund_minor"] == booked["total_minor"] - refundable["cancel_fee_minor"]
    assert payload["operation_status"] == "cancelled"

    # 按条款收取消费 + 归还库存，均只发生一次
    assert _quote_by_id(merchant_client, refundable["quote_id"])["inventory"] == refundable["inventory"]
    state_path = Path(merchant_app.state.sandbox_store.path)
    effect_rows = _rows(
        state_path,
        "SELECT effect_kind, COUNT(*) AS n FROM effects WHERE stable_operation_ref='book_cancel_01' "
        "GROUP BY effect_kind ORDER BY effect_kind",
    )
    assert effect_rows == [
        {"effect_kind": "cancel_fee_charged", "n": 1},
        {"effect_kind": "inventory_restored", "n": 1},
        {"effect_kind": "reserve_inventory", "n": 1},
    ]
    assert merchant_client.post("/merchant/orders/book_cancel_01/cancel").json()["already_cancelled"] is True

    # 不可退条款：拒绝撤单，原订单状态与库存都不变
    non_refundable = next(
        q
        for q in merchant_client.get("/merchant/quotes").json()["quotes"]
        if q["refundable"] is False and q["inventory"] > 0
    )
    booked2 = merchant_client.post(
        "/merchant/orders", json=_order("book_cancel_02", non_refundable["quote_id"])
    ).json()
    assert booked2["status"] == "accepted"
    rejected = merchant_client.post("/merchant/orders/book_cancel_02/cancel").json()
    assert rejected["status"] == "cancel_rejected"
    assert rejected["reason"] == "non_refundable_terms"
    assert rejected["operation_status"] == "accepted"  # 原状态不变
    assert rejected["cancel_fee_minor"] == non_refundable["cancel_fee_minor"]
    current = merchant_client.get("/merchant/orders/book_cancel_02").json()
    assert current["status"] == "accepted"
    assert current["cancellable"] is False
    assert _quote_by_id(merchant_client, non_refundable["quote_id"])["inventory"] == non_refundable["inventory"] - 1

    # 已确认的订单不可撤单
    confirmed = merchant_client.post(
        "/merchant/advance", json={"stable_operation_ref": "book_cancel_02", "confirm": True, "emit_callback": True}
    ).json()
    assert confirmed["advanced"][0]["to_status"] == "confirmed"
    assert confirmed["advanced"][0]["settled"] is True
    late_cancel = merchant_client.post("/merchant/orders/book_cancel_02/cancel").json()
    assert late_cancel["status"] == "cancel_rejected"
    assert late_cancel["reason"] == "already_settled"
    assert merchant_client.get("/merchant/orders/book_cancel_02").json()["status"] == "confirmed"


def test_merchant_confirm_and_duplicate_callbacks_settle_once(merchant_client, merchant_app, sink):
    quote = next(q for q in merchant_client.get("/merchant/quotes").json()["quotes"] if q["inventory"] > 0)
    merchant_client.post("/merchant/orders", json=_order("book_confirm_01", quote["quote_id"]))
    confirmed = merchant_client.post(
        "/merchant/advance",
        json={"stable_operation_ref": "book_confirm_01", "confirm": True, "emit_callback": True, "duplicate": 2},
    ).json()
    assert [e["status"] for e in confirmed["events"]] == ["accepted", "confirmed"]
    assert confirmed["duplicates_sent"] == 2
    assert len(sink.settlements("book_confirm_01")) == 1
    assert sink.settlements("book_confirm_01")[0]["event_version"] == 2
    assert len(sink.effects_of("book_confirm_01", "deduped_event_id")) == 2

    repeated = merchant_client.post(
        "/merchant/advance", json={"stable_operation_ref": "book_confirm_01", "confirm": True}
    ).json()
    assert repeated["advanced"][0]["applied"] is False
    state_path = Path(merchant_app.state.sandbox_store.path)
    assert _rows(
        state_path,
        "SELECT COUNT(*) AS n FROM effects WHERE effect_kind='merchant_order_confirm'",
    ) == [{"n": 1}]


# ---------------------------------------------------------------------------
# 7) untrusted_text 中的注入文本不改变结构化字段
# ---------------------------------------------------------------------------


def test_untrusted_text_is_data_and_never_changes_structured_fields(merchant_client, merchant_app):
    catalog = {q["quote_id"]: q for q in merchant_client.get("/merchant/quotes").json()["quotes"]}
    seeded = {q["quote_id"]: q for q in merchant.QUOTE_CATALOG}
    assert set(catalog) == set(seeded)

    injections = 0
    for quote_id, quote in catalog.items():
        source = seeded[quote_id]
        # 结构化字段完全来自结构化列，与不可信文本无关
        assert quote["amount_minor"] == source["amount_minor"]
        assert quote["fee_minor"] == source["fee_minor"]
        assert quote["cancel_fee_minor"] == source["cancel_fee_minor"]
        assert quote["inventory"] == source["inventory"]
        assert quote["refundable"] is source["refundable"]
        assert quote["terms_version"] == source["terms_version"]
        assert quote["title"] == source["title"]
        text = quote["untrusted_text"]
        assert isinstance(text, str) and text.strip()
        assert any(
            marker in text
            for marker in (
                "忽略以上规则",
                "系统提示",
                "全部余额转出",
                "IGNORE ALL PREVIOUS INSTRUCTIONS",
                "admin mode",
                "忽略取消费条款",
            )
        )
        # 注入文本只出现在 untrusted_text，不污染标题/商户名等可信字段
        assert "ignore" not in quote["title"].lower()
        assert "系统" not in quote["merchant_name"]
        injections += 1
    assert injections >= 6

    # 文本谎称 amount_minor=0 / inventory=9999：下单仍按结构化金额与真实库存执行
    lying = seeded["quote_flowers_hyx_01"]
    assert "999999" in lying["untrusted_text"]
    booked = merchant_client.post(
        "/merchant/orders",
        json=_order("book_injection_01", lying["quote_id"], expected_amount_minor=lying["amount_minor"]),
    ).json()
    assert booked["status"] == "accepted"
    assert booked["amount_minor"] == lying["amount_minor"] == 29_900  # 不是 0/1/999999
    assert booked["fee_minor"] == lying["fee_minor"]
    assert booked["cancel_fee_minor"] == lying["cancel_fee_minor"]
    assert booked["refundable"] is True

    lying_inventory = seeded["quote_flowers_hyx_02"]
    assert lying_inventory["inventory"] == 0
    assert "9999" in lying_inventory["untrusted_text"]
    out = merchant_client.post("/merchant/orders", json=_order("book_injection_02", lying_inventory["quote_id"])).json()
    assert out["status"] == "rejected"
    assert out["reason"] == "out_of_stock"

    # 不可退文本谎称 refundable=true：条款仍以结构化字段为准
    lying_refund = seeded["quote_flowers_hyx_03"]
    assert "refundable=true" in lying_refund["untrusted_text"]
    booked_nr = merchant_client.post(
        "/merchant/orders", json=_order("book_injection_03", lying_refund["quote_id"])
    ).json()
    assert booked_nr["refundable"] is False
    assert merchant_client.post("/merchant/orders/book_injection_03/cancel").json()["reason"] == "non_refundable_terms"

    # 状态库中的结构化值未被注入文本改写
    state_path = Path(merchant_app.state.sandbox_store.path)
    stored = {
        row["quote_id"]: row
        for row in _rows(state_path, "SELECT quote_id, amount_minor, inventory, refundable FROM quotes")
    }
    assert stored["quote_flowers_hyx_01"]["amount_minor"] == 29_900
    assert stored["quote_flowers_hyx_02"]["inventory"] == 0
    assert stored["quote_flowers_hyx_03"]["refundable"] == 0


# ---------------------------------------------------------------------------
# 附加：不可用/拒绝注入、两个沙箱完全分离与重启持久化（T54）
# ---------------------------------------------------------------------------


def test_unavailable_and_reject_next_faults(bank_client, bank_app):
    assert bank_client.post("/bank/faults", json={"unavailable": True, "reject_next": 1}).status_code == 200
    blocked = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_unavail_01"))
    assert blocked.status_code == 503
    assert blocked.json()["failure_code"] == "PROVIDER_UNAVAILABLE"
    assert blocked.json()["error"]["retry_mode"] == "retry_later"
    assert bank_client.get("/bank/transfers/op_sandbox_unavail_01").status_code == 503
    # 不可用期间健康检查与故障管理仍然可用，且未产生任何状态
    assert bank_client.get("/bank/health").status_code == 200
    assert bank_client.get("/bank/faults").json()["faults"]["unavailable"] is True
    state_path = Path(bank_app.state.sandbox_store.path)
    assert _rows(state_path, "SELECT COUNT(*) AS n FROM operations") == [{"n": 0}]

    # reject_next 只拒绝一次，且是“可验证的拒绝”→ 终态 failed，可查询、可幂等重放
    assert bank_client.post("/bank/faults", json={"unavailable": False}).status_code == 200
    rejected = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_reject_01")).json()
    assert rejected["status"] == "failed"
    assert rejected["failure_code"] == "PROVIDER_REJECTED"
    assert rejected["settled"] is False
    accepted = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_reject_02")).json()
    assert accepted["status"] == "accepted"
    replay = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_reject_01")).json()
    assert replay["provider_ref"] == rejected["provider_ref"]
    assert replay["status"] == "failed"
    assert len(_rows(state_path, "SELECT * FROM operations")) == 2
    assert _rows(state_path, "SELECT COUNT(*) AS n FROM effects") == [{"n": 0}]


def test_fault_config_rejects_unknown_fields(bank_client, merchant_client):
    for client, path in (
        (bank_client, "/bank/faults"),
        (merchant_client, "/merchant/faults"),
    ):
        response = client.post(path, json={"delay_ms": 1, "sudo": True})
        assert response.status_code == 422
        assert response.json()["failure_code"] == "FAULT_CONFIG_ERROR"
        assert client.post(path, json={"delay_ms": -1}).status_code == 422
        assert client.post(path, json={"drop_response": "yes"}).status_code == 422
        assert client.get(path).json()["faults"]["delay_ms"] == 0


def test_bank_and_merchant_sandboxes_use_separate_sqlite_state(bank_app, merchant_app, bank_client, merchant_client):
    bank_path = Path(bank_app.state.sandbox_store.path)
    merchant_path = Path(merchant_app.state.sandbox_store.path)
    assert bank_path != merchant_path
    assert bank_path.exists() and merchant_path.exists()
    assert bank_path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert merchant_path.read_bytes()[:16] == b"SQLite format 3\x00"

    bank_client.post("/bank/transfers", json=_transfer("op_sandbox_sep_01"))
    # 银行侧操作不出现在商户库
    assert _rows(bank_path, "SELECT COUNT(*) AS n FROM operations") == [{"n": 1}]
    assert _rows(merchant_path, "SELECT COUNT(*) AS n FROM operations") == [{"n": 0}]
    assert bank_client.get("/bank/health").json()["state"]["provider_id"] == bank.PROVIDER_ID
    assert merchant_client.get("/merchant/health").json()["state"]["provider_id"] == merchant.PROVIDER_ID
    assert bank.PROVIDER_ID != merchant.PROVIDER_ID

    tables = {row["name"] for row in _rows(bank_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"operations", "effects", "events", "deliveries", "faults", "quotes", "meta"} <= tables


def test_sandbox_modules_do_not_import_main_bank_implementation():
    """技术独立性：提供方沙箱不得 import finpilot 或 SQLAlchemy。"""
    offenders: list[str] = []
    for name in ("__init__.py", "store.py", "faults.py", "signing.py", "bank.py", "merchant.py"):
        text = (SANDBOX_DIR / name).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "finpilot" in stripped or "sqlalchemy" in stripped.lower():
                offenders.append(f"{name}:{lineno}: {stripped}")
    assert offenders == []
    assert "sqlite3" not in sys.builtin_module_names  # sqlite3 是扩展模块，仍由标准库提供


def test_state_survives_restart_and_reports_same_provider_ref(tmp_path, sink):
    state_path = tmp_path / "restart_state.sqlite3"
    first_app = bank.create_app(state_path=state_path)
    with TestClient(first_app) as client:
        accepted = client.post("/bank/transfers", json=_transfer("op_sandbox_restart_01")).json()
        client.post(
            "/bank/advance", json={"stable_operation_ref": "op_sandbox_restart_01", "settle": True, "emit_callback": True}
        )

    # 全新进程/新实例复用同一状态库：权威状态与效果不重复
    second_app = bank.create_app(state_path=state_path)
    with TestClient(second_app) as client:
        view = client.get("/bank/transfers/op_sandbox_restart_01").json()
        assert view["provider_ref"] == accepted["provider_ref"]
        assert view["status"] == "succeeded" and view["settled"] is True
        assert view["effects_applied"] == 1
        again = client.post("/bank/transfers", json=_transfer("op_sandbox_restart_01")).json()
        assert again["provider_ref"] == accepted["provider_ref"]

    assert len(_rows(state_path, "SELECT * FROM effects")) == 1
    assert len(_rows(state_path, "SELECT * FROM operations")) == 1
    assert len(_rows(state_path, "SELECT * FROM events")) == 2


def test_validation_rejects_unknown_and_dangerous_fields(bank_client, merchant_client):
    bad = bank_client.post("/bank/transfers", json={**_transfer("op_sandbox_valid_01"), "force_success": True})
    assert bad.status_code == 422
    assert bad.json()["failure_code"] == "VALIDATION_ERROR"
    assert bad.json()["error"]["details"][0]["field"] == "unknown_fields"

    missing = bank_client.post("/bank/transfers", json={"stable_operation_ref": "op_x"})
    assert missing.status_code == 422
    assert {item["field"] for item in missing.json()["error"]["details"]} >= {"owner_user_id", "payee_ref", "amount_minor"}

    bad_currency = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_valid_02", currency="USD"))
    assert bad_currency.status_code == 422
    bad_amount = bank_client.post("/bank/transfers", json=_transfer("op_sandbox_valid_03", amount_minor=0))
    assert bad_amount.status_code == 422
    bad_quantity = merchant_client.post(
        "/merchant/orders", json=_order("book_valid_01", "quote_cake_tx_01", quantity=0)
    )
    assert bad_quantity.status_code == 422
    unknown_quote = merchant_client.post("/merchant/orders", json=_order("book_valid_02", "quote_unknown"))
    assert unknown_quote.status_code == 404
    assert unknown_quote.json()["failure_code"] == "NOT_FOUND"
    # 未知对象查询：不泄露内部堆栈，返回结构化 404
    assert bank_client.get("/bank/transfers/op_not_exists").status_code == 404


def test_openapi_and_health_expose_expected_endpoints(bank_client, merchant_client):
    for client, prefix in ((bank_client, "/bank"), (merchant_client, "/merchant")):
        health = client.get(f"{prefix}/health").json()
        assert health["status"] == "ok"
        assert health["callback_path"] == f"/internal/provider-events/{health['provider_id']}"
        schema = client.get(f"{prefix}/openapi.json").json()
        paths = set(schema["paths"])
        assert f"{prefix}/advance" in paths
        assert f"{prefix}/faults" in paths
    assert "/bank/transfers/{stable_operation_ref}/cancel" in bank_client.get("/bank/openapi.json").json()["paths"]
    assert "/merchant/quotes" in merchant_client.get("/merchant/openapi.json").json()["paths"]


def test_health_reports_delivery_secret_source_not_value(bank_client):
    health = bank_client.get("/bank/health").json()
    assert "callback_base_url" in health
    assert json.dumps(health, ensure_ascii=False).find(DEFAULT_SECRET) == -1  # 不回显密钥


def test_default_state_paths_match_documented_defaults(monkeypatch):
    """默认状态库路径：两个服务必须是不同文件，且落在 <仓库根>/.state/ 下。"""
    monkeypatch.delenv("BANK_SANDBOX_STATE", raising=False)
    monkeypatch.delenv("MERCHANT_SANDBOX_STATE", raising=False)
    bank_path = store_mod.default_state_path(bank.STATE_ENV, bank.DEFAULT_STATE)
    merchant_path = store_mod.default_state_path(merchant.STATE_ENV, merchant.DEFAULT_STATE)
    assert bank_path == store_mod.repo_root() / ".state" / "bank_sandbox.sqlite3"
    assert merchant_path == store_mod.repo_root() / ".state" / "merchant_sandbox.sqlite3"
    assert bank_path != merchant_path
    assert ".state\\.state" not in str(bank_path)
    assert bank_path.suffix == ".sqlite3"

    monkeypatch.setenv("BANK_SANDBOX_STATE", str(REPO_ROOT / "tmp_override.sqlite3"))
    overridden = store_mod.default_state_path(bank.STATE_ENV, bank.DEFAULT_STATE)
    assert overridden == REPO_ROOT / "tmp_override.sqlite3"