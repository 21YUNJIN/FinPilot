"""HTTP 契约端到端：会话、任务、确认、强验证、隔离、版本冲突、隐私（V5 16.1、T20/T32/T40/T47）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from finpilot.kernel.identity import totp_at

pytestmark = [pytest.mark.integration]


def _login(client, user_id: str, password: str = "Passw0rd!demo") -> dict:
    response = client.post("/v1/auth/sessions", json={"user_id": user_id, "password": password, "channel": "app"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    return {"Authorization": f"Bearer {payload['data']['token']}"}


def test_health_and_meta_endpoints(client):
    health = client.get("/healthz").json()
    assert health["status"] in ("ok", "degraded")
    assert health["mode"] in ("mock", "offline_replay", "live")
    assert "模拟渠道" in health["labeling"]

    tools = client.get("/v1/meta/tools").json()["data"]
    names = {item["tool"] for item in tools["tools"]}
    assert {"transfer.prepare", "transfer.submit", "bill.report", "card.report_lost"} <= names
    # 人类确认与运营端点绝不出现在模型可见集合
    assert "transfer.submit" not in tools["agent_exposed"]
    assert "operator.card_decision" not in tools["agent_exposed"]

    policy = client.get("/v1/meta/policy").json()["data"]
    assert policy["transfer_daily_yellow_limit_minor"] == 100_000
    assert any("恰好 100000 分" in note for note in policy["notes"])


def test_login_requires_valid_credentials(client, seed):
    bad = client.post("/v1/auth/sessions", json={"user_id": "user_demo_01", "password": "wrong-password", "channel": "app"})
    assert bad.status_code == 401
    body = bad.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert "不存在" not in body["error"]["safe_message"], "不得泄露用户是否存在"


def test_missing_token_returns_envelope(client):
    response = client.get("/v1/tasks")
    assert response.status_code == 401
    assert response.json()["error"]["retry_mode"] == "after_reauthorization"


def test_yellow_transfer_flow_end_to_end(client, seed):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "给李四转100元", "channel": "app"}, headers=headers).json()
    assert created["status"] == "ok"
    assert created["provenance"]["required_level"] == "YELLOW"
    data = created["data"]
    plan = data["plan"]
    assert plan["feasible"] is True

    confirmed = client.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"scope_hash": plan["scope_hash"], "expected_revision": data["revision"]},
        headers=headers,
    ).json()
    assert confirmed["status"] == "ok"
    execution = confirmed["data"]["execution"]
    assert execution["status"] == "succeeded"

    task = client.get(f"/v1/tasks/{data['task_id']}", headers=headers).json()["data"]
    assert task["status"] == "succeeded"
    operation = task["operations"][0]
    assert operation["status"] == "succeeded"
    assert operation["effect_confirmed"] is True
    assert operation["provider_ref"]

    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    salary = next(a for a in accounts["accounts"] if a["account_id"] == "account_salary")
    assert salary["posted_balance_minor"] == 890_000
    assert accounts["daily_usage"]["settled_transfer_minor"] == 10_000


def test_scope_hash_mismatch_is_rejected(client, seed):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "给李四转100元"}, headers=headers).json()["data"]
    plan = created["plan"]
    response = client.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"scope_hash": "0" * 64, "expected_revision": created["revision"]},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    # 未确认前不得产生任何操作或资金效果
    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    assert accounts["accounts"][0]["posted_balance_minor"] == 900_000


def test_stale_revision_conflicts(client, seed):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "给李四转100元"}, headers=headers).json()["data"]
    response = client.post(
        f"/v1/tasks/{created['task_id']}/messages",
        json={"message": "改成50元", "expected_revision": created["revision"] + 5},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert response.json()["error"]["details"]["current_revision"] == created["revision"]


def test_red_flow_requires_totp_and_settles_once(client, seed, clock):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "从储蓄卡给李四转2000元"}, headers=headers).json()
    assert created["provenance"]["required_level"] == "RED"
    data = created["data"]
    plan = data["plan"]

    confirmed = client.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"scope_hash": plan["scope_hash"], "expected_revision": data["revision"]},
        headers=headers,
    ).json()["data"]
    assert confirmed["status"] == "challenge_required"
    challenge_id = confirmed["challenge_id"]

    wrong = client.post(
        f"/v1/auth/challenges/{challenge_id}/verify", json={"factor_response": "000000"}, headers=headers
    )
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "CHALLENGE_FAILED"

    # 使用服务端同一时间源（可注入时钟）生成的真实 TOTP
    code, _ = totp_at(seed["totp_secret"], clock.now())
    verified = client.post(
        f"/v1/auth/challenges/{challenge_id}/verify", json={"factor_response": code}, headers=headers
    ).json()
    assert verified["status"] == "ok", verified
    assert verified["data"]["execution"]["status"] == "succeeded"

    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    savings = next(a for a in accounts["accounts"] if a["account_id"] == "account_savings")
    assert savings["posted_balance_minor"] == 1_300_000, "2000 元只扣一次（1500000 − 200000）"


def test_clarification_then_answer(client, seed):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "给张三转100元", "channel": "im"}, headers=headers).json()["data"]
    assert created["status"] == "clarifying"
    clarification = created["clarifications"][0]
    assert clarification["field"] == "payee_id"
    assert len(clarification["options"]) == 2, "同名联系人必须先澄清"
    assert "****" in clarification["options"][0]["label"], "选项必须脱敏展示"

    answered = client.post(
        f"/v1/tasks/{created['task_id']}/messages",
        json={"message": clarification["options"][0]["value"], "expected_revision": created["revision"]},
        headers=headers,
    ).json()
    assert answered["status"] == "ok"
    assert answered["data"]["plan"]["feasible"] is True


def test_unsupported_business_is_blocked_without_writes(client, seed):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "帮我炒股，买入贵州茅台"}, headers=headers).json()
    assert created["data"]["status"] == "blocked"
    assert created["data"]["goal"]["facts"]["unsupported_reason"] == "securities_or_crypto"
    assert created["data"]["plan"] is None
    assert client.get("/v1/operations", headers=headers).json()["data"]["items"] == []


def test_cross_user_isolation_on_tasks_operations_and_cards(client, seed):
    owner = _login(client, "user_demo_01")
    other = _login(client, "user_demo_02")
    created = client.post("/v1/tasks", json={"message": "给李四转100元"}, headers=owner).json()["data"]
    plan = created["plan"]
    client.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"scope_hash": plan["scope_hash"], "expected_revision": created["revision"]},
        headers=owner,
    )
    operation_id = client.get(f"/v1/tasks/{created['task_id']}", headers=owner).json()["data"]["operations"][0]["operation_id"]

    for path in (f"/v1/tasks/{created['task_id']}", f"/v1/operations/{operation_id}"):
        response = client.get(path, headers=other)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "OBJECT_NOT_ACCESSIBLE"

    # 列表也不得串户
    assert client.get("/v1/tasks", headers=other).json()["data"]["items"] == []
    assert client.get("/v1/operations", headers=other).json()["data"]["items"] == []


def test_cancel_reports_inflight_and_stops_pending_steps(client, seed):
    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "给李四转100元"}, headers=headers).json()["data"]
    cancelled = client.post(f"/v1/tasks/{created['task_id']}/cancel", headers=headers).json()
    assert cancelled["status"] == "ok"
    assert cancelled["data"]["status"] == "cancelled"
    assert "撤销只停止尚未提交的动作" in cancelled["data"]["note"]
    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    assert accounts["accounts"][0]["posted_balance_minor"] == 900_000


def test_error_envelope_shape_and_no_internal_leak(client, seed):
    headers = _login(client, "user_demo_01")
    response = client.post("/v1/tasks", json={"message": ""}, headers=headers)
    assert response.status_code in (400, 422)
    body = response.json()
    assert "error" in body or "detail" in body
    text = response.text
    for leak in ("Traceback", "psycopg", "SELECT ", "password_hash", "totp"):
        assert leak not in text, f"错误响应泄露内部实现: {leak}"


def test_privacy_export_and_delete(client, seed):
    headers = _login(client, "user_demo_01")
    client.post("/v1/tasks", json={"message": "给李四转100元"}, headers=headers)
    exported = client.get("/v1/privacy/export", headers=headers).json()["data"]
    assert exported["export"]["user_id"] == "user_demo_01"
    assert "redaction_summary" in exported

    retention = client.get("/v1/privacy/retention", headers=headers).json()["data"]
    assert retention["chat_retention_days"] == 30
    assert retention["audit_retention_days"] == 90

    deleted = client.post("/v1/privacy/delete", json={"scope": "chat"}, headers=headers).json()["data"]
    assert deleted["deleted"]["messages"] >= 2
    assert "不随聊天删除而消失" in deleted["note"]


def test_operator_actions_require_operator_role(client, seed):
    customer = _login(client, "user_demo_01")
    forbidden = client.get("/operator/cases", headers=customer)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    operator = _login(client, "user_operator_01")
    allowed = client.get("/operator/cases", headers=operator)
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ok"


def test_audit_chain_is_verifiable_after_flow(client, seed):
    """审计必须能串起授权→提交→资金效果，且哈希链自检通过。"""
    from finpilot.db import new_session
    from finpilot.security.audit import signed_digest, verify_chain

    headers = _login(client, "user_demo_01")
    created = client.post("/v1/tasks", json={"message": "给李四转100元"}, headers=headers).json()["data"]
    plan = created["plan"]
    client.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"scope_hash": plan["scope_hash"], "expected_revision": created["revision"]},
        headers=headers,
    )

    session = new_session()
    try:
        result = verify_chain(session)
        assert result["ok"] is True, result
        assert result["checked"] >= 3
        digest = signed_digest(session)
        assert digest["head_hash"]
        assert digest["signature"]
    finally:
        session.close()