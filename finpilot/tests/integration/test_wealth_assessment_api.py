"""S3 风险测评：问卷入口、幂等、以及对话触发时不得带空答案去执行（D1/D2）。

背景：此前 ``risk.assess`` 的 ``answers`` 永远为空，确认后执行抛 ``PARAM_INVALID``，
任务显示 failed、operation 永久停在 dispatching，且没有任何 HTTP 入口能提交答案。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]

PASSWORD = "Passw0rd!demo"


def _login(client, user_id: str = "user_demo_01") -> dict:
    response = client.post("/v1/auth/sessions", json={"user_id": user_id, "password": PASSWORD, "channel": "app"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def _mid_answers(questions: list[dict]) -> dict[str, str]:
    """每题取中间档，保证落在合法选项上（不依赖具体 option_id 字面量）。"""
    return {q["question_id"]: q["options"][2]["option_id"] for q in questions}


def test_questionnaire_is_versioned_and_covers_six_dimensions(client, seed):
    headers = _login(client)
    payload = client.get("/v1/wealth/questionnaire", headers=headers).json()
    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["questionnaire_version"]
    assert [item["dimension"] for item in data["dimensions"]] == [
        "investment_experience",
        "loss_tolerance",
        "risk_attitude",
        "investment_horizon",
        "liquidity_need",
        "capital_purpose",
    ]
    assert len(data["questions"]) == 6
    for question in data["questions"]:
        assert len(question["options"]) == 5
        assert all("score" in option for option in question["options"])


def test_submit_answers_creates_profile_and_is_idempotent(client, seed):
    headers = _login(client)
    questionnaire = client.get("/v1/wealth/questionnaire", headers=headers).json()["data"]
    answers = _mid_answers(questionnaire["questions"])

    first = client.post("/v1/wealth/assessment", json={"answers": answers}, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "ok"
    data = body["data"]["data"]
    assert data["risk_level"] in {"R1", "R2", "R3", "R4", "R5"}
    assert data["expires_at"]
    assert data["answers_hash"]

    view = client.get("/v1/wealth/assessment", headers=headers).json()["data"]
    assert view is not None
    assert view["risk_profile_id"] == data["profile_id"]

    # 同一份答案重复提交：幂等，不产生第二条有效测评
    second = client.post("/v1/wealth/assessment", json={"answers": answers}, headers=headers).json()
    assert second["status"] == "ok"
    assert second["data"]["data"]["profile_id"] == data["profile_id"]
    assert "幂等" in second["data"]["note"]


def test_submit_rejects_missing_dimension(client, seed):
    headers = _login(client)
    questionnaire = client.get("/v1/wealth/questionnaire", headers=headers).json()["data"]
    answers = _mid_answers(questionnaire["questions"])
    missing_key = next(q["question_id"] for q in questionnaire["questions"] if q["dimension"] == "capital_purpose")
    answers.pop(missing_key)

    response = client.post("/v1/wealth/assessment", json={"answers": answers}, headers=headers)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "PARAM_INVALID"
    assert "missing_answers" in (error.get("reason_codes") or [])

    assert client.get("/v1/wealth/assessment", headers=headers).json()["data"] is None


def test_chat_assessment_is_blocked_with_questionnaire_guidance(client, seed):
    """对话里说"做风险测评"必须给出可执行的下一步，而不是生成空答案计划后失败。"""
    headers = _login(client)
    created = client.post(
        "/v1/tasks",
        json={"message": "我要为子女教育金做风险测评，问卷至少覆盖投资经验、损失承受能力、风险态度、期限、流动性和资金用途。"},
        headers=headers,
    ).json()
    assert created["status"] == "ok"
    data = created["data"]
    assert data["status"] == "blocked", data["status"]
    assert data["plan"]["feasible"] is False
    assert "ASSESSMENT_REQUIRES_QUESTIONNAIRE" in data["plan"]["infeasible_reasons"]
    alternatives = {item["action"] for item in data["plan"]["alternatives"]}
    assert "submit_risk_questionnaire" in alternatives
    # 不得创建任何 operation，更不得留下 dispatching 的孤儿操作
    assert data.get("operations") in ([], None) or data["operations"] == []

def _ensure_product(session) -> None:
    from finpilot import models

    if session.get(models.WealthProduct, "product_huoli") is not None:
        return
    session.add(
        models.WealthProduct(
            product_id="product_huoli",
            name="活期宝",
            issuer="模拟银行理财",
            product_type="fixed_income",
            terms_version="2026.10",
            issuer_risk_level="R1",
            min_minor=100,
            fee_bps=0,
            fee_fixed_minor=0,
            term_days=0,
            open_days=[0, 1, 2, 3, 4],
            cutoff_time="15:00",
            liquidity_terms="随时赎回，T+0 到账",
            cancel_terms="不可撤单",
            cooling_off_days=0,
            settlement_days=0,
            history_ref="",
            meta={},
            is_active=True,
        )
    )
    session.commit()


def test_purchase_task_resolves_product_name(client, seed, session):
    """申购请求必须把完整产品名解析成 product_id，而不是让计划在规划期报 PRODUCT_NOT_FOUND。"""
    _ensure_product(session)
    headers = _login(client)
    questionnaire = client.get("/v1/wealth/questionnaire", headers=headers).json()["data"]
    answers = _mid_answers(questionnaire["questions"])
    submitted = client.post("/v1/wealth/assessment", json={"answers": answers}, headers=headers).json()
    assert submitted["status"] == "ok"

    created = client.post(
        "/v1/tasks",
        json={"message": "用储蓄卡申购活期宝100元"},
        headers=headers,
    ).json()["data"]
    plan = created["plan"]
    assert plan["feasible"] is True, plan.get("infeasible_reasons")
    assert created["status"] == "awaiting_authorization"
    steps = plan["scope"]["steps"]
    assert [step["tool"] for step in steps] == ["wealth.purchase"]
    assert steps[0]["product_id"] == "product_huoli"
    assert steps[0]["amount_minor"] == 10000
    assert plan["required_level"] == "RED"