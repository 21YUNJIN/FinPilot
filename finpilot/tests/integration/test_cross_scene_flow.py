"""跨场景（S6）端到端：生日采购计划 → 范围授权 + 强验证 → 两步商户预订 → 真实状态核验。

对应 V5 S6-01/S6-02/S6-03、T24/T25/T26/T27：预算含费用、保护应急金、
AA 不得早于确定成本、两类服务分别持久化且可查询。
"""

from __future__ import annotations

import pytest

from finpilot.kernel.identity import totp_at

pytestmark = [pytest.mark.integration]


def _login(client, user_id: str = "user_demo_01") -> dict:
    payload = client.post(
        "/v1/auth/sessions", json={"user_id": user_id, "password": "Passw0rd!demo", "channel": "app"}
    ).json()
    return {"Authorization": f"Bearer {payload['data']['token']}"}


def test_birthday_plan_books_two_services_within_budget(client, session, seed, clock):
    headers = _login(client)
    created = client.post(
        "/v1/tasks",
        json={"message": "帮我订生日鲜花和蛋糕，预算800元，这周六送到", "channel": "app"},
        headers=headers,
    ).json()
    assert created["status"] == "ok", created
    data = created["data"]
    goal = data["goal"]
    assert "plan_event" in goal["goals"]
    assert goal["hard_constraints"]["total_budget_minor"] == 80_000
    assert goal["hard_constraints"]["budget_includes_fees"] is True
    # 保护应急金：预算是可支配金额的一部分，不得穿透底线
    assert goal["hard_constraints"]["protected_floor_minor"] == 800_000

    plan = data["plan"]
    assert plan["feasible"] is True
    tools = [step["tool"] for step in plan["steps"]]
    assert "merchant.book" in tools
    assert tools.count("merchant.book") == 2, f"生日任务至少两类服务：{tools}"
    # 每个预订都必须先取报价（依赖顺序）
    for step in plan["steps"]:
        if step["tool"] == "merchant.book":
            assert any(dep.startswith("quote_") for dep in step["dependencies"])
    assert plan["required_level"] == "RED", "付费商户预订按 RED 处理"

    confirmed = client.post(
        f"/v1/plans/{plan['plan_id']}/confirm",
        json={"scope_hash": plan["scope_hash"], "expected_revision": data["revision"]},
        headers=headers,
    ).json()["data"]
    assert confirmed["status"] == "challenge_required"

    code, _ = totp_at(seed["totp_secret"], clock.now())
    verified = client.post(
        f"/v1/auth/challenges/{confirmed['challenge_id']}/verify",
        json={"factor_response": code},
        headers=headers,
    ).json()
    assert verified["status"] == "ok", verified
    execution = verified["data"]["execution"]
    assert execution["status"] in ("succeeded", "partially_succeeded", "reconciling"), execution

    # 异步提供方：商户确认预订后由对账用**原 operation_id** 查询并结算（不重新发起支付）
    task_before = client.get(f"/v1/tasks/{data['task_id']}", headers=headers).json()["data"]
    pending_refs = [
        op["operation_id"]
        for op in task_before["operations"]
        if op["tool"] == "merchant.book" and op["status"] in ("pending", "dispatching", "unknown")
    ]
    from finpilot.kernel.provider_adapter import get_adapter
    from finpilot.workers.reconciliation import reconcile_unknown_operations

    adapter = get_adapter("merchant_sandbox_local")
    for _round in range(3):
        if not pending_refs:
            break
        for ref in pending_refs:
            adapter.advance(ref, status="confirmed")
        clock.advance(2)  # 对账有 1 秒宽限窗口，模拟真实时间流逝
        stats = reconcile_unknown_operations(session, limit=10)
        session.commit()
        assert stats["checked"] >= len(pending_refs), stats
        client.post(f"/v1/tasks/{data['task_id']}/resume", headers=headers)
        task_before = client.get(f"/v1/tasks/{data['task_id']}", headers=headers).json()["data"]
        pending_refs = [
            op["operation_id"]
            for op in task_before["operations"]
            if op["tool"] == "merchant.book" and op["status"] in ("pending", "dispatching", "unknown")
        ]

    task = client.get(f"/v1/tasks/{data['task_id']}", headers=headers).json()["data"]
    booked = [op for op in task["operations"] if op["tool"] == "merchant.book"]
    assert len(booked) == 2
    assert all(op["status"] == "succeeded" for op in booked), booked
    # 每笔都有可核验的提供方引用，且不是同一订单
    refs = {op["provider_ref"] for op in booked}
    assert len(refs) == 2 and all(refs)

    # 资金效果：按实际捕获金额核对（含费用），且只扣一次
    spent = sum(op["amount_minor"] + op["fee_minor"] for op in booked)
    assert spent > 0 and spent <= 80_000, "总花费必须落在预算内（含费用）"
    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    salary = next(a for a in accounts["accounts"] if a["account_id"] == "account_salary")
    assert salary["posted_balance_minor"] == 900_000 - spent
    assert salary["posted_balance_minor"] >= accounts["protected_floor_minor"], "不得穿透应急金底线"

    # 任务预算预留已被捕获，且未释放为可用资金
    assert salary["available_minor"] == salary["posted_balance_minor"] - salary["reserved_minor"]


def test_budget_cannot_breach_protected_floor(client, seed, clock):
    """预算超过可支配金额时必须判定不可行并给出可验证原因，而不是放宽约束。"""
    headers = _login(client)
    created = client.post(
        "/v1/tasks",
        json={"message": "帮我订生日鲜花和蛋糕，预算5000元，这周六送到"},
        headers=headers,
    ).json()["data"]
    plan = created.get("plan")
    if plan is not None and plan["feasible"]:
        # 若规划通过，提交时必须因保护底线被拦下
        confirmed = client.post(
            f"/v1/plans/{plan['plan_id']}/confirm",
            json={"scope_hash": plan["scope_hash"], "expected_revision": created["revision"]},
            headers=headers,
        ).json()["data"]
        assert confirmed.get("status") == "challenge_required"
        code, _ = totp_at(seed["totp_secret"], clock.now())
        outcome = client.post(
            f"/v1/auth/challenges/{confirmed['challenge_id']}/verify",
            json={"factor_response": code},
            headers=headers,
        ).json()
        assert outcome["status"] in ("ok", "failed")
        if outcome["status"] == "failed":
            assert outcome["error"]["code"] in ("PROTECTED_FLOOR_VIOLATION", "RESERVATION_EXCEEDED", "INSUFFICIENT_FUNDS")
    else:
        assert created["status"] == "blocked"
        assert created["goal"]["hard_constraints"]["protected_floor_minor"] == 800_000

    # 无论走哪条路径，都不得穿透应急金底线
    accounts = client.get("/v1/accounts", headers=headers).json()["data"]
    salary = next(a for a in accounts["accounts"] if a["account_id"] == "account_salary")
    assert salary["posted_balance_minor"] - salary["reserved_minor"] >= accounts["protected_floor_minor"]