"""卡片领域测试（S4-01/02/04；验收族 T12/T13/T17/T18）。

覆盖：申请不生成卡、批准才生成卡、拒绝与撤回、提额申请与批准分离、被拒申请不可翻案、
旧版本冲突、用户锁与风控锁叠加、挂失不清风控锁、补卡新旧关系与旧卡关闭、脱敏卡号、幂等，
以及领域对象与 ``contracts/schemas/{card,card_application,card_control}.json`` 的一致性。

全部跑真实 PostgreSQL（``TEST_DATABASE_URL``）；金额均为整数分。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlalchemy import func, select

from finpilot.domains import cards
from finpilot.errors import FinPilotError
from finpilot.ids import idempotency_request_hash, new_id, parameters_hash
from finpilot.kernel.grants import activate_grant, create_exact_grant
from finpilot.kernel.identity import AuthContext
from finpilot.kernel.submission import OperationDraft, prepare_operation, submit_operation
from finpilot.models import Card, CardApplication, CardControl, Operation
from finpilot.security import policy
from finpilot.timeutil import utcnow

CUSTOMER = "user_demo_01"
OTHER_CUSTOMER = "user_demo_02"
OPERATOR = "user_operator_01"

MASKED_PAN_RE = re.compile(r"^\*{4} \*{4} \*{4} \d{4}$")
SCHEMA_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schemas"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _ctx(user_id: str = CUSTOMER, roles: tuple[str, ...] = ("customer",), channel: str = "app") -> AuthContext:
    return AuthContext(user_id=user_id, roles=roles, channel=channel, session_id=f"sess_{user_id}_{channel}")


def _schema_registry() -> Registry:
    resources = []
    for path in SCHEMA_DIR.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in schema:
            resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def assert_contract(instance: dict, schema_name: str) -> None:
    """领域对象必须与 contracts/schemas 的合同一致（additionalProperties=false）。"""
    if not SCHEMA_DIR.exists():  # pragma: no cover - 契约目录缺失时明确跳过
        pytest.skip("contracts/schemas 缺失")
    schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, registry=_schema_registry())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def assert_application_contract(data: dict) -> None:
    """card_application 合同校验。

    契约已与实现对齐：contracts/schemas/card_application.json 的 kind 枚举
    同时包含 limit_increase 与 limit_change（后者为策略层"被拒提额不可翻案"使用的值）。
    """
    assert_contract(data, "card_application.json")


def _submit(session, ctx: AuthContext, tool: str, typed_args: dict, *, source_account_id: str | None = None) -> Operation:
    """走完整内核：prepare → （按等级）签授权 → submit。金额恒为 0（卡片领域不划款）。"""
    draft = OperationDraft(tool=tool, typed_args=typed_args, source_account_id=source_account_id)
    operation = prepare_operation(session, ctx, draft)
    grant_id = None
    if operation.required_level != "GREEN":
        scope = {
            "tool": tool,
            "amount_minor": 0,
            "account_id": source_account_id,
            "card_id": typed_args.get("card_id"),
        }
        if operation.required_level == "RED":
            grant = create_exact_grant(
                session,
                ctx,
                plan_id=None,
                plan_revision=1,
                scope=scope,
                required_level="RED",
                verification_level="TOTP",
            )
            activate_grant(session, ctx, grant_id=grant.grant_id, verification_level="TOTP")
        else:
            grant = create_exact_grant(
                session, ctx, plan_id=None, plan_revision=1, scope=scope, required_level="YELLOW"
            )
        grant_id = grant.grant_id
    submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant_id)
    return operation


def _run(session, ctx: AuthContext, tool: str, typed_args: dict, *, source_account_id: str | None = None) -> dict:
    operation = _submit(session, ctx, tool, typed_args, source_account_id=source_account_id)
    return cards.TOOL_HANDLERS[tool](session, ctx, operation)


def _fake_operation(session, *, owner_user_id: str, tool: str, typed_args: dict) -> Operation:
    """直接构造已提交的操作行：用于运营角色的处理器路径与绕过策略层的对抗用例。"""
    now = utcnow()
    operation = Operation(
        operation_id=new_id("op"),
        owner_user_id=owner_user_id,
        tool=tool,
        typed_args=typed_args,
        parameters_hash=parameters_hash(typed_args),
        idempotency_key=f"idem_{new_id('k')}",
        request_hash=idempotency_request_hash(tool, typed_args),
        status="dispatching",
        required_level="GREEN",
        amount_minor=0,
        fee_minor=0,
        currency="CNY",
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    session.flush()
    return operation


def _apply(session, ctx: AuthContext | None = None, *, limit_minor: int = 300_000, account_id: str = "account_salary") -> dict:
    return _run(session, ctx or _ctx(), "card.apply", {"account_id": account_id, "requested_limit_minor": limit_minor})


def _approved_card(session, operator_ctx: AuthContext, *, limit_minor: int = 300_000) -> tuple[dict, Card]:
    application = _apply(session, _ctx(), limit_minor=limit_minor)
    cards.start_review(session, operator_ctx, application["data"]["application_id"])
    result = cards.approve_application(
        session, operator_ctx, application["data"]["application_id"], True, limit_minor, "自动化测试批准"
    )
    card = session.get(Card, result["data"]["card_id"])
    assert card is not None
    return result["data"], card


def _add_control(session, ctx: AuthContext, card_id: str, **args) -> dict:
    payload = {"card_id": card_id, "action": "add", **args}
    if ctx.is_operator:
        return cards.TOOL_HANDLERS["card.set_control"](
            session, ctx, _fake_operation(session, owner_user_id=ctx.user_id, tool="card.set_control", typed_args=payload)
        )
    return _run(session, ctx, "card.set_control", payload)


def _release_control_via_handler(session, ctx: AuthContext, card_id: str, **args) -> dict:
    payload = {"card_id": card_id, "action": "release", **args}
    return cards.TOOL_HANDLERS["card.set_control"](
        session, ctx, _fake_operation(session, owner_user_id=ctx.user_id, tool="card.set_control", typed_args=payload)
    )


def _count(session, model) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _assert_error(code: str, callable_, *args, **kwargs) -> FinPilotError:
    with pytest.raises(FinPilotError) as excinfo:
        callable_(*args, **kwargs)
    assert excinfo.value.code == code, f"期望 {code}，实际 {excinfo.value.code}: {excinfo.value}"
    return excinfo.value


# ---------------------------------------------------------------------------
# 1 申请与卡生命周期分离
# ---------------------------------------------------------------------------


def test_apply_creates_application_but_no_card(session, seed):
    """申请成功只表示已提交：不得生成有效卡，也不写批准额度。"""
    result = _apply(session, limit_minor=300_000)

    assert result["source"] == "cards"
    assert result["version"] == "2026.10"
    assert result["type"] == "card_application"
    assert result["domain"] == "cards"
    assert result["result_type"] == "card_application"
    assert result["observed_at"]
    assert cards.SIMULATED_DECISION_LABEL in result["warnings"]
    assert result["produces_card"] is False
    assert result["simulated_decision_rule"]["label"] == "模拟银行决策"

    data = result["data"]
    assert data["application_status"] == "submitted"
    assert data["kind"] == "new_card"
    assert data["approved_limit_minor"] is None
    assert data["decided_by"] is None
    assert data["decided_at"] is None
    assert data["requested_limit_minor"] == 300_000
    assert_application_contract(data)

    assert _count(session, Card) == 0
    assert cards.list_cards(session, _ctx()) == []
    applications = cards.list_applications(session, _ctx())
    assert len(applications) == 1
    assert applications[0]["application_id"] == data["application_id"]
    assert_application_contract(applications[0])


def test_only_approval_creates_active_card(session, seed, operator_ctx):
    """approved 时才创建 Card(lifecycle_status='active')，并记录审批人与时间与理由。"""
    approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)

    assert approved["application_status"] == "approved"
    assert approved["approved_limit_minor"] == 300_000
    assert approved["decided_by"] == OPERATOR
    assert approved["decided_at"] is not None
    assert approved["decision_reason"] == "自动化测试批准"
    assert_application_contract(approved)

    assert _count(session, Card) == 1
    assert card.lifecycle_status == "active"
    assert card.credit_limit_minor == 300_000
    assert card.owner_user_id == CUSTOMER
    assert card.account_id == "account_salary"

    listed = cards.list_cards(session, _ctx())
    assert [c["card_id"] for c in listed] == [card.card_id]
    assert listed[0]["lifecycle_status"] == "active"
    assert listed[0]["blocked"] is False
    assert_contract({k: v for k, v in listed[0].items() if k not in ("blocked", "blocked_reasons")}, "card.json")

    status = cards.card_status(session, _ctx(), card.card_id)
    assert status["credit_limit_minor"] == 300_000
    assert status["blocked"] is False
    assert status["effective_controls"]["count"] == 0
    assert [a["application_status"] for a in status["applications"]] == ["approved"]


def test_customer_cannot_approve_and_agent_cannot_self_approve(session, seed, operator_ctx):
    """审批是独立运营角色动作：客户身份不能批准，也不能推进审核。"""
    application = _apply(session)
    application_id = application["data"]["application_id"]

    err = _assert_error("POLICY_DENIED", cards.approve_application, session, _ctx(), application_id, True, 300_000, "自批")
    assert "operator_role_required" in err.details["reason_codes"]
    assert _count(session, Card) == 0

    err = _assert_error("POLICY_DENIED", cards.start_review, session, _ctx(), application_id)
    assert "operator_role_required" in err.details["reason_codes"]


def test_simulated_rule_is_public_and_drives_review(session, seed, operator_ctx):
    """模拟审批规则必须公开可读、标注"模拟银行决策"，并可推进 under_review。"""
    rule = cards.simulated_approval_rule()
    assert rule["label"] == "模拟银行决策"
    assert rule["max_approved_limit_minor"] == 500_000
    assert "模拟银行" in rule["disclosure"]
    assert rule["approve_conditions"] and rule["reject_conditions"]

    within = _apply(session, limit_minor=500_000)
    reviewed = cards.auto_review(session, operator_ctx, within["data"]["application_id"])
    assert reviewed["data"]["application_status"] == "approved"
    assert "模拟银行决策" in reviewed["data"]["decision_reason"]
    assert _count(session, Card) == 1

    over = _apply(session, limit_minor=500_001)
    rejected = cards.auto_review(session, operator_ctx, over["data"]["application_id"])
    assert rejected["data"]["application_status"] == "rejected"
    assert "requested_limit_over_simulated_cap" in rejected["data"]["decision_reason"]
    assert rejected["data"]["approved_limit_minor"] is None
    assert _count(session, Card) == 1  # 拒绝不生成卡


# ---------------------------------------------------------------------------
# 2 拒绝与撤回
# ---------------------------------------------------------------------------


def test_reject_and_withdraw_never_create_card(session, seed, operator_ctx):
    applied = _apply(session)
    rejected = cards.approve_application(
        session, operator_ctx, applied["data"]["application_id"], False, None, "收入证明不足"
    )
    assert rejected["data"]["application_status"] == "rejected"
    assert rejected["data"]["approved_limit_minor"] is None
    assert rejected["data"]["decided_by"] == OPERATOR
    assert rejected["data"]["decision_reason"] == "收入证明不足"
    assert_application_contract(rejected["data"])
    assert _count(session, Card) == 0

    second = _apply(session)
    withdrawn = cards.withdraw_application(session, _ctx(), second["data"]["application_id"])
    assert withdrawn["data"]["application_status"] == "withdrawn"
    assert withdrawn["data"]["decided_by"] == CUSTOMER
    assert_application_contract(withdrawn["data"])
    assert _count(session, Card) == 0

    # 终态申请不能再次决策，也不能再撤回
    _assert_error(
        "POLICY_DENIED", cards.approve_application, session, operator_ctx, second["data"]["application_id"], True, 300_000, ""
    )
    _assert_error("POLICY_DENIED", cards.withdraw_application, session, _ctx(), second["data"]["application_id"])

    stored = session.get(CardApplication, second["data"]["application_id"])
    assert stored.application_status == "withdrawn"
    assert stored.approved_limit_minor is None


def test_withdraw_requires_owner_and_open_state(session, seed, operator_ctx):
    applied = _apply(session)
    application_id = applied["data"]["application_id"]

    _assert_error("OBJECT_NOT_ACCESSIBLE", cards.withdraw_application, session, _ctx(OTHER_CUSTOMER), application_id)

    cards.start_review(session, operator_ctx, application_id)
    assert cards.list_applications(session, _ctx(), status="under_review")[0]["application_id"] == application_id
    # under_review 仍可撤回
    cards.withdraw_application(session, _ctx(), application_id)

    approved, _card = _approved_card(session, operator_ctx)
    _assert_error("POLICY_DENIED", cards.withdraw_application, session, _ctx(), approved["application_id"])


# ---------------------------------------------------------------------------
# 3 额度调整：申请与批准分离
# ---------------------------------------------------------------------------


def test_limit_change_is_application_only(session, seed, operator_ctx):
    """客户侧只产生 limit_change 申请，绝不直接改 Card.credit_limit_minor。"""
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    assert card.credit_limit_minor == 300_000
    card_version = card.version

    result = _run(
        session,
        _ctx(),
        "card.set_limit",
        {
            "card_id": card.card_id,
            "direction": "increase",
            "requested_limit_minor": 450_000,
            "expected_card_version": card_version,
        },
    )
    data = result["data"]
    assert data["kind"] == "limit_change"
    assert data["requested_limit_minor"] == 450_000
    assert data["approved_limit_minor"] is None
    assert data["application_status"] == "submitted"
    assert result["card_limit_unchanged"] is True
    assert result["current_credit_limit_minor"] == 300_000
    assert result["direction"] == "increase"
    assert_application_contract(data)

    session.expire_all()
    card = session.get(Card, card.card_id)
    assert card.credit_limit_minor == 300_000, "客户申请不得直接改额度"

    approved = cards.approve_application(session, operator_ctx, data["application_id"], True, 450_000, "符合提额规则")
    assert approved["data"]["approved_limit_minor"] == 450_000
    assert_application_contract(approved["data"])
    session.expire_all()
    assert session.get(Card, card.card_id).credit_limit_minor == 450_000

    # 运营审批只履行原客户请求：不得超过申请额度
    other = _approved_card(session, operator_ctx, limit_minor=200_000)
    pending = _run(
        session,
        _ctx(),
        "card.set_limit",
        {"card_id": other[1].card_id, "direction": "increase", "requested_limit_minor": 260_000},
    )
    err = _assert_error(
        "POLICY_DENIED", cards.approve_application, session, operator_ctx, pending["data"]["application_id"], True, 300_000, "擅提"
    )
    assert "exceeds_client_request" in err.details["reason_codes"]


def test_limit_levels_come_from_policy(session, seed, operator_ctx):
    """等级由 policy 决定（提额 RED、降额 YELLOW），领域只做事后校验。"""
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)

    increase_decision = policy.evaluate_policy(
        session,
        _ctx(),
        tool="card.set_limit",
        typed_args={"card_id": card.card_id, "direction": "increase", "requested_limit_minor": 400_000},
    )
    assert increase_decision.required_level == "RED"

    decrease_decision = policy.evaluate_policy(
        session,
        _ctx(),
        tool="card.set_limit",
        typed_args={"card_id": card.card_id, "direction": "decrease", "requested_limit_minor": 100_000},
    )
    assert decrease_decision.required_level == "YELLOW"

    # 提额未经强验证时，内核拒绝提交（RED 需要 TOTP）
    operation = prepare_operation(
        session,
        _ctx(),
        OperationDraft(
            tool="card.set_limit",
            typed_args={"card_id": card.card_id, "direction": "increase", "requested_limit_minor": 400_000},
        ),
    )
    assert operation.required_level == "RED"
    err = _assert_error("GRANT_REQUIRED", submit_operation, session, _ctx(), operation_id=operation.operation_id)
    assert "grant_missing" in err.details["reason_codes"]


def test_limit_decrease_direction_and_validation(session, seed, operator_ctx):
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)

    decrease = _run(
        session, _ctx(), "card.set_limit", {"card_id": card.card_id, "direction": "decrease", "requested_limit_minor": 100_000}
    )
    assert decrease["direction"] == "decrease"
    assert decrease["data"]["application_status"] == "submitted"
    assert session.get(Card, card.card_id).credit_limit_minor == 300_000
    cards.approve_application(session, operator_ctx, decrease["data"]["application_id"], True, 100_000, "客户降额")
    session.expire_all()
    assert session.get(Card, card.card_id).credit_limit_minor == 100_000

    _assert_error(
        "PARAM_INVALID",
        _run,
        session,
        _ctx(),
        "card.set_limit",
        {"card_id": card.card_id, "direction": "decrease", "requested_limit_minor": 900_000},
    )


def test_rejected_increase_cannot_be_revived(session, seed, operator_ctx):
    """被拒的提额申请不能通过重新发问变为批准：参数未变更的重复申请返回 previously_rejected。"""
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    first = _run(
        session, _ctx(), "card.set_limit", {"card_id": card.card_id, "direction": "increase", "requested_limit_minor": 400_000}
    )
    cards.approve_application(session, operator_ctx, first["data"]["application_id"], False, None, "还款记录不足")

    # (a) 走完整内核：策略层先拒（previously_rejected_application）
    typed_args = {"card_id": card.card_id, "direction": "increase", "requested_limit_minor": 400_000}
    err = _assert_error("POLICY_DENIED", _submit, session, _ctx(), "card.set_limit", typed_args)
    assert "previously_rejected_application" in err.details["reason_codes"]
    decision = policy.evaluate_policy(session, _ctx(), tool="card.set_limit", typed_args=typed_args)
    assert decision.allow is False
    assert decision.reason_codes == ["previously_rejected_application"]

    # (b) 绕过策略层的处理器路径：领域层再次拒绝，原因码 previously_rejected
    operation = _fake_operation(session, owner_user_id=CUSTOMER, tool="card.set_limit", typed_args=typed_args)
    err = _assert_error("POLICY_DENIED", cards.request_limit_change, session, _ctx(), operation)
    assert err.details["reason_codes"] == ["previously_rejected"]
    assert err.details["rejected_application_id"] == first["data"]["application_id"]

    # 参数变更后的新申请不受"未变更参数"限制（是否批准由运营决定）
    changed = cards.request_limit_change(
        session,
        _ctx(),
        _fake_operation(
            session,
            owner_user_id=CUSTOMER,
            tool="card.set_limit",
            typed_args={"card_id": card.card_id, "direction": "increase", "requested_limit_minor": 420_000},
        ),
    )
    assert changed["data"]["application_status"] == "submitted"
    assert changed["data"]["requested_limit_minor"] == 420_000


def test_stale_card_version_is_rejected(session, seed, operator_ctx):
    """expected_card_version 不匹配 → VERSION_CONFLICT，且不产生申请。"""
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    stale = card.version + 5
    err = _assert_error(
        "VERSION_CONFLICT",
        _run,
        session,
        _ctx(),
        "card.set_limit",
        {
            "card_id": card.card_id,
            "direction": "increase",
            "requested_limit_minor": 400_000,
            "expected_card_version": stale,
        },
    )
    assert err.details["reason_codes"] == ["version_mismatch"]
    assert err.details["current_version"] == card.version
    assert [a["kind"] for a in cards.list_applications(session, _ctx())] == ["new_card"]


# ---------------------------------------------------------------------------
# 4 叠加卡控
# ---------------------------------------------------------------------------


def test_user_lock_and_risk_freeze_stack_and_customer_cannot_release_freeze(session, seed, operator_ctx):
    """同一卡同时存在用户锁与风控锁；客户可解用户锁，但解不掉风控锁。"""
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)

    _add_control(session, _ctx(), card.card_id, control_type="user_lock", reason="疑似卡片丢失")
    _add_control(session, operator_ctx, card.card_id, control_type="risk_freeze", reason="风控模型命中")

    controls = session.execute(select(CardControl).where(CardControl.card_id == card.card_id)).scalars().all()
    assert len(controls) == 2
    assert all(c.active for c in controls)
    freeze = next(c for c in controls if c.control_type == "risk_freeze")
    assert freeze.created_by_role == "risk"
    assert next(c for c in controls if c.control_type == "user_lock").created_by_role == "customer"

    status = cards.card_status(session, _ctx(), card.card_id)
    view = status["effective_controls"]
    assert view["count"] == 2
    assert view["user_locked"] is True
    assert view["risk_frozen"] is True
    assert sorted(view["control_types"]) == ["risk_freeze", "user_lock"]
    assert len(status["controls"]) == 2
    assert_contract({k: v for k, v in status.items() if k not in ("blocked", "blocked_reasons", "applications")}, "card.json")
    for control in status["controls"]:
        assert_contract(control, "card_control.json")

    # 领域视图与 policy.card_control_block_reason 结果一致（多重集）
    policy_reasons = policy.card_control_block_reason(session, card)
    assert sorted(cards.control_block_reasons(view, card_status=card.lifecycle_status)) == sorted(policy_reasons)
    assert sorted(policy_reasons) == ["risk_freeze", "user_lock"]
    assert sorted(status["blocked_reasons"]) == ["risk_freeze", "user_lock"]

    # 客户解除用户锁成功，风控锁不受影响
    released = _release_control_via_handler(session, _ctx(), card.card_id, control_type="user_lock")
    assert released["data"]["active"] is False
    assert released["data"]["released_by"] == CUSTOMER
    assert released["data"]["released_at"] is not None
    assert_contract(released["data"], "card_control.json")
    assert released["effective_controls"]["risk_frozen"] is True
    assert released["effective_controls"]["user_locked"] is False

    # 客户解除风控锁：策略层拒绝（OPERATOR 级），处理器层再次拒绝
    err = _assert_error(
        "POLICY_DENIED",
        _submit,
        session,
        _ctx(),
        "card.set_control",
        {"card_id": card.card_id, "action": "release", "control_type": "risk_freeze"},
    )
    assert "operator_only" in err.details["reason_codes"]
    err = _assert_error(
        "POLICY_DENIED",
        _release_control_via_handler,
        session,
        _ctx(),
        card.card_id,
        control_type="risk_freeze",
    )
    assert err.details["reason_codes"] == ["control_release_not_allowed"]

    # 运营角色可以解除风控锁；已释放记录保留供审计（叠加控制互不清除）
    released_freeze = _release_control_via_handler(session, operator_ctx, card.card_id, control_type="risk_freeze")
    assert released_freeze["data"]["active"] is False
    assert released_freeze["effective_controls"]["count"] == 0
    assert _count(session, CardControl) == 2
    assert cards.card_status(session, _ctx(), card.card_id)["blocked"] is False


def test_channel_lock_and_limit_are_additive(session, seed, operator_ctx):
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    _add_control(session, _ctx(), card.card_id, control_type="channel_lock", channel="online")
    _add_control(session, _ctx(), card.card_id, control_type="limit", limit_minor=50_000)

    status = cards.card_status(session, _ctx(), card.card_id)
    view = status["effective_controls"]
    assert view["channel_locks"] == ["online"]
    assert view["limits_minor"] == [50_000]
    assert view["count"] == 2

    # 渠道与金额维度的原因码与策略层一致
    policy_reasons = policy.card_control_block_reason(session, card, channel="online", amount_minor=60_000)
    mine = cards.control_block_reasons(
        view, card_status=card.lifecycle_status, channel="online", amount_minor=60_000
    )
    assert sorted(mine) == sorted(policy_reasons) == ["channel_lock", "over_limit"]
    policy_offline = policy.card_control_block_reason(session, card, channel="offline", amount_minor=10_000)
    assert policy_offline == []
    assert cards.control_block_reasons(
        view, card_status=card.lifecycle_status, channel="offline", amount_minor=10_000
    ) == []

    # 契约别名（user_limit）归一为 policy 识别的 limit，且缺少 channel 的渠道锁被拒
    aliased = _add_control(session, _ctx(), card.card_id, control_type="user_limit", limit_minor=20_000)
    assert aliased["data"]["control_type"] == "limit"
    _assert_error(
        "PARAM_INVALID",
        _run,
        session,
        _ctx(),
        "card.set_control",
        {"card_id": card.card_id, "control_type": "channel_lock"},
    )
    _assert_error(
        "PARAM_INVALID",
        _run,
        session,
        _ctx(),
        "card.set_control",
        {"card_id": card.card_id, "control_type": "channel_lock", "channel": "branch"},
    )


# ---------------------------------------------------------------------------
# 5 挂失 / 解挂
# ---------------------------------------------------------------------------


def test_report_lost_keeps_risk_freeze_and_unfreeze_restores(session, seed, operator_ctx):
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    _add_control(session, operator_ctx, card.card_id, control_type="risk_freeze", reason="风控模型命中")

    lost = _run(session, _ctx(), "card.report_lost", {"card_id": card.card_id, "action": "lost"})
    assert lost["data"]["lifecycle_status"] == "lost"
    assert lost["action"] == "lost"
    assert lost["risk_freeze_preserved"] is True
    assert [c["control_type"] for c in lost["controls_preserved"]] == ["risk_freeze"]
    assert sorted(lost["blocked_reasons"]) == ["card_lost", "risk_freeze"]
    assert_contract(lost["data"], "card.json")

    restored = _run(session, _ctx(), "card.report_lost", {"card_id": card.card_id, "action": "unfreeze"})
    assert restored["data"]["lifecycle_status"] == "active"
    assert restored["risk_freeze_preserved"] is True
    assert sorted(restored["blocked_reasons"]) == ["risk_freeze"]
    assert _count(session, CardControl) == 1

    # 挂失／解挂需要 RED（强验证因子），客户不能凭 YELLOW 完成
    typed_args = {"card_id": card.card_id, "action": "lost"}
    operation = prepare_operation(session, _ctx(), OperationDraft(tool="card.report_lost", typed_args=typed_args))
    assert operation.required_level == "RED"
    _assert_error("GRANT_REQUIRED", submit_operation, session, _ctx(), operation_id=operation.operation_id)


def test_red_card_operation_requires_real_strong_verification(session, seed, clock, operator_ctx):
    """挂失是 RED：只有真实 TOTP 验证签发的授权能提交，且授权绑定参数（T17）。"""
    from finpilot.kernel.identity import totp_at
    from finpilot.kernel.verifier import issue_challenge, verify_challenge

    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    typed_args = {"card_id": card.card_id, "action": "lost"}
    operation = prepare_operation(session, _ctx(), OperationDraft(tool="card.report_lost", typed_args=typed_args))
    assert operation.required_level == "RED"
    assert operation.status == "awaiting_authorization"

    scope = {
        "kind": "exact",
        "tool": "card.report_lost",
        "amount_minor": 0,
        "card_id": card.card_id,
        "operation_id": operation.operation_id,
        "parameters_hash": operation.parameters_hash,
        "required_level": "RED",
    }
    challenge = issue_challenge(session, _ctx(), scope=scope, plan_id=None, plan_revision=1)

    # 错误验证码不签发授权
    good_code, _counter = totp_at(seed["totp_secret"], clock.now())
    wrong_code = f"{(int(good_code) + 1) % 1_000_000:06d}"
    _assert_error(
        "CHALLENGE_FAILED", verify_challenge, session, _ctx(), challenge_id=challenge.challenge_id, factor_response=wrong_code
    )
    session.expire_all()
    assert session.get(Operation, operation.operation_id).status == "awaiting_authorization"

    # 真实 TOTP 通过后签发 TOTP 级授权
    clock.advance(31)  # 换一个时间步，避免与刚用掉的计数器重放
    code, _counter = totp_at(seed["totp_secret"], clock.now())
    outcome = verify_challenge(session, _ctx(), challenge_id=challenge.challenge_id, factor_response=code)
    grant_id = outcome["grant"]["grant_id"]
    assert outcome["grant"]["verification_level"] == "TOTP"

    # 精确授权绑定参数：换参数的操作不能用同一授权
    other = prepare_operation(
        session,
        _ctx(),
        OperationDraft(tool="card.report_lost", typed_args={**typed_args, "reason": "换参数的重复请求"}),
    )
    err = _assert_error(
        "GRANT_SCOPE_VIOLATION", submit_operation, session, _ctx(), operation_id=other.operation_id, grant_id=grant_id
    )
    assert "parameter_hash_mismatch" in err.details["reason_codes"]

    submit_operation(session, _ctx(), operation_id=operation.operation_id, grant_id=grant_id)
    lost = cards.TOOL_HANDLERS["card.report_lost"](session, _ctx(), operation)
    assert lost["data"]["lifecycle_status"] == "lost"
    session.expire_all()
    assert session.get(Card, card.card_id).lifecycle_status == "lost"


def test_unfreeze_closed_card_is_rejected(session, seed, operator_ctx):
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    card.lifecycle_status = "closed"
    card.version += 1
    session.flush()
    err = _assert_error(
        "POLICY_DENIED", _run, session, _ctx(), "card.report_lost", {"card_id": card.card_id, "action": "unfreeze"}
    )
    assert "illegal_transition" in err.details["reason_codes"]


# ---------------------------------------------------------------------------
# 6 补卡
# ---------------------------------------------------------------------------


def test_replacement_links_cards_and_closes_old(session, seed, operator_ctx):
    _approved, old_card = _approved_card(session, operator_ctx, limit_minor=300_000)

    application = _run(session, _ctx(), "card.replace", {"card_id": old_card.card_id})
    data = application["data"]
    assert data["kind"] == "replacement"
    assert data["application_status"] == "submitted"
    assert data["card_id"] == old_card.card_id
    assert data["requested_limit_minor"] == 300_000
    assert_application_contract(data)
    assert application["replaces_card"]["card_id"] == old_card.card_id
    assert _count(session, Card) == 1, "批准前不得创建新卡"

    # 在途补卡申请不能被重复提交
    _assert_error("POLICY_DENIED", _run, session, _ctx(), "card.replace", {"card_id": old_card.card_id})

    approved = cards.approve_application(session, operator_ctx, data["application_id"], True, None, "补卡批准")
    new_card_id = approved["created_card"]["card_id"]
    assert new_card_id != old_card.card_id
    assert_contract(approved["created_card"], "card.json")

    session.expire_all()
    old_after = session.get(Card, old_card.card_id)
    new_after = session.get(Card, new_card_id)
    assert new_after.lifecycle_status == "active"
    assert new_after.replaces_card_id == old_card.card_id
    assert old_after.replaced_by_card_id == new_card_id
    assert old_after.lifecycle_status == "closed"
    assert not (old_after.lifecycle_status == "active" and new_after.lifecycle_status == "active")
    assert new_after.credit_limit_minor == 300_000
    assert new_after.account_id == old_after.account_id
    assert new_after.masked_pan != old_after.masked_pan

    # 已补卡的旧卡不能再补卡
    _assert_error("POLICY_DENIED", _run, session, _ctx(), "card.replace", {"card_id": old_card.card_id})
    # 新卡额度沿用旧卡；后续提额必须走客户自己的额度申请，并由运营审批
    limit_change = _run(
        session, _ctx(), "card.set_limit", {"card_id": new_card_id, "direction": "increase", "requested_limit_minor": 350_000}
    )
    assert limit_change["data"]["kind"] == "limit_change"
    cards.approve_application(session, operator_ctx, limit_change["data"]["application_id"], True, 350_000, "新卡单独提额")
    session.expire_all()
    assert session.get(Card, new_card_id).credit_limit_minor == 350_000


def test_replacement_limit_cannot_change_without_separate_application(session, seed, operator_ctx):
    _approved, old_card = _approved_card(session, operator_ctx, limit_minor=300_000)
    application = _run(session, _ctx(), "card.replace", {"card_id": old_card.card_id})
    err = _assert_error(
        "POLICY_DENIED",
        cards.approve_application,
        session,
        operator_ctx,
        application["data"]["application_id"],
        True,
        500_000,
        "顺带提额",
    )
    assert "replacement_limit_change_requires_separate_application" in err.details["reason_codes"]


# ---------------------------------------------------------------------------
# 7 脱敏卡号与敏感字段
# ---------------------------------------------------------------------------


def test_masked_pan_only_and_sensitive_fields_rejected(session, seed, operator_ctx):
    _approved, card = _approved_card(session, operator_ctx, limit_minor=200_000)
    status = cards.card_status(session, _ctx(), card.card_id)
    assert MASKED_PAN_RE.match(status["masked_pan"]), status["masked_pan"]
    assert len(status["masked_pan"]) <= 24
    assert "cvv" not in Card.__table__.columns
    assert "pan" not in Card.__table__.columns
    # 返回里没有完整卡号或安全码字段
    assert "card_number" not in status
    assert not any(k in status for k in ("cvv", "cvv2", "security_code", "pan", "full_pan", "expiry", "pin"))
    assert "full_pan_stored" not in status

    err = _assert_error(
        "PARAM_INVALID",
        _run,
        session,
        _ctx(),
        "card.apply",
        {"account_id": "account_salary", "requested_limit_minor": 100_000, "cvv": "123"},
    )
    assert "sensitive_field_rejected" in err.details["reason_codes"]
    assert _count(session, CardApplication) == 1


# ---------------------------------------------------------------------------
# 8 归属与幂等
# ---------------------------------------------------------------------------


def test_ownership_is_enforced(session, seed, operator_ctx):
    _approved, card = _approved_card(session, operator_ctx, limit_minor=300_000)
    other = _ctx(OTHER_CUSTOMER)

    _assert_error("OBJECT_NOT_ACCESSIBLE", cards.card_status, session, other, card.card_id)
    _assert_error(
        "OBJECT_NOT_ACCESSIBLE",
        cards.request_limit_change,
        session,
        other,
        _fake_operation(
            session,
            owner_user_id=OTHER_CUSTOMER,
            tool="card.set_limit",
            typed_args={"card_id": card.card_id, "direction": "increase", "requested_limit_minor": 400_000},
        ),
    )
    assert cards.list_cards(session, other) == []
    assert cards.list_applications(session, other) == []
    # 运营角色可跨客户查看
    assert len(cards.list_cards(session, operator_ctx)) == 1


def test_handlers_are_idempotent_per_operation_id(session, seed):
    """同一 operation_id 重复执行不得产生第二条业务记录。"""
    typed_args = {"account_id": "account_salary", "requested_limit_minor": 250_000}
    operation = _submit(session, _ctx(), "card.apply", typed_args)

    first = cards.TOOL_HANDLERS["card.apply"](session, _ctx(), operation)
    second = cards.TOOL_HANDLERS["card.apply"](session, _ctx(), operation)
    assert first == second
    assert _count(session, CardApplication) == 1

    # 清掉操作结果缓存后，仍按 operation_id 命中既有申请（持久层幂等）
    operation.domain_result = {}
    session.flush()
    third = cards.TOOL_HANDLERS["card.apply"](session, _ctx(), operation)
    assert third["data"]["application_id"] == first["data"]["application_id"]
    assert _count(session, CardApplication) == 1

    session.expire_all()
    assert session.get(Operation, operation.operation_id).status == "succeeded"


def test_executor_registration_points(session, seed):
    """执行器／调度器按模块名收集注册点：TOOL_HANDLERS 覆盖全部卡片写工具。"""
    from finpilot.domains.cards import service as cards_service
    from finpilot.orchestration.executor import tool_handlers
    from finpilot.workers.scheduler import job_handlers

    assert set(cards.TOOL_HANDLERS) == {
        "card.apply",
        "card.set_limit",
        "card.set_control",
        "card.report_lost",
        "card.replace",
    }
    assert cards_service.JOB_HANDLERS == {}
    # 领域状态表与 models 常量逐字一致
    from finpilot.models import CARD_APPLICATION_STATUS, CARD_LIFECYCLE

    assert set(cards.APPLICATION_TRANSITIONS) == set(CARD_APPLICATION_STATUS)
    assert set(cards_service.CARD_LIFECYCLE_TRANSITIONS) == set(CARD_LIFECYCLE)
    discovered = tool_handlers()
    for tool in cards.TOOL_HANDLERS:
        assert tool in policy.TOOL_POLICY
        assert discovered.get(tool) is cards.TOOL_HANDLERS[tool]
    assert "card.apply" not in job_handlers()
    assert json.dumps(cards.simulated_approval_rule(), ensure_ascii=False)


def test_operator_handoff_call_compatibility(session, seed, operator_ctx):
    """运营接管路径（orchestration/handoff.py）以关键字调用审批，签名必须兼容。"""
    application = _apply(session)
    outcome = cards.approve_application(
        session,
        operator_ctx,
        application["data"]["application_id"],
        approve=True,
        approved_limit_minor=None,
        reason="模拟银行运营审批",
    )
    assert outcome["data"]["application_status"] == "approved"
    assert outcome["data"]["approved_limit_minor"] == 300_000
    assert outcome["data"]["decided_by"] == OPERATOR


def test_jsonschema_helper_sanity():
    """合同校验器本身可用（防止契约测试静默通过）。"""
    with pytest.raises(AssertionError):
        assert_contract({"card_id": "c1"}, "card.json")
    validator = Draft202012Validator({"type": "object"})
    assert list(validator.iter_errors({})) == []
    assert jsonschema.__name__ == "jsonschema"