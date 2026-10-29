"""订阅取消端到端链路（V5 第 13 节 S5-01/S5-03、S5-04；验收族 T14/T16/T22/T27/T31）。

一条完整链路跑真实 PostgreSQL：

1. **查询**：``subscription.list`` 带官方 YELLOW 例外标记（``requires_confirmation=True``），
   合同项与推断项分开，只有流水证据的疑似周期支出不可取消；
2. **取消商户合同**：``subscription.cancel`` 只改 ``Subscription.status``，银行代扣保持有效，
   并如实返回未解决义务；
3. **解除代扣**：``mandate.revoke``（RED，需强验证）只改 ``Mandate.status``，合同状态不被改写；
4. **核验两端状态与审计事实**：两张表的独立状态、操作状态与事件、授权消费、零退款、
   零账本分录、提醒同意记录不被静默删除。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from finpilot.domains.subscriptions import (
    READ_HANDLERS,
    SUBSCRIPTION_QUERY_EXCEPTION,
    cancel_reminder,
    cancel_subscription,
    cancel_subscription_and_mandate,
    create_reminder,
    detect_recurring,
    revoke_mandate,
    run_due_reminders,
)
from finpilot.errors import FinPilotError
from finpilot.ids import new_id
from finpilot.kernel.grants import activate_grant, create_exact_grant
from finpilot.kernel.identity import AuthContext
from finpilot.kernel.submission import OperationDraft, operation_view, prepare_operation, submit_operation
from finpilot.models import (
    BillingTransaction,
    GrantUse,
    LedgerEntry,
    LedgerTransaction,
    Mandate,
    OperationEvent,
    Refund,
    Reminder,
    ScheduledJob,
    Subscription,
)
from finpilot.timeutil import SHANGHAI, format_iso, to_shanghai
from finpilot.workers.scheduler import run_due_jobs

pytestmark = pytest.mark.integration

REFERENCE_TIME = datetime(2026, 10, 22, 10, 0, 0, tzinfo=SHANGHAI)
USER = "user_demo_01"
SUBSCRIPTION_ID = "sub_cloud_music"
MANDATE_ID = "man_cloud_music"


def ctx_for(user_id: str = USER, channel: str = "app") -> AuthContext:
    return AuthContext(user_id=user_id, roles=("customer",), channel=channel, session_id="sess_integration")


def run_write(session, ctx: AuthContext, tool: str, typed_args: dict, *, level="YELLOW", verification="NONE", handler=None):
    """真实写路径：prepare → 授权 → submit_operation → 领域处理器（同事务，同 operation_id 幂等）。"""
    draft = OperationDraft(tool=tool, typed_args=typed_args, required_level=level)
    operation = prepare_operation(session, ctx, draft)
    grant = create_exact_grant(
        session,
        ctx,
        plan_id=None,
        plan_revision=1,
        scope={"tool": tool, **typed_args},
        required_level=level,
        verification_level=verification,
    )
    if grant.status == "pending":
        activate_grant(session, ctx, grant_id=grant.grant_id, verification_level=verification or "TOTP")
    operation = submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
    if handler is None:
        return operation
    return operation, handler(session, ctx, operation)


def seed_billing(session, *, merchant_name: str, entries: list[tuple[int, int]], kind: str = "subscription") -> None:
    for days_ago, amount_minor in entries:
        posted_at = REFERENCE_TIME - timedelta(days=days_ago)
        session.add(
            BillingTransaction(
                bt_id=new_id("bt"),
                owner_user_id=USER,
                account_id="account_salary",
                posted_at=posted_at,
                business_day=to_shanghai(posted_at).date(),
                amount_minor=amount_minor,
                direction="out",
                kind=kind,
                category="entertainment",
                merchant_name=merchant_name,
                counterparty=merchant_name,
                memo=f"{merchant_name} 扣费",
                source="synthetic",
            )
        )
    session.flush()


def seed_contract_and_mandate(session) -> tuple[Subscription, Mandate]:
    subscription = Subscription(
        subscription_id=SUBSCRIPTION_ID,
        owner_user_id=USER,
        merchant_id="merchant_yunting",
        merchant_name="云听音乐",
        amount_minor=2_900,
        period="monthly",
        next_charge_at=REFERENCE_TIME + timedelta(days=30),
        status="active",
        evidence="contract",
        terms_version="2026.10",
        cancel_terms="可随时取消；已扣费用不退还，退款需另行申请。",
        last_charged_at=REFERENCE_TIME - timedelta(days=1),
        version=1,
        created_at=REFERENCE_TIME,
    )
    mandate = Mandate(
        mandate_id=MANDATE_ID,
        owner_user_id=USER,
        subscription_id=SUBSCRIPTION_ID,
        merchant_id="merchant_yunting",
        account_id="account_salary",
        status="active",
        version=1,
        created_at=REFERENCE_TIME,
    )
    session.add_all([subscription, mandate])
    session.flush()
    return subscription, mandate


def operation_events(session, operation_id: str) -> list[OperationEvent]:
    return list(
        session.execute(
            select(OperationEvent)
            .where(OperationEvent.operation_id == operation_id)
            .order_by(OperationEvent.created_at, OperationEvent.event_id)
        ).scalars()
    )


def test_subscription_cancellation_full_chain(session, seed, clock):
    ctx = ctx_for()
    subscription, mandate = seed_contract_and_mandate(session)
    seed_billing(session, merchant_name="云听音乐", entries=[(60, 2_900), (30, 2_900), (0, 2_900)])
    # 只有流水证据的第二个商户：不得声称存在可取消订阅
    seed_billing(session, merchant_name="云端备份", entries=[(360, 19_900), (0, 19_900)])

    # --- 1. 查询（官方 YELLOW 例外） -------------------------------------
    listing = READ_HANDLERS["subscription.list"](session, ctx, {})
    assert listing["requires_confirmation"] is True
    assert listing["required_level"] == "YELLOW"
    assert listing["policy"]["exception"] == SUBSCRIPTION_QUERY_EXCEPTION
    assert listing["contract_count"] == 1 and listing["inferred_count"] == 1

    contract_item = [i for i in listing["subscriptions"] if i["evidence"] == "contract"][0]
    assert contract_item["subscription_id"] == SUBSCRIPTION_ID
    assert contract_item["merchant_name"] == "云听音乐"
    assert contract_item["status"] == "active" and contract_item["cancellable"] is True
    assert contract_item["next_charge_at"] and contract_item["amount_minor"] == 2_900
    assert contract_item["mandate_status"] == "active"
    assert contract_item["source"]["observed_at"]

    inferred_item = [i for i in listing["subscriptions"] if i["evidence"] == "inferred"][0]
    assert inferred_item["merchant_name"] == "云端备份"
    assert inferred_item["cancellable"] is False and inferred_item["note"] == "仅疑似周期支出"
    assert inferred_item["confidence"] >= 0.5
    # 已有合同的商户不再以推断项重复出现
    assert [i["merchant_name"] for i in listing["subscriptions"] if i["evidence"] == "inferred"] == ["云端备份"]

    mandate_read = READ_HANDLERS["mandate.status"](session, ctx, {})
    assert mandate_read["active_count"] == 1 and mandate_read["requires_confirmation"] is False

    reminder_operation, reminder_result = run_write(
        session,
        ctx,
        "reminder.create",
        {
            "subscription_id": SUBSCRIPTION_ID,
            "due_at": format_iso(REFERENCE_TIME + timedelta(days=27)),
            "channel": "im",
            "timezone": "Asia/Shanghai",
            "dedupe_key": "renewal:cloud-music",
        },
        handler=create_reminder,
    )
    reminder_id = reminder_result["reminder"]["reminder_id"]
    assert reminder_result["delivery"]["sent_real_message"] is False

    # --- 2. 取消商户合同（只动合同） -------------------------------------
    contract_operation, contract_result = run_write(
        session,
        ctx,
        "subscription.cancel",
        {"subscription_id": SUBSCRIPTION_ID, "reason": "user_requested"},
        handler=cancel_subscription,
    )

    assert contract_result["status"] == "succeeded"
    assert contract_result["leg"] == "merchant_contract"
    assert contract_result["mandate_still_active"] is True
    assert contract_result["refund_created"] is False
    assert contract_result["pending_reminders"] == 1
    assert any("代扣" in note for note in contract_result["unresolved_obligations"])
    assert session.get(Subscription, SUBSCRIPTION_ID).status == "cancelled"
    assert session.get(Subscription, SUBSCRIPTION_ID).next_charge_at is None
    assert session.get(Mandate, MANDATE_ID).status == "active", "取消合同不得改动银行代扣状态"

    # --- 3. 解除银行代扣（RED + 强验证，只动代扣） ------------------------
    mandate_operation, mandate_result = run_write(
        session,
        ctx,
        "mandate.revoke",
        {"mandate_id": MANDATE_ID, "subscription_id": SUBSCRIPTION_ID},
        level="RED",
        verification="TOTP",
        handler=revoke_mandate,
    )

    assert mandate_result["status"] == "succeeded"
    assert mandate_result["leg"] == "bank_mandate"
    assert mandate_result["contract_still_active"] is False
    assert mandate_result["refund_created"] is False
    mandate_row = session.get(Mandate, MANDATE_ID)
    assert mandate_row.status == "revoked" and mandate_row.revoked_at is not None
    assert session.get(Subscription, SUBSCRIPTION_ID).status == "cancelled", "解除代扣不得改写合同状态"

    # --- 4. 分别核验两端状态与审计事实 -----------------------------------
    after = READ_HANDLERS["subscription.list"](session, ctx, {})
    item_after = [i for i in after["subscriptions"] if i["evidence"] == "contract"][0]
    assert item_after["status"] == "cancelled"
    assert item_after["cancellable"] is False and item_after["cancellable_reason"]
    assert item_after["mandate_status"] == "revoked" and item_after["mandate_active"] is False
    assert item_after["mandate"]["revoked_at"]
    assert READ_HANDLERS["mandate.status"](session, ctx, {})["active_count"] == 0

    contract_view = operation_view(session, ctx, contract_operation.operation_id)
    mandate_view = operation_view(session, ctx, mandate_operation.operation_id)
    assert contract_view["status"] == "succeeded" and contract_view["effect_confirmed"] is True
    assert mandate_view["status"] == "succeeded" and mandate_view["effect_confirmed"] is True
    assert contract_view["settled_at"] and mandate_view["settled_at"]
    assert contract_view["domain_result"]["domain"] == "subscriptions"
    assert contract_view["domain_result"]["result_type"] == "subscription_cancel"
    assert contract_view["domain_result"]["data"]["status"] == "cancelled"
    assert mandate_view["domain_result"]["domain"] == "subscriptions"
    assert mandate_view["domain_result"]["result_type"] == "mandate_revoke"
    assert mandate_view["domain_result"]["data"]["status"] == "revoked"
    assert mandate_view["domain_result"]["data"]["revoked_at"]

    contract_event_reasons = [e.reason for e in operation_events(session, contract_operation.operation_id)]
    mandate_event_reasons = [e.reason for e in operation_events(session, mandate_operation.operation_id)]
    # 审计链：准备 → 授权 → 提交 → 领域效果（同一冻结时钟内 created_at 相同，按集合核验）
    assert set(contract_event_reasons) == {"prepared", "authorization_granted", "submitted", "subscription_cancelled"}
    assert set(mandate_event_reasons) == {"prepared", "authorization_granted", "submitted", "mandate_revoked"}
    contract_event_states = [e.to_status for e in operation_events(session, contract_operation.operation_id)]
    assert contract_event_states[-1] == "succeeded" or "succeeded" in contract_event_states
    assert {e2.to_status for e2 in operation_events(session, mandate_operation.operation_id)} >= {
        "awaiting_authorization",
        "authorized",
        "dispatching",
        "succeeded",
    }

    used = session.execute(select(GrantUse)).scalars().all()
    assert len(used) == 3, "每个写操作各消费一次授权（合同、代扣、提醒）"
    assert {u.use_status for u in used} == {"consumed"}
    assert {u.operation_id for u in used} == {
        contract_operation.operation_id,
        mandate_operation.operation_id,
        reminder_operation.operation_id,
    }
    assert len({u.grant_id for u in used}) == 3, "同一授权不得被两个操作重复消费"

    # 取消不产生退款，也不产生账本分录；原始流水保持不变
    assert len(session.execute(select(Refund)).scalars().all()) == 0
    assert len(session.execute(select(LedgerTransaction)).scalars().all()) == 0
    assert len(session.execute(select(LedgerEntry)).scalars().all()) == 0
    assert len(session.execute(select(BillingTransaction)).scalars().all()) == 5

    # 合并视图对两端结果幂等：重放不产生第二次效果
    merged = cancel_subscription_and_mandate(session, ctx, contract_operation, mandate_operation=mandate_operation)
    assert merged["status"] == "succeeded"
    assert merged["contract_leg"]["result"]["idempotent_replay"] is True
    assert merged["mandate_leg"]["result"]["idempotent_replay"] is True
    assert session.get(Subscription, SUBSCRIPTION_ID).version == 2
    assert session.get(Mandate, MANDATE_ID).version == 2

    # --- 5. 提醒：同意记录不被静默删除，显式取消后不再触发 ---------------
    reminder_row = session.get(Reminder, reminder_id)
    assert reminder_row.status == "scheduled" and reminder_row.consent_at is not None
    assert reminder_row.channel == "im" and reminder_row.timezone == "Asia/Shanghai"
    job = session.execute(
        select(ScheduledJob).where(ScheduledJob.dedupe_key == f"reminder-job:{reminder_id}")
    ).scalars().one()
    assert job.state == "scheduled" and job.grant_id

    _, cancelled = run_write(
        session, ctx, "reminder.cancel", {"reminder_id": reminder_id}, handler=cancel_reminder
    )
    assert cancelled["job_state"] == "cancelled"
    clock.freeze(REFERENCE_TIME + timedelta(days=27, minutes=5))
    summary = run_due_jobs(session)
    domain = run_due_reminders(session)
    assert summary["leased"] == 0 and domain["fired"] == 0
    fired = session.execute(select(Reminder).where(Reminder.status == "fired")).scalars().all()
    assert fired == []
    assert reminder_operation.status == "succeeded"


def test_partial_success_chain_keeps_unresolved_obligations_visible(session, seed):
    """商户合同已取消 + 银行侧尚未确认解除：部分成功，且两端状态互不冒充、零退款。"""
    ctx = ctx_for()
    seed_contract_and_mandate(session)
    session.get(Mandate, MANDATE_ID).status = "pending_revocation"
    session.flush()

    contract_operation = run_write(session, ctx, "subscription.cancel", {"subscription_id": SUBSCRIPTION_ID})
    mandate_operation = run_write(
        session, ctx, "mandate.revoke", {"mandate_id": MANDATE_ID}, level="RED", verification="TOTP"
    )

    merged = cancel_subscription_and_mandate(session, ctx, contract_operation, mandate_operation=mandate_operation)

    assert merged["status"] == "partial_success"
    assert merged["legs_succeeded"] == ["merchant_contract"]
    assert merged["legs_failed"] == ["bank_mandate"]
    assert merged["mandate_leg"]["error"]["code"] == "CANCEL_NOT_EFFECTIVE"
    assert any("仍可能扣款" in note for note in merged["unresolved_obligations"])
    assert "不产生退款" in merged["refund_policy"]
    assert session.get(Subscription, SUBSCRIPTION_ID).status == "cancelled"
    assert session.get(Mandate, MANDATE_ID).status == "pending_revocation"
    assert len(session.execute(select(Refund)).scalars().all()) == 0

    # 显式失败：疑似周期项无法被取消，也不能凭空创建合同行
    seed_billing_operation = session.execute(select(BillingTransaction)).scalars().all()
    assert seed_billing_operation == []
    seed_billing(session, merchant_name="云端备份", entries=[(60, 5_000), (30, 5_000), (0, 5_000)])
    detected = detect_recurring(session, ctx)
    assert len(detected) == 1 and detected[0]["cancellable"] is False
    with pytest.raises(FinPilotError) as excinfo:
        run_write(
            session,
            ctx,
            "subscription.cancel",
            {"subscription_id": detected[0]["detection_id"]},
            handler=cancel_subscription,
        )
    assert excinfo.value.code == "OBJECT_NOT_ACCESSIBLE"