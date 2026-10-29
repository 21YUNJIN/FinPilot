"""AA 分摊领域测试（S1-04；验收族 T04/T27）。

覆盖：100 元三人 33.34/33.33/33.33 且合计相等、大额与 2 人／7 人的最大余数、发起人份额单列、
不产生任何资金变动、未收不可花、部分到账、退款调整可追溯、重复创建幂等，
以及领域对象与 ``contracts/schemas/aa_request.json`` / ``aa_share.json`` 的一致性。

全部跑真实 PostgreSQL；金额均为整数分，期望值全部手算给出。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlalchemy import func, select

from finpilot.domains import aa as aa_domain
from finpilot.domains.aa import service as aa_service
from finpilot.errors import FinPilotError
from finpilot.ids import idempotency_request_hash, new_id, parameters_hash
from finpilot.kernel.grants import create_exact_grant
from finpilot.kernel.identity import AuthContext
from finpilot.kernel.snapshots import get_snapshot
from finpilot.kernel.submission import OperationDraft, prepare_operation, submit_operation
from finpilot.models import (
    AARequest,
    AAShare,
    Account,
    LedgerEntry,
    LedgerTransaction,
    Operation,
    Outbox,
    Reservation,
)
from finpilot.timeutil import utcnow

INITIATOR = "user_demo_01"
PARTICIPANT = "user_demo_02"

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schemas"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _ctx(user_id: str = INITIATOR, roles: tuple[str, ...] = ("customer",)) -> AuthContext:
    return AuthContext(user_id=user_id, roles=roles, channel="app", session_id=f"sess_{user_id}")


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


def _participant(name: str, ref: str, user_id: str | None = None, weight: int | None = None) -> dict:
    item: dict = {"display_name": name, "participant_ref": ref}
    if user_id:
        item["participant_user_id"] = user_id
    if weight is not None:
        item["weight"] = weight
    return item


def _create(session, typed_args: dict, ctx: AuthContext | None = None, *, submit: bool = True) -> tuple[dict, Operation]:
    """走完整内核链路创建 AA（aa.create 为 YELLOW）。"""
    ctx = ctx or _ctx()
    draft = OperationDraft(tool="aa.create", typed_args=typed_args)
    operation = prepare_operation(session, ctx, draft)
    if submit:
        grant = create_exact_grant(
            session, ctx, plan_id=None, plan_revision=1, scope={"tool": "aa.create", "amount_minor": 0}, required_level="YELLOW"
        )
        submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    result = aa_domain.TOOL_HANDLERS["aa.create"](session, ctx, operation)
    return result, operation


def _fake_payment_operation(session, *, owner_user_id: str, amount_minor: int, status: str = "succeeded") -> Operation:
    """参与人自己的转账操作（权威操作记录；AA 只登记到账，不移动资金）。"""
    now = utcnow()
    typed_args = {"amount_minor": amount_minor, "payee_id": "payee_demo"}
    operation = Operation(
        operation_id=new_id("op"),
        owner_user_id=owner_user_id,
        tool="transfer.submit",
        typed_args=typed_args,
        parameters_hash=parameters_hash(typed_args),
        idempotency_key=f"idem_{new_id('k')}",
        request_hash=idempotency_request_hash("transfer.submit", typed_args),
        status=status,
        required_level="YELLOW",
        amount_minor=amount_minor,
        fee_minor=0,
        currency="CNY",
        created_at=now,
        updated_at=now,
    )
    session.add(operation)
    session.flush()
    return operation


def _balances(session) -> dict[str, int]:
    return {
        account_id: balance
        for account_id, balance in session.execute(
            select(Account.account_id, Account.posted_balance_minor).order_by(Account.account_id)
        ).all()
    }


def _ledger_counts(session) -> tuple[int, int]:
    txs = int(session.execute(select(func.count()).select_from(LedgerTransaction)).scalar_one())
    entries = int(session.execute(select(func.count()).select_from(LedgerEntry)).scalar_one())
    return txs, entries


def _shares_by_ref(state: dict) -> dict[str, dict]:
    return {s["participant_ref"]: s for s in state["participant_shares"]}


def _count(session, model) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _assert_error(code: str, callable_, *args, **kwargs) -> FinPilotError:
    with pytest.raises(FinPilotError) as excinfo:
        callable_(*args, **kwargs)
    assert excinfo.value.code == code, f"期望 {code}，实际 {excinfo.value.code}: {excinfo.value}"
    return excinfo.value


def _three_people_args(**overrides) -> dict:
    args = {
        "title": "周末聚餐",
        "final_cost_minor": 10_000,
        "participants": [_participant("张三", "p_zhangsan"), _participant("李四", "p_lisi")],
        "initiator_included": True,
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# 1 分摊精度
# ---------------------------------------------------------------------------


def test_100_yuan_three_people_split(session, seed):
    """100 元三人（含发起人）：33.34 / 33.33 / 33.33，合计严格等于 10000 分。"""
    result, _operation = _create(session, _three_people_args())

    assert result["type"] == "aa_request"
    assert result["source"] == "aa"
    assert result["version"] == "2026.10"
    assert result["domain"] == "aa"
    assert result["warnings"], "必须提示仅发收款请求、不代扣"
    assert any("不代扣" in w for w in result["warnings"])
    assert result["outstanding_minor"] == 3333 + 3333

    data = result["data"]
    assert_contract(data, "aa_request.json")
    shares = data["participant_shares"]
    assert [s["share_minor"] for s in shares] == [3334, 3333, 3333]
    assert [s["display_name"] for s in shares] == ["演示用户一", "张三", "李四"]
    assert sum(s["share_minor"] for s in shares) == data["final_cost_minor"] == 10_000
    assert data["currency"] == "CNY"
    assert data["status"] == aa_service.AA_OPEN
    assert data["received_minor"] == 0
    assert data["received_minor_is_spendable"] is False
    assert data["reconciled"] is False
    for share in shares:
        assert_contract(share, "aa_share.json")
        assert share["auto_debit_allowed"] is False

    initiator = shares[0]
    assert initiator["is_initiator"] is True
    assert initiator["status"] == aa_service.SHARE_PAID
    assert initiator["participant_user_id"] == INITIATOR
    assert result["initiator_share_minor"] == 3334
    for share in shares[1:]:
        assert share["is_initiator"] is False
        assert share["status"] == aa_service.SHARE_PENDING

    view = aa_domain.aa_view(session, _ctx(), data["aa_id"])
    assert view["initiator_included"] is True
    assert view["initiator_share_minor"] == 3334
    assert view["self_funded_minor"] == 3334
    assert view["collected_minor"] == 0
    assert view["outstanding_minor"] == 6666
    assert view["collect_only"] is True
    assert view["auto_debit"] is False


def test_large_amount_and_two_and_seven_people(session, seed):
    """大额与 2 人／7 人的最大余数分摊（手算期望）。"""
    # 100 元 2 人：10000 / 2 = 5000 整
    two, _ = _create(
        session,
        _three_people_args(title="两人打车", participants=[_participant("张三", "p_zhangsan")]),
    )
    assert [s["share_minor"] for s in two["data"]["participant_shares"]] == [5000, 5000]

    # 100 元 7 人：10000 = 7×1428 + 4 → 前四人各 +1：1429,1429,1429,1429,1428,1428,1428
    seven, _ = _create(
        session,
        _three_people_args(
            title="七人团建",
            participants=[_participant(f"队友{i}", f"p_{i}") for i in range(1, 7)],
        ),
    )
    seven_shares = [s["share_minor"] for s in seven["data"]["participant_shares"]]
    assert seven_shares == [1429, 1429, 1429, 1429, 1428, 1428, 1428]
    assert sum(seven_shares) == 10_000

    # 大额：100000.00 元三人 = 10000000 分 = 3×3333333 + 1 → 3333334,3333333,3333333
    large, _ = _create(session, _three_people_args(title="大额装修分摊", final_cost_minor=10_000_000))
    large_shares = [s["share_minor"] for s in large["data"]["participant_shares"]]
    assert large_shares == [3_333_334, 3_333_333, 3_333_333]
    assert sum(large_shares) == 10_000_000


def test_initiator_share_can_be_excluded(session, seed):
    """initiator_included=False：发起人不分摊，全部由参与人承担。"""
    result, _ = _create(
        session,
        {
            "title": "代付团购",
            "final_cost_minor": 10_000,
            "participants": [
                _participant("张三", "p_zhangsan"),
                _participant("李四", "p_lisi"),
                _participant("王五", "p_wangwu"),
            ],
            "initiator_included": False,
        },
    )
    data = result["data"]
    assert [s["share_minor"] for s in data["participant_shares"]] == [3334, 3333, 3333]
    assert all(s["is_initiator"] is False for s in data["participant_shares"])
    assert result["initiator_share_minor"] == 0
    assert result["outstanding_minor"] == 10_000

    view = aa_domain.aa_view(session, _ctx(), data["aa_id"])
    assert view["initiator_included"] is False
    assert view["initiator_share"] is None
    assert view["outstanding_minor"] == 10_000


def test_weights_split_conserves_total(session, seed):
    """带权重时仍走最大余数法：3000 分按 2:1 分 → 2000 / 1000。"""
    result, _ = _create(
        session,
        {
            "title": "按比例分摊",
            "final_cost_minor": 3_000,
            "participants": [_participant("张三", "p_zhangsan", weight=1)],
            "initiator_included": True,
            "initiator_weight": 2,
        },
    )
    shares = [s["share_minor"] for s in result["data"]["participant_shares"]]
    assert shares == [2000, 1000]
    assert sum(shares) == 3000
    assert_contract(result["data"], "aa_request.json")


def test_budget_amount_is_rejected(session, seed):
    """必须按已确认实际支出分摊，预算／估算口径直接拒绝。"""
    operation = prepare_operation(
        session,
        _ctx(),
        OperationDraft(
            tool="aa.create",
            typed_args={"title": "预算单", "budget_minor": 10_000, "participants": [_participant("张三", "p_z")]},
        ),
    )
    err = _assert_error("PARAM_INVALID", aa_domain.TOOL_HANDLERS["aa.create"], session, _ctx(), operation)
    assert "final_cost_required" in err.details["reason_codes"]
    assert err.details["budget_fields_present"] == ["budget_minor"]
    assert _count(session, AARequest) == 0


# ---------------------------------------------------------------------------
# 2 只发收款请求，不产生任何资金变动
# ---------------------------------------------------------------------------


def test_creation_moves_no_money(session, seed):
    before = _balances(session)
    assert _ledger_counts(session) == (0, 0)
    assert _count(session, Reservation) == 0
    assert _count(session, Outbox) == 0

    result, operation = _create(session, _three_people_args())

    assert _balances(session) == before
    assert _ledger_counts(session) == (0, 0)
    assert _count(session, Reservation) == 0
    assert _count(session, Outbox) == 0
    # 创建操作本身金额为 0：不含任何代扣或转账意图
    assert operation.amount_minor == 0
    assert operation.tool == "aa.create"
    assert result["data"]["received_minor"] == 0
    assert result["data"]["received_minor_is_spendable"] is False
    assert all(share["auto_debit_allowed"] is False for share in result["data"]["participant_shares"])


def test_pending_receivable_is_visible_but_not_spendable(session, seed):
    """未付款只标记应收：快照可见 pending_receivables_minor，但不计入 available_minor。"""
    _create(session, _three_people_args())

    snapshot = get_snapshot(session, _ctx())
    assert snapshot["pending_receivables_minor"] == 3333 + 3333  # 手算：3333 + 3333 = 6666
    assert snapshot["unconfirmed_receivables_are_spendable"] is False
    salary = next(a for a in snapshot["accounts"] if a["account_id"] == "account_salary")
    assert salary["posted_balance_minor"] == 900_000
    assert salary["reserved_minor"] == 0
    assert salary["available_minor"] == 900_000, "应收不得进入可用余额"
    assert salary["available_minor"] == salary["posted_balance_minor"]
    # 保护组只含 account_salary（种子应急金范围），可用金额不含应收
    assert snapshot["group"]["account_ids"] == ["account_salary"]
    assert snapshot["group"]["available_minor"] == 900_000
    assert snapshot["group"]["spendable_minor"] == 900_000 - 800_000


# ---------------------------------------------------------------------------
# 3 部分到账
# ---------------------------------------------------------------------------


def test_partial_then_full_collection(session, seed):
    result, _ = _create(
        session,
        _three_people_args(
            participants=[
                _participant("张三", "p_zhangsan", PARTICIPANT),
                _participant("李四", "p_lisi"),
            ]
        ),
    )
    aa_id = result["data"]["aa_id"]
    shares = _shares_by_ref(result["data"])

    # 身份：发起人不能拿自己的付款操作替参与人登记到账
    own = _fake_payment_operation(session, owner_user_id=INITIATOR, amount_minor=3333)
    _assert_error(
        "OBJECT_NOT_ACCESSIBLE",
        aa_domain.mark_share_paid,
        session,
        aa_id,
        shares["p_zhangsan"]["share_id"],
        operation_id=own.operation_id,
    )
    # 失败／取消的付款操作不能登记到账
    failed = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=3333, status="failed")
    err = _assert_error(
        "POLICY_DENIED",
        aa_domain.mark_share_paid,
        session,
        aa_id,
        shares["p_zhangsan"]["share_id"],
        operation_id=failed.operation_id,
    )
    assert err.details["reason_codes"] == ["payment_not_effective"]
    # 发起人自付份额不是应收
    err = _assert_error(
        "POLICY_DENIED",
        aa_domain.mark_share_paid,
        session,
        aa_id,
        shares[INITIATOR]["share_id"],
        operation_id=own.operation_id,
    )
    assert err.details["reason_codes"] == ["initiator_share_not_collectable"]

    payment = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=3333)
    after_first = aa_domain.mark_share_paid(session, aa_id, shares["p_zhangsan"]["share_id"], operation_id=payment.operation_id)
    assert after_first["collected_minor"] == 3333
    assert after_first["outstanding_minor"] == 3333
    assert after_first["status"] == aa_service.AA_PARTIALLY_SETTLED
    assert after_first["received_minor"] == 3333
    assert after_first["reconciled"] is False
    assert _shares_by_ref(after_first)["p_zhangsan"]["status"] == aa_service.SHARE_PAID
    assert _shares_by_ref(after_first)["p_lisi"]["status"] == aa_service.SHARE_PENDING

    # 幂等：同一付款操作重复登记不重复计数
    again = aa_domain.mark_share_paid(session, aa_id, shares["p_zhangsan"]["share_id"], operation_id=payment.operation_id)
    assert again["collected_minor"] == 3333
    assert len(again["collection_events"]) == 1

    # 余额在到账登记前后都不变（AA 不代扣、不入账）
    before_second = _balances(session)
    second = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=3333)
    after_all = aa_domain.mark_share_paid(session, aa_id, shares["p_lisi"]["share_id"], operation_id=second.operation_id)
    assert after_all["collected_minor"] == 6666
    assert after_all["outstanding_minor"] == 0
    assert after_all["status"] == aa_service.AA_SETTLED
    assert after_all["reconciled"] is True
    assert _balances(session) == before_second
    assert _ledger_counts(session) == (0, 0)

    # 已收份额不能重复收取
    third = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=3333)
    err = _assert_error(
        "POLICY_DENIED", aa_domain.mark_share_paid, session, aa_id, shares["p_lisi"]["share_id"], operation_id=third.operation_id
    )
    assert err.details["reason_codes"] == ["share_already_paid"]

    snapshot = get_snapshot(session, _ctx())
    assert snapshot["pending_receivables_minor"] == 0
    view = aa_domain.aa_view(session, _ctx(), aa_id)
    assert view["collected_minor"] == 6666
    assert view["self_funded_minor"] == 3334
    assert_contract(aa_domain.aa_contract_view(view), "aa_request.json")


# ---------------------------------------------------------------------------
# 4 退款调整
# ---------------------------------------------------------------------------


def test_refund_adjustment_is_traceable(session, seed):
    """原消费退款 → 可追溯调整；已收款份额只记待授权，不直接退钱。"""
    result, _ = _create(
        session,
        _three_people_args(
            participants=[
                _participant("张三", "p_zhangsan", PARTICIPANT),
                _participant("李四", "p_lisi"),
            ],
            linked_orders=[{"operation_id": "op_expense_1", "amount_minor": 10_000, "label": "海底捞"}],
        ),
    )
    aa_id = result["data"]["aa_id"]
    shares = _shares_by_ref(result["data"])
    assert result["data"]["linked_orders"] == ["op_expense_1"]

    payment = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=3333)
    aa_domain.mark_share_paid(session, aa_id, shares["p_zhangsan"]["share_id"], operation_id=payment.operation_id)

    before = _balances(session)
    # 退款 3000 分：按份额 3334/3333/3333 用最大余数法分摊 → 1000 / 1000 / 1000
    adjusted = aa_domain.apply_refund_adjustment(session, aa_id, original_operation_id="op_expense_1", refund_amount_minor=3000)

    deltas = {a["participant_ref"]: a["delta_minor"] for a in adjusted["adjustments"]}
    assert deltas == {"user_demo_01": 1000, "p_zhangsan": 1000, "p_lisi": 1000}
    assert sum(deltas.values()) == 3000
    assert adjusted["adjusted_total_minor"] == 3000
    for share in adjusted["participant_shares"]:
        assert share["adjusted_minor"] == 1000
        assert share["share_minor"] - share["adjusted_minor"] in (2334, 2333)
    # Σ 有效份额 == 实付 − 已退
    assert sum(s["share_minor"] - s["adjusted_minor"] for s in adjusted["participant_shares"]) == 10_000 - 3000

    # 已收款份额转待授权；未收款份额调整已生效；份额状态本身不变（合同枚举不含 refund_pending）
    by_ref = {a["participant_ref"]: a for a in adjusted["adjustments"]}
    assert by_ref["p_zhangsan"]["status"] == "pending_authorization"
    assert by_ref["p_zhangsan"]["requires_separate_authorization"] is True
    assert by_ref["p_lisi"]["status"] == "applied"
    assert by_ref["p_lisi"]["requires_separate_authorization"] is False
    # 发起人自付份额的退款随原订单回到发起人账户，不需要 AA 收款授权
    assert by_ref["user_demo_01"]["status"] == "applied"
    assert by_ref["user_demo_01"]["self_funded"] is True
    assert _shares_by_ref(adjusted)["p_zhangsan"]["status"] == aa_service.SHARE_PAID
    assert adjusted["refund_pending_minor"] == 1000
    assert {a["refund_id"] for a in adjusted["adjustments"]} == {adjusted["adjustments"][0]["refund_id"]}

    # 调整不产生任何资金变动
    assert _balances(session) == before
    assert _ledger_counts(session) == (0, 0)

    # 可追溯：视图返回调整记录与关联订单
    view = aa_domain.aa_view(session, _ctx(), aa_id)
    assert [a["original_operation_id"] for a in view["adjustments"]] == ["op_expense_1"] * 3
    assert view["linked_orders"] == ["op_expense_1"]
    assert view["linked_order_details"][0]["label"] == "海底捞"
    assert_contract(aa_domain.aa_contract_view(view), "aa_request.json")

    # 未知原订单与超额退款都被拒
    err = _assert_error(
        "PARAM_INVALID",
        aa_domain.apply_refund_adjustment,
        session,
        aa_id,
        original_operation_id="op_unknown",
        refund_amount_minor=100,
    )
    assert err.details["reason_codes"] == ["unknown_original_order"]
    # 给出 actor_ctx 时强制归属：无关用户不能登记退款调整
    _assert_error(
        "OBJECT_NOT_ACCESSIBLE",
        aa_domain.apply_refund_adjustment,
        session,
        aa_id,
        original_operation_id="op_expense_1",
        refund_amount_minor=100,
        actor_ctx=_ctx(PARTICIPANT),
    )
    err = _assert_error(
        "POLICY_DENIED",
        aa_domain.apply_refund_adjustment,
        session,
        aa_id,
        original_operation_id="op_expense_1",
        refund_amount_minor=8000,
    )
    assert err.details["reason_codes"] == ["refund_exceeds_original"]
    assert err.details["refunded_before_minor"] == 3000
    assert err.details["refundable_cap_minor"] == 10_000


def test_refund_on_paid_share_requires_separate_authorization(session, seed):
    """已收款份额的退款只记录待授权，不自动退；份额仍是已收，不能重复登记到账。"""
    result, _ = _create(
        session,
        {
            "title": "退款中的 AA",
            "final_cost_minor": 9_000,
            "participants": [_participant("张三", "p_zhangsan", PARTICIPANT)],
            "initiator_included": True,
            "linked_orders": [{"operation_id": "op_expense_2", "amount_minor": 9_000}],
        },
    )
    data = result["data"]
    share = next(s for s in data["participant_shares"] if s["participant_ref"] == "p_zhangsan")
    payment = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=4500)
    aa_domain.mark_share_paid(session, data["aa_id"], share["share_id"], operation_id=payment.operation_id)

    before = _balances(session)
    adjusted = aa_domain.apply_refund_adjustment(
        session, data["aa_id"], original_operation_id="op_expense_2", refund_amount_minor=1000
    )
    entries = [a for a in adjusted["adjustments"] if a["participant_ref"] == "p_zhangsan"]
    assert len(entries) == 1
    assert entries[0]["requires_separate_authorization"] is True
    assert entries[0]["delta_minor"] == 500  # 9000 分 2 人各 4500，退款 1000 → 各 500
    assert adjusted["refund_pending_minor"] == 500
    assert _shares_by_ref(adjusted)["p_zhangsan"]["status"] == aa_service.SHARE_PAID
    assert _balances(session) == before  # 不直接退钱

    another = _fake_payment_operation(session, owner_user_id=PARTICIPANT, amount_minor=4500)
    err = _assert_error(
        "POLICY_DENIED", aa_domain.mark_share_paid, session, data["aa_id"], share["share_id"], operation_id=another.operation_id
    )
    assert err.details["reason_codes"] == ["share_already_paid"]


# ---------------------------------------------------------------------------
# 5 幂等与读接口
# ---------------------------------------------------------------------------


def test_duplicate_create_is_idempotent(session, seed):
    typed_args = _three_people_args(title="幂等 AA")
    first, operation = _create(session, typed_args)
    second = aa_domain.TOOL_HANDLERS["aa.create"](session, _ctx(), operation)
    assert second["data"]["aa_id"] == first["data"]["aa_id"]
    assert _count(session, AARequest) == 1
    assert _count(session, AAShare) == 3

    # 清掉操作缓存后仍按 operation_id 命中既有 AA（持久层幂等）
    operation.domain_result = {}
    session.flush()
    third = aa_domain.TOOL_HANDLERS["aa.create"](session, _ctx(), operation)
    assert third["data"]["aa_id"] == first["data"]["aa_id"]
    assert third["already_created"] is True
    assert _count(session, AARequest) == 1
    assert _count(session, AAShare) == 3

    session.expire_all()
    assert session.get(Operation, operation.operation_id).status == "succeeded"


def test_aa_view_ownership_and_participant_access(session, seed):
    result, _ = _create(
        session,
        _three_people_args(
            title="可见性 AA",
            participants=[
                _participant("张三", "p_zhangsan", PARTICIPANT),
                _participant("李四", "p_lisi"),
            ],
        ),
    )
    aa_id = result["data"]["aa_id"]

    owner_view = aa_domain.aa_view(session, _ctx(), aa_id)
    assert owner_view["aa_id"] == aa_id
    assert len(owner_view["participant_shares"]) == 3
    # 参与人本人可以查看自己参与的 AA
    participant_view = aa_domain.aa_view(session, _ctx(PARTICIPANT), aa_id)
    assert participant_view["outstanding_minor"] == 6666
    # 无关用户与不存在的 AA 都是 OBJECT_NOT_ACCESSIBLE（不泄露存在性）
    _assert_error("OBJECT_NOT_ACCESSIBLE", aa_domain.aa_view, session, _ctx("user_stranger"), aa_id)
    _assert_error("OBJECT_NOT_ACCESSIBLE", aa_domain.aa_view, session, _ctx(), "aa_not_exists")
    assert [r["aa_id"] for r in aa_domain.list_aa_requests(session, _ctx())] == [aa_id]
    assert aa_domain.list_aa_requests(session, _ctx(), status=aa_service.AA_OPEN)[0]["aa_id"] == aa_id
    assert aa_domain.list_aa_requests(session, _ctx(), status=aa_service.AA_SETTLED) == []


def test_executor_registration_point(session, seed):
    """执行器按模块名 finpilot.domains.aa.service 收集 TOOL_HANDLERS。"""
    from finpilot.orchestration.executor import tool_handlers

    assert set(aa_domain.TOOL_HANDLERS) == {"aa.create"}
    assert aa_domain.TOOL_HANDLERS["aa.create"] is aa_domain.handle_aa_create
    assert tool_handlers().get("aa.create") is aa_domain.handle_aa_create
    assert aa_service.CONTRACT_KEYS == (
        "aa_id",
        "owner_user_id",
        "task_id",
        "title",
        "final_cost_minor",
        "status",
        "participant_shares",
        "linked_orders",
        "adjustments",
        "received_minor",
        "received_minor_is_spendable",
        "reconciled",
        "currency",
        "created_at",
        "updated_at",
    )


def test_jsonschema_helper_sanity():
    """合同校验器本身可用（防止契约测试静默通过）。"""
    with pytest.raises(AssertionError):
        assert_contract({"aa_id": "aa1"}, "aa_request.json")