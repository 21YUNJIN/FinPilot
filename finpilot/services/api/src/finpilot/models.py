"""持久化数据模型（V5 第 4.2 节归属与外键）。

设计要点：
- 金额一律 BigInteger 整数分；份额用 1e-8 定点整数。
- 时间列一律 timestamptz，持久化 UTC；业务日另存 Asia/Shanghai 的 date。
- 每个业务对象带 version，供并发比较；历史快照只追加不改写。
- 归属通过 owner_user_id 外键或账户链路检查；不仅靠界面隐藏。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(astext_type=String()), "postgresql")

# --- 状态常量（字符串存储，便于迁移与审计可读） -------------------------

TASK_STATUSES = (
    "draft",
    "clarifying",
    "planning",
    "awaiting_authorization",
    "ready",
    "running",
    "reconciling",
    "replanning",
    "awaiting_human",
    "succeeded",
    "partially_succeeded",
    "failed",
    "cancelled",
    "blocked",
)
TASK_TERMINAL = {"succeeded", "partially_succeeded", "failed", "cancelled"}

OPERATION_STATUSES = (
    "created",
    "awaiting_authorization",
    "authorized",
    "dispatching",
    "pending",
    "unknown",
    "succeeded",
    "failed",
    "cancelled",
)

GRANT_STATUSES = ("pending", "active", "consumed", "revoked", "expired")

AUTH_LEVELS = ("GREEN", "YELLOW", "RED")

RESERVATION_STATUSES = ("active", "partially_captured", "captured", "released", "expired")

JOB_STATES = ("scheduled", "leased", "running", "completed", "awaiting_authorization", "missed", "failed", "cancelled")

CARD_LIFECYCLE = ("inactive", "active", "lost", "closed")
CARD_APPLICATION_STATUS = ("submitted", "under_review", "approved", "rejected", "withdrawn")

SUBSCRIPTION_STATUS = ("active", "cancelled", "pending_cancellation", "ended")
MANDATE_STATUS = ("active", "revoked", "pending_revocation")

WEALTH_ORDER_STATUS = (
    "submitted",
    "accepted",
    "awaiting_confirmation",
    "confirmed",
    "rejected",
    "redeeming",
    "settled",
    "failed",
    "cancelled",
)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# 身份与会话
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_ref: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    email_ref: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    totp_secret_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    roles: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (CheckConstraint("status in ('active','locked','disabled')", name="ck_users_status"),)


class SessionToken(Base):
    """认证会话。身份由服务端会话确定，请求体不能覆盖 user_id。"""

    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # app / im / operator
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# 资金：账户、账本、联系人、保护组、日额度、预留
# ---------------------------------------------------------------------------


class Account(Base):
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    account_type: Mapped[str] = mapped_column(String(24), nullable=False, default="salary")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    posted_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frozen_other_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    masked_number: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("posted_balance_minor >= 0", name="ck_accounts_non_negative"),
        CheckConstraint("frozen_other_minor >= 0", name="ck_accounts_frozen_non_negative"),
        CheckConstraint("status in ('active','frozen','closed')", name="ck_accounts_status"),
    )


class LedgerTransaction(Base):
    """双重记账事务头。每笔事务的分录金额之和必须为零。"""

    __tablename__ = "ledger_transactions"

    ledger_tx_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    memo: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("operation_id", "kind", name="uq_ledger_tx_operation_kind"),
        Index("ix_ledger_tx_posted_at", "posted_at"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    entry_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    ledger_tx_id: Mapped[str] = mapped_column(
        ForeignKey("ledger_transactions.ledger_tx_id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    signed_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Payee(Base):
    __tablename__ = "payees"

    payee_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_ref: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    destination_account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(64), nullable=False, default="模拟银行")
    aliases: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ProtectionPolicy(Base):
    """应急金底线。保护金额变更需单独授权（RED）。"""

    __tablename__ = "protection_policies"

    policy_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    protected_floor_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    account_scope: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyLimit(Base):
    """转账日额度桶：按用户 + 业务日 + 币种，串行计数，跨账户跨会话一致。"""

    __tablename__ = "daily_limits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    business_day: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    settled_transfer_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    held_pending_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (UniqueConstraint("user_id", "business_day", "currency", name="uq_daily_limit_bucket"),)


class DailyQuotaHold(Base):
    """待执行转账本金对当日额度的占用：并发分拆不能绕过等级升级。"""

    __tablename__ = "daily_quota_holds"

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    business_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    principal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="held")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("state in ('held','settled','released')", name="ck_quota_hold_state"),
        Index("ix_quota_hold_user_day", "user_id", "business_day", "state"),
    )


class Reservation(Base):
    """独立资金预留：创建不改 B，只减少可支配金额；捕获时 B 与 H 同减。"""

    __tablename__ = "reservations"

    reservation_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, default="task_budget")
    original_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remaining_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    released_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    inflight_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    long_lived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("remaining_minor >= 0 and captured_minor >= 0 and released_minor >= 0", name="ck_reservation_amounts"),
        CheckConstraint(
            "status in ('active','partially_captured','captured','released','expired')",
            name="ck_reservation_status",
        ),
        Index("ix_reservations_account_status", "account_id", "status"),
    )


class Refund(Base):
    """退款：引用原扣款，累计本金退款不得超过原扣款可退本金；到账后才增加余额。"""

    __tablename__ = "refunds"

    refund_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    original_operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 任务、计划与消息
# ---------------------------------------------------------------------------


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="app")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reference_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    goal_spec: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    clarifications: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    preferences: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    result_summary: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="mock")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','clarifying','planning','awaiting_authorization','ready','running',"
            "'reconciling','replanning','awaiting_human','succeeded','partially_succeeded','failed','cancelled','blocked')",
            name="ck_tasks_status",
        ),
    )


class TaskMessage(Base):
    __tablename__ = "task_messages"

    message_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant / system / operator
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="app")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskEvent(Base):
    """任务状态迁移与恢复事件：每次恢复必须产生事件。"""

    __tablename__ = "task_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Plan(Base):
    __tablename__ = "plans"

    plan_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    goal_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    snapshot_ref: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    required_level: Mapped[str] = mapped_column(String(8), nullable=False, default="YELLOW")
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    infeasible_reasons: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    alternatives: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    total_estimated_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("task_id", "revision", name="uq_plan_task_revision"),)


class PlanStep(Base):
    __tablename__ = "plan_steps"

    step_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.plan_id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node_id: Mapped[str] = mapped_column(String(48), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    typed_args: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    dependencies: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    read_set: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    write_set: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    preconditions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    expected_effects: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    compensation_policy: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    required_level: Mapped[str] = mapped_column(String(8), nullable=False, default="YELLOW")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (UniqueConstraint("plan_id", "node_id", name="uq_plan_step_node"),)


# ---------------------------------------------------------------------------
# 操作、授权、挑战
# ---------------------------------------------------------------------------


class Operation(Base):
    __tablename__ = "operations"

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    typed_args: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    parameters_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    required_level: Mapped[str] = mapped_column(String(8), nullable=False, default="GREEN")
    grant_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    source_account_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    provider_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    provider_order_ref: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    original_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    effect_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unknown_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    domain_result: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "operation_id", name="uq_operation_owner"),
        UniqueConstraint("provider_id", "provider_order_ref", name="uq_operation_provider_ref"),
        CheckConstraint(
            "status in ('created','awaiting_authorization','authorized','dispatching','pending','unknown',"
            "'succeeded','failed','cancelled')",
            name="ck_operations_status",
        ),
    )


class OperationEvent(Base):
    __tablename__ = "operation_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.operation_id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Grant(Base):
    """授权：精确授权绑定参数与修订；范围授权只在已批准子集内继承且共享额度。"""

    __tablename__ = "grants"

    grant_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="exact")  # exact / scoped
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="policy-2026.10")
    required_level: Mapped[str] = mapped_column(String(8), nullable=False, default="YELLOW")
    verification_level: Mapped[str] = mapped_column(String(8), nullable=False, default="NONE")
    challenge_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_total_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    approved_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lineage: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("status in ('pending','active','consumed','revoked','expired')", name="ck_grants_status"),
        CheckConstraint("scope_kind in ('exact','scoped')", name="ck_grants_scope_kind"),
    )


class GrantUse(Base):
    __tablename__ = "grant_uses"

    grant_use_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    grant_id: Mapped[str] = mapped_column(ForeignKey("grants.grant_id", ondelete="CASCADE"), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    authorized_parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    use_status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("grant_id", "operation_id", name="uq_grant_use_operation"),)


class AuthChallenge(Base):
    """强验证挑战：不含明文验证码；TOTP 计数器防重放。"""

    __tablename__ = "auth_challenges"

    challenge_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    operation_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    factor_type: Mapped[str] = mapped_column(String(24), nullable=False, default="totp")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("status in ('pending','verified','failed','locked','expired')", name="ck_challenge_status"),)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Receipt(Base):
    __tablename__ = "receipts"

    receipt_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(48), nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(96), nullable=False)
    source: Mapped[str] = mapped_column(String(48), nullable=False, default="provider_callback")
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_status: Mapped[str] = mapped_column(String(24), nullable=False, default="verified")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="accepted")

    __table_args__ = (UniqueConstraint("operation_id", "payload_hash", name="uq_receipt_operation_payload"),)


class ProviderEvent(Base):
    __tablename__ = "provider_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    provider_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    provider_event_id: Mapped[str] = mapped_column(String(96), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    signature_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_status: Mapped[str] = mapped_column(String(24), nullable=False, default="verified")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    conflict_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)

    __table_args__ = (UniqueConstraint("provider_id", "provider_event_id", name="uq_provider_event"),)


class Outbox(Base):
    __tablename__ = "outbox"

    outbox_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    delivery_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    job_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    timezone: Mapped[str] = mapped_column(String(48), nullable=False, default="Asia/Shanghai")
    grant_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="scheduled")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "state in ('scheduled','leased','running','completed','awaiting_authorization','missed','failed','cancelled')",
            name="ck_job_state",
        ),
    )


class CircuitBreaker(Base):
    __tablename__ = "circuit_breakers"

    scope: Mapped[str] = mapped_column(String(96), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="closed")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    refusal_timestamps: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    """追加写审计链：事件哈希链 + 独立签名摘要；不保存模型私有思维链与验证码。"""

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False, default="system")
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    grant_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    signature: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 领域：理财
# ---------------------------------------------------------------------------


class RiskProfile(Base):
    __tablename__ = "risk_profiles"

    profile_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    questionnaire_version: Mapped[str] = mapped_column(String(32), nullable=False)
    answers: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False, default="R1")
    max_loss_tolerance: Mapped[str] = mapped_column(String(16), nullable=False, default="low")
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WealthProduct(Base):
    __tablename__ = "wealth_products"

    product_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    issuer: Mapped[str] = mapped_column(String(64), nullable=False, default="模拟银行理财")
    product_type: Mapped[str] = mapped_column(String(32), nullable=False, default="fixed_income")
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False, default="2026.10")
    issuer_risk_level: Mapped[str] = mapped_column(String(8), nullable=False, default="R2")
    min_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fee_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fee_fixed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    term_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_days: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    cutoff_time: Mapped[str] = mapped_column(String(8), nullable=False, default="15:00")
    liquidity_terms: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cancel_terms: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    cooling_off_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    settlement_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    history_ref: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProductNav(Base):
    __tablename__ = "product_navs"

    nav_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(ForeignKey("wealth_products.product_id"), nullable=False, index=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    nav_e6: Mapped[int] = mapped_column(BigInteger, nullable=False)
    daily_return_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False, default="2026.10")

    __table_args__ = (UniqueConstraint("product_id", "as_of", name="uq_nav_product_day"),)


class Holding(Base):
    __tablename__ = "holdings"

    holding_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("wealth_products.product_id"), nullable=False)
    confirmed_shares_e8: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frozen_shares_e8: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    valuation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (UniqueConstraint("owner_user_id", "account_id", "product_id", name="uq_holding"),)


class WealthOrder(Base):
    __tablename__ = "wealth_orders"

    order_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("wealth_products.product_id"), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False, default="purchase")
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_shares_e8: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    confirmed_shares_e8: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")
    product_terms_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    reject_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 领域：卡片
# ---------------------------------------------------------------------------


class Card(Base):
    __tablename__ = "cards"

    card_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), nullable=False)
    masked_pan: Mapped[str] = mapped_column(String(24), nullable=False, default="**** **** **** 0000")
    brand: Mapped[str] = mapped_column(String(24), nullable=False, default="VIRTUAL")
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="inactive")
    credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    replaces_card_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    replaced_by_card_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("lifecycle_status in ('inactive','active','lost','closed')", name="ck_card_lifecycle"),
    )


class CardApplication(Base):
    __tablename__ = "card_applications"

    application_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="new_card")
    card_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    requested_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    approved_limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    application_status: Mapped[str] = mapped_column(String(24), nullable=False, default="submitted")
    decision_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(48), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "application_status in ('submitted','under_review','approved','rejected','withdrawn')",
            name="ck_card_application_status",
        ),
    )


class CardControl(Base):
    """叠加式卡控：用户锁、渠道锁、风控冻结、限额可同时存在。"""

    __tablename__ = "card_controls"

    control_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.card_id", ondelete="CASCADE"), nullable=False, index=True)
    control_type: Mapped[str] = mapped_column(String(24), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(24), nullable=True)
    limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(48), nullable=False, default="user")
    created_by_role: Mapped[str] = mapped_column(String(32), nullable=False, default="customer")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    released_by: Mapped[str | None] = mapped_column(String(48), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 领域：订阅与代扣
# ---------------------------------------------------------------------------


class Subscription(Base):
    __tablename__ = "subscriptions"

    subscription_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    merchant_id: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    merchant_name: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    period: Mapped[str] = mapped_column(String(16), nullable=False, default="monthly")
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    evidence: Mapped[str] = mapped_column(String(16), nullable=False, default="contract")  # contract / inferred
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False, default="2026.10")
    cancel_terms: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    last_charged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("evidence in ('contract','inferred')", name="ck_subscription_evidence"),)


class Mandate(Base):
    """银行代扣授权，与商户合同状态互相独立。"""

    __tablename__ = "mandates"

    mandate_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    merchant_id: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    account_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Reminder(Base):
    __tablename__ = "reminders"

    reminder_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="app")
    timezone: Mapped[str] = mapped_column(String(48), nullable=False, default="Asia/Shanghai")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 领域：账单
# ---------------------------------------------------------------------------


class BillingTransaction(Base):
    __tablename__ = "billing_transactions"

    bt_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    business_day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="out")  # in / out
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="purchase")
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="other")
    merchant_name: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    counterparty: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    memo: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    linked_bt_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    anomaly_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    untrusted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="synthetic")

    __table_args__ = (Index("ix_bt_owner_day", "owner_user_id", "business_day"),)


class CategoryCorrection(Base):
    __tablename__ = "category_corrections"

    correction_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    bt_id: Mapped[str] = mapped_column(ForeignKey("billing_transactions.bt_id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    from_category: Mapped[str] = mapped_column(String(24), nullable=False)
    to_category: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 领域：AA
# ---------------------------------------------------------------------------


class AARequest(Base):
    __tablename__ = "aa_requests"

    aa_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    final_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    linked_orders: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    adjustments: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AAShare(Base):
    __tablename__ = "aa_shares"

    share_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    aa_id: Mapped[str] = mapped_column(ForeignKey("aa_requests.aa_id", ondelete="CASCADE"), nullable=False, index=True)
    participant_ref: Mapped[str] = mapped_column(String(96), nullable=False)
    participant_user_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    share_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adjusted_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    is_initiator: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# 其他：报价缓存、沙箱运行、运营接管、删除请求
# ---------------------------------------------------------------------------


class MerchantQuote(Base):
    __tablename__ = "merchant_quotes"

    quote_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    offer_code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(96), nullable=False, default="")
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cancel_fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    refundable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    inventory: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False, default="2026.10")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    untrusted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    run_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    resources: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    stdout_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperatorCase(Base):
    __tablename__ = "operator_cases"

    case_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    owner_user_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    handoff: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    actions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    assigned_role: Mapped[str] = mapped_column(String(32), nullable=False, default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataDeletionRequest(Base):
    __tablename__ = "data_deletion_requests"

    request_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")
    deleted_counts: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


ALL_TABLES = list(Base.metadata.tables.keys())