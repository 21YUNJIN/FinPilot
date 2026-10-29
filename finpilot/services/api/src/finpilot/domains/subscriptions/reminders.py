"""续费提醒领域逻辑（V5 第 13 节 S5-02、T15/T22）。

设计要点：

- **同意记录**：``create_reminder`` 写一条 ``reminders`` 行作为同意记录（``consent_at``、渠道、
  时区、``dedupe_key``），重复创建按键幂等，不产生第二条设置。
- **持久调度**：通过 ``kernel.scheduled_jobs.create_job(job_type="reminder.fire", ...)`` 建持久作业，
  并签发一条覆盖执行时刻的未来授权（``job.grant_id``）。作业带时区、允许窗口与重启恢复语义。
- **逻辑提醒**：到点后 ``fire_reminder`` 生成一条 ``status='fired'``、``simulated=True`` 的
  *逻辑提醒行*（通知实例），并在返回结构中明确标注**模拟渠道、未发送真实短信/IM**。
  取消后不再新增，同一同意记录最多生成一条逻辑提醒（``<同意键>:fire`` 去重）。
- **作业接线**：``JOB_HANDLERS = {"reminder.fire": handle_reminder_job}`` 供
  ``workers.scheduler`` 的 ``job_type`` 分派点使用；未接线时用 :func:`run_due_reminders`
  按同一契约（到期、窗口、状态机、幂等）执行。

返回结构：写处理器返回 ``DomainResult``（``domain/result_type/data/source/observed_at/warnings``，
见 ``contracts/schemas/domain_result.json``），界面聚合字段放在顶层。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import get_settings
from ...errors import FinPilotError
from ...ids import canonical_hash, new_id
from ...kernel.grants import create_exact_grant
from ...kernel.identity import AuthContext
from ...kernel.scheduled_jobs import cancel_job, create_job, in_window, mark_completed, mark_missed
from ...kernel.state_machine import assert_transition
from ...kernel.submission import transition
from ...models import Operation, Reminder, ScheduledJob, User
from ...timeutil import SHANGHAI, format_iso, parse_iso, to_utc, utcnow

DOMAIN = "subscriptions"
RESULT_TYPE_CREATE = "reminder_create"
RESULT_TYPE_CANCEL = "reminder_cancel"
SOURCE = "local_ledger"
JOB_TYPE = "reminder.fire"

#: 提醒允许执行窗口（分钟）。提醒不是资金动作，窗口主要用于抵抗重启；
#: 默认转账窗口 10 分钟对「到点提醒」过窄，超出窗口才进入 missed 并等待重新安排。
REMINDER_WINDOW_MINUTES = 60

#: 渠道与时区按 ``contracts/schemas/reminder.json`` 冻结：app / im，Asia/Shanghai
ALLOWED_CHANNELS = ("app", "im")
CHANNEL_LABELS: dict[str, str] = {
    "app": "APP 站内提醒（模拟渠道）",
    "im": "IM 会话提醒（模拟渠道）",
}
SUPPORTED_TIMEZONES = ("Asia/Shanghai",)
DEFAULT_TIMEZONE = "Asia/Shanghai"
SIMULATED_NOTE = "模拟渠道展示：未通过短信网关或 IM 服务发送真实消息。"

#: 周期别名：合同契约用 yearly，识别与提醒内部统一为 yearly（接受 annual 作为别名）
PERIOD_ALIASES: dict[str, str] = {
    "weekly": "weekly",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "yearly": "yearly",
    "annual": "yearly",
}
PERIODS = tuple(dict.fromkeys(PERIOD_ALIASES.values()))
PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}

WRITE_OPERATION_STATUSES = ("authorized", "dispatching", "pending", "succeeded")

#: 同意记录去重键前缀（与「逻辑提醒」的 ``<同意键>:fire`` 区分开）
CONSENT_KEY_PREFIX = "reminder-consent:"


def normalize_period(period: str | None) -> str | None:
    """把周期写法归一（``annual`` → ``yearly``）；未知周期报 PARAM_INVALID。"""
    if period is None:
        return None
    key = str(period).strip().lower()
    canonical = PERIOD_ALIASES.get(key)
    if canonical is None:
        raise FinPilotError(
            "PARAM_INVALID",
            "周期只支持 weekly / monthly / quarterly / yearly。",
            details={"reason_codes": ["unsupported_period"], "period": period},
        )
    return canonical


def shift_period(moment: datetime, period: str) -> datetime:
    """按日历推进一个周期，日号超出目标月份时钳制到月末。"""
    canonical = normalize_period(period)
    assert canonical is not None
    if canonical == "weekly":
        return moment + timedelta(days=7)
    total = moment.month - 1 + PERIOD_MONTHS[canonical]
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def reminder_contract_view(reminder: Reminder) -> dict:
    """``contracts/schemas/reminder.json`` 形状的提醒对象（同意记录或逻辑提醒）。"""
    return {
        "reminder_id": reminder.reminder_id,
        "owner_user_id": reminder.owner_user_id,
        "subscription_id": reminder.subscription_id,
        "channel": reminder.channel,
        "timezone": reminder.timezone,
        "due_at": format_iso(reminder.due_at),
        "consent_at": format_iso(reminder.consent_at),
        "dedupe_key": reminder.dedupe_key,
        "status": reminder.status,
        "simulated": bool(reminder.simulated),
        "created_at": format_iso(reminder.created_at),
    }


def reminder_view(reminder: Reminder) -> dict:
    """提醒展示视图：区分「同意记录」与「已触发的逻辑提醒」，并标注模拟渠道。"""
    try:
        zone = ZoneInfo(reminder.timezone or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - 建库时已校验
        zone = SHANGHAI
    kind = {"fired": "notification", "scheduled": "consent", "cancelled": "consent"}.get(reminder.status, "consent")
    return {
        **reminder_contract_view(reminder),
        "kind": kind,
        "status_label": {
            "scheduled": "提醒已设置",
            "fired": "逻辑提醒已生成（模拟渠道）",
            "cancelled": "提醒已取消",
        }.get(reminder.status, reminder.status),
        "channel_label": CHANNEL_LABELS.get(reminder.channel, f"{reminder.channel}（模拟渠道）"),
        "due_local": reminder.due_at.astimezone(zone).isoformat(timespec="seconds"),
        "delivery": {
            "mode": "simulated",
            "channel": reminder.channel,
            "channel_label": CHANNEL_LABELS.get(reminder.channel, f"{reminder.channel}（模拟渠道）"),
            "simulated": True,
            "sent_real_message": False,
            "note": SIMULATED_NOTE,
        },
        "source": {
            "table": "reminders",
            "data_source": "本行提醒设置与逻辑提醒记录",
            "observed_at": format_iso(utcnow()),
        },
    }


def consent_dedupe_key(value: str | None, *, fallback: str) -> str:
    """同意记录去重键：调用方给出的键原样使用（幂等），否则用确定性回退键。"""
    raw = str(value or "").strip() or fallback
    return raw if raw.startswith(CONSENT_KEY_PREFIX) else f"{CONSENT_KEY_PREFIX}{raw}"


def job_view(job: ScheduledJob) -> dict:
    """调度作业视图：给出到期时刻、时区、窗口与授权引用，便于「重启后仍执行」核对。"""
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "state": job.state,
        "due_at": format_iso(job.due_at),
        "window_minutes": job.window_minutes,
        "timezone": job.timezone,
        "attempts": job.attempts,
        "grant_id": job.grant_id,
        "dedupe_key": job.dedupe_key,
        "payload": dict(job.payload or {}),
        "created_at": format_iso(job.created_at),
    }


def list_reminders(session: Session, ctx: AuthContext, *, include_fired: bool = True) -> list[dict]:
    """当前用户的提醒清单（同意记录与已触发逻辑提醒）。"""
    stmt = select(Reminder).where(Reminder.owner_user_id == ctx.user_id)
    if not include_fired:
        stmt = stmt.where(Reminder.status != "fired")
    rows = session.execute(stmt.order_by(Reminder.created_at)).scalars().all()
    return [reminder_view(r) for r in rows]


# ---------------------------------------------------------------------------
# 写处理器：提醒设置 / 取消
# ---------------------------------------------------------------------------


def _assert_write_operation(ctx: AuthContext, operation: Operation, *, tool: str) -> None:
    ctx.require_customer()
    if operation is None or operation.owner_user_id != ctx.user_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该操作。")
    if operation.tool != tool:
        raise FinPilotError(
            "PARAM_INVALID",
            f"操作与工具不一致：期望 {tool}。",
            details={"reason_codes": ["tool_mismatch"], "tool": operation.tool},
        )
    if operation.status not in WRITE_OPERATION_STATUSES:
        raise FinPilotError(
            "POLICY_DENIED",
            "该操作尚未通过提交事务，不能产生业务效果。",
            details={"reason_codes": ["operation_not_submitted"], "status": operation.status},
        )


def _stored_result(operation: Operation, result_type: str) -> dict | None:
    """按 operation_id 幂等：返回首次领域结果（兼容执行器直接存入的 DomainResult）。"""
    stored = operation.domain_result or {}
    if stored.get("domain") != DOMAIN or stored.get("result_type") != result_type:
        return None
    return dict(stored)


def _store_result(session: Session, operation: Operation, result: dict) -> None:
    operation.domain_result = result
    session.flush()


def _domain_result(*, result_type: str, data: dict, warnings: list[str] | None = None, **extra) -> dict:
    """DomainResult：``data`` 放合同对象，展示型聚合放顶层。"""
    result = {
        "domain": DOMAIN,
        "result_type": result_type,
        "data": data,
        "source": SOURCE,
        "observed_at": format_iso(utcnow()),
        "warnings": list(warnings or []),
    }
    result.update(extra)
    return result


def _parse_due_at(raw: object) -> datetime:
    if isinstance(raw, datetime):
        return to_utc(raw)
    if not raw or not isinstance(raw, str):
        raise FinPilotError(
            "PARAM_INVALID",
            "提醒时间必填，格式为带时区的 ISO 8601。",
            details={"reason_codes": ["missing_due_at"]},
        )
    try:
        return parse_iso(raw)
    except ValueError as exc:
        raise FinPilotError(
            "PARAM_INVALID",
            "提醒时间必须是带时区偏移的 ISO 8601。",
            details={"reason_codes": ["bad_due_at"]},
        ) from exc


def _validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise FinPilotError(
            "PARAM_INVALID",
            "时区名称无效。",
            details={"reason_codes": ["bad_timezone"], "timezone": name},
        ) from exc
    if name not in SUPPORTED_TIMEZONES:
        # 契约冻结为 Asia/Shanghai；支持更多时区需要先改 contracts/schemas/reminder.json
        raise FinPilotError(
            "PARAM_INVALID",
            "当前版本提醒仅支持 Asia/Shanghai 时区。",
            details={"reason_codes": ["unsupported_timezone"], "timezone": name},
        )
    return name


def create_reminder(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """工具 ``reminder.create``：记录同意 + 建持久调度，重复创建按键幂等。"""
    _assert_write_operation(ctx, operation, tool="reminder.create")

    stored = _stored_result(operation, RESULT_TYPE_CREATE)
    if stored is not None:
        stored["idempotent_replay"] = True
        return stored

    args = operation.typed_args or {}
    subscription_id = args.get("subscription_id")
    due_at = _parse_due_at(args.get("due_at"))
    now = utcnow()
    if due_at <= now:
        raise FinPilotError(
            "PARAM_INVALID",
            "提醒时间必须晚于当前时间。",
            details={"reason_codes": ["due_at_in_past"], "due_at": format_iso(due_at)},
        )

    channel = str(args.get("channel") or (ctx.channel if ctx.channel in ALLOWED_CHANNELS else "app"))
    if channel not in ALLOWED_CHANNELS:
        raise FinPilotError(
            "PARAM_INVALID",
            "提醒渠道不受支持（仅 app / im）。",
            details={"reason_codes": ["unsupported_channel"], "channel": channel},
        )
    timezone_name = _validate_timezone(str(args.get("timezone") or DEFAULT_TIMEZONE))
    period = normalize_period(args.get("period"))

    subscription = None
    if subscription_id:
        from ...models import Subscription

        subscription = session.get(Subscription, subscription_id)
        if subscription is None or subscription.owner_user_id != ctx.user_id:
            raise FinPilotError(
                "OBJECT_NOT_ACCESSIBLE",
                "无法访问该订阅。",
                details={"reason_codes": ["subscription_not_accessible"]},
            )

    dedupe_key = consent_dedupe_key(
        args.get("dedupe_key"),
        fallback=canonical_hash(
            {
                "user_id": ctx.user_id,
                "subscription_id": subscription_id,
                "due_at": format_iso(due_at),
                "channel": channel,
            }
        )[:48],
    )

    existing = session.execute(select(Reminder).where(Reminder.dedupe_key == dedupe_key)).scalar_one_or_none()
    if existing is not None:
        if existing.owner_user_id != ctx.user_id:
            raise FinPilotError(
                "IDEMPOTENCY_CONFLICT",
                "同一去重键已被其他提醒使用，已拒绝。",
                details={"reason_codes": ["dedupe_key_conflict"]},
            )
        result = _reminder_result(session, existing, operation=operation, idempotent=True, warnings=[])
        _store_result(session, operation, result)
        transition(
            session, operation, "succeeded", reason="reminder_duplicate", detail={"reminder_id": existing.reminder_id}
        )
        session.flush()
        return result

    warnings: list[str] = []
    if subscription is not None and subscription.next_charge_at is not None and due_at > subscription.next_charge_at:
        warnings.append("提醒时间晚于已知的下一次扣费日，请确认是否需要提前。")

    reminder = Reminder(
        reminder_id=new_id("rem"),
        owner_user_id=ctx.user_id,
        subscription_id=subscription_id,
        channel=channel,
        timezone=timezone_name,
        due_at=due_at,
        consent_at=now,
        dedupe_key=dedupe_key,
        status="scheduled",
        simulated=True,
        created_at=now,
    )
    session.add(reminder)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as exc:  # 并发同键：幂等返回既有记录，不污染外层事务
        raise FinPilotError(
            "IDEMPOTENCY_CONFLICT",
            "同一去重键的提醒已存在，请查询既有提醒。",
            details={"reason_codes": ["dedupe_key_conflict"]},
        ) from exc

    ttl_seconds = max(
        get_settings().grant_exact_ttl_seconds,
        int((due_at - now).total_seconds()) + 3600,
    )
    grant = create_exact_grant(
        session,
        ctx,
        plan_id=operation.plan_id,
        plan_revision=operation.plan_revision,
        scope={
            "kind": "exact",
            "tool": "reminder.create",
            "reminder_id": reminder.reminder_id,
            "dedupe_key": dedupe_key,
            "channel": channel,
            "due_at": format_iso(due_at),
            "purpose": "scheduled_reminder_fire",
        },
        required_level="YELLOW",
        ttl_seconds=ttl_seconds,
    )

    fire_args = {
        "reminder_id": reminder.reminder_id,
        "subscription_id": subscription_id,
        "channel": channel,
        "timezone": timezone_name,
        "dedupe_key": dedupe_key,
        "due_at": format_iso(due_at),
        "consent_at": format_iso(now),
    }
    job = create_job(
        session,
        ctx,
        job_type=JOB_TYPE,
        due_at=due_at,
        payload={
            # 兼容尚未接入 job_type 分派的调度器：payload 仍带一个已登记工具。
            "tool": "reminder.create",
            "typed_args": fire_args,
            "reminder_id": reminder.reminder_id,
            "subscription_id": subscription_id,
            "channel": channel,
            "timezone": timezone_name,
            "consent_at": format_iso(now),
            "simulated": True,
            "period": period,
            "repeat": bool(period),
            "plan_revision": operation.plan_revision,
        },
        task_id=operation.task_id,
        grant_id=grant.grant_id,
        window_minutes=REMINDER_WINDOW_MINUTES,
        dedupe_key=f"reminder-job:{reminder.reminder_id}",
    )

    result = _reminder_result(session, reminder, operation=operation, idempotent=False, warnings=warnings)
    result["job"] = job_view(job)
    _store_result(session, operation, result)
    transition(
        session, operation, "succeeded", reason="reminder_scheduled", detail={"reminder_id": reminder.reminder_id}
    )
    session.flush()
    return result


def _reminder_result(
    session: Session,
    reminder: Reminder,
    *,
    operation: Operation,
    idempotent: bool,
    warnings: list[str],
) -> dict:
    return _domain_result(
        result_type=RESULT_TYPE_CREATE,
        data=reminder_contract_view(reminder),
        warnings=warnings,
        status="scheduled",
        tool="reminder.create",
        leg="reminder",
        operation_id=operation.operation_id,
        task_id=operation.task_id,
        idempotent_duplicate=idempotent,
        reminder=reminder_view(reminder),
        consent={
            "at": format_iso(reminder.consent_at),
            "channel": reminder.channel,
            "timezone": reminder.timezone,
            "dedupe_key": reminder.dedupe_key,
            "operation_id": operation.operation_id,
            "source": "user_confirmed_operation",
        },
        delivery={
            "mode": "simulated",
            "channel": reminder.channel,
            "channel_label": CHANNEL_LABELS.get(reminder.channel, f"{reminder.channel}（模拟渠道）"),
            "sent_real_message": False,
            "note": SIMULATED_NOTE,
        },
    )


def cancel_reminder(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """工具 ``reminder.cancel``：取消提醒设置与对应调度，取消后到点不再新增提醒。"""
    _assert_write_operation(ctx, operation, tool="reminder.cancel")

    stored = _stored_result(operation, RESULT_TYPE_CANCEL)
    if stored is not None:
        stored["idempotent_replay"] = True
        return stored

    args = operation.typed_args or {}
    reminder_id = args.get("reminder_id")
    dedupe_key = args.get("dedupe_key")
    if not reminder_id and not dedupe_key:
        raise FinPilotError(
            "PARAM_INVALID",
            "取消提醒需要提供 reminder_id 或 dedupe_key。",
            details={"reason_codes": ["missing_reminder_reference"]},
        )
    stmt = select(Reminder).where(Reminder.owner_user_id == ctx.user_id)
    if reminder_id:
        stmt = stmt.where(Reminder.reminder_id == reminder_id)
    else:
        stmt = stmt.where(Reminder.dedupe_key == consent_dedupe_key(dedupe_key, fallback=str(dedupe_key or "")))
    reminder = session.execute(stmt.with_for_update()).scalars().first()
    if reminder is None:
        raise FinPilotError(
            "OBJECT_NOT_ACCESSIBLE",
            "无法访问该提醒。",
            details={"reason_codes": ["reminder_not_found"]},
        )

    now = utcnow()
    if reminder.status == "fired":
        raise FinPilotError(
            "CANCEL_NOT_EFFECTIVE",
            "该提醒已经触发，无法撤回已生成的逻辑提醒。",
            details={"reason_codes": ["already_fired"], "reminder_id": reminder.reminder_id},
        )

    already_cancelled = reminder.status == "cancelled"
    if not already_cancelled:
        reminder.status = "cancelled"
        session.flush()

    # 周期提醒可能同时存在多个未来作业（当前周期 + 后续周期），取消必须覆盖全部未终态作业
    jobs = session.execute(
        select(ScheduledJob)
        .where(ScheduledJob.dedupe_key.like(f"reminder-job:{reminder.reminder_id}%"))
        .order_by(ScheduledJob.due_at)
    ).scalars().all()
    cancelled_job_ids: list[str] = []
    for job in jobs:
        if job.state in ("scheduled", "leased", "running", "awaiting_authorization"):
            cancel_job(session, ctx, job_id=job.job_id, reason="reminder_cancelled")
            cancelled_job_ids.append(job.job_id)
    job_state = "cancelled" if cancelled_job_ids else (jobs[-1].state if jobs else "missing")

    result = _domain_result(
        result_type=RESULT_TYPE_CANCEL,
        data=reminder_contract_view(reminder),
        warnings=[],
        status="cancelled",
        tool="reminder.cancel",
        leg="reminder",
        operation_id=operation.operation_id,
        task_id=operation.task_id,
        already_cancelled=already_cancelled,
        reminder=reminder_view(reminder),
        job_id=jobs[0].job_id if jobs else None,
        job_ids=[job.job_id for job in jobs],
        cancelled_job_ids=cancelled_job_ids,
        job_state=job_state,
        notifications_sent=0,
        note="已取消：到点后不再生成新的逻辑提醒（已生成的模拟提醒记录保留，用于核对同意与取消时间）。",
    )
    _store_result(session, operation, result)
    transition(
        session, operation, "succeeded", reason="reminder_cancelled", detail={"reminder_id": reminder.reminder_id}
    )
    session.flush()
    return result


# ---------------------------------------------------------------------------
# 到点执行：生成逻辑提醒（模拟渠道）
# ---------------------------------------------------------------------------


def _delivery_block(reminder: Reminder) -> dict:
    return {
        "mode": "simulated",
        "channel": reminder.channel,
        "channel_label": CHANNEL_LABELS.get(reminder.channel, f"{reminder.channel}（模拟渠道）"),
        "simulated": True,
        "sent_real_message": False,
        "note": SIMULATED_NOTE,
    }


def fire_reminder(session: Session, job: ScheduledJob, *, now: datetime | None = None) -> dict:
    """到点生成一条逻辑提醒（``status='fired'``、``simulated=True``）。

    幂等：同一同意记录最多一条逻辑提醒（``<同意去重键>:fire``）；
    取消后的设置不再新增提醒；超出允许窗口不补发。
    """
    moment = to_utc(now) if now is not None else utcnow()
    payload = dict(job.payload or {})
    typed_args = dict(payload.get("typed_args") or {})
    reminder_id = payload.get("reminder_id") or typed_args.get("reminder_id")

    base = {
        "fired": False,
        "reason": "",
        "job": {
            "job_id": job.job_id,
            "state": job.state,
            "due_at": format_iso(job.due_at),
            "timezone": job.timezone,
            "attempts": job.attempts,
        },
        "simulated": True,
        "observed_at": format_iso(moment),
    }

    if not reminder_id:
        base["reason"] = "consent_record_missing"
        return base

    setting = session.get(Reminder, reminder_id)
    if setting is None:
        base["reason"] = "consent_record_missing"
        return base
    if setting.status == "fired":
        base["reason"] = "already_fired"
        base["reminder"] = reminder_view(setting)
        return base
    if setting.status == "cancelled":
        base["reason"] = "cancelled"
        base["reminder"] = reminder_view(setting)
        return base

    if job.state in ("cancelled", "missed", "failed"):
        base["reason"] = f"job_{job.state}"
        return base

    if not in_window(job, moment):
        base["reason"] = "out_of_window"
        return base

    fired_key = f"{setting.dedupe_key}:fire"
    existing = session.execute(select(Reminder).where(Reminder.dedupe_key == fired_key)).scalar_one_or_none()
    if existing is not None:
        base["reason"] = "already_fired"
        base["reminder"] = reminder_view(existing)
        return base

    owner = session.get(User, setting.owner_user_id)
    if owner is None:
        base["reason"] = "consent_record_missing"
        return base

    fired = Reminder(
        reminder_id=new_id("rem"),
        owner_user_id=setting.owner_user_id,
        subscription_id=setting.subscription_id,
        channel=setting.channel,
        timezone=setting.timezone,
        due_at=job.due_at,
        consent_at=setting.consent_at,
        dedupe_key=fired_key,
        status="fired",
        simulated=True,
        created_at=moment,
    )
    session.add(fired)
    try:
        with session.begin_nested():  # 并发重复触发：嵌套事务回滚，不影响外层调度事务
            session.flush()
    except IntegrityError:
        existing = session.execute(select(Reminder).where(Reminder.dedupe_key == fired_key)).scalar_one_or_none()
        base["reason"] = "already_fired"
        if existing is not None:
            base["reminder"] = reminder_view(existing)
        return base

    next_job = _schedule_next_occurrence(session, setting, job, owner=owner, moment=moment)

    base.update(
        {
            "fired": True,
            "reason": "fired",
            "data": reminder_contract_view(fired),
            "reminder": reminder_view(fired),
            "delivery": _delivery_block(fired),
            "consent_at": format_iso(setting.consent_at),
            "next_job": next_job,
        }
    )
    return base


def _schedule_next_occurrence(
    session: Session,
    setting: Reminder,
    job: ScheduledJob,
    *,
    owner: User,
    moment: datetime,
) -> dict | None:
    """周期提醒：生成下一次到期作业（含新的未来授权，谱系记录上一次授权）。"""
    payload = dict(job.payload or {})
    period = payload.get("period")
    if period not in PERIODS or payload.get("repeat") is False:
        return None
    next_due = shift_period(job.due_at, period)
    next_key = f"reminder-job:{setting.reminder_id}:{format_iso(next_due)}"
    existing = session.execute(select(ScheduledJob).where(ScheduledJob.dedupe_key == next_key)).scalar_one_or_none()
    if existing is not None:
        return job_view(existing)

    ctx = AuthContext(
        user_id=owner.user_id,
        roles=tuple(owner.roles or ["customer"]),
        channel="scheduler",
        session_id="scheduler",
    )
    ttl_seconds = max(
        get_settings().grant_exact_ttl_seconds,
        int((next_due - moment).total_seconds()) + 3600,
    )
    grant = create_exact_grant(
        session,
        ctx,
        plan_id=None,
        plan_revision=int(payload.get("plan_revision", 1)),
        scope={
            "kind": "exact",
            "tool": "reminder.create",
            "reminder_id": setting.reminder_id,
            "dedupe_key": setting.dedupe_key,
            "channel": setting.channel,
            "due_at": format_iso(next_due),
            "purpose": "scheduled_reminder_fire",
            "recurrence": period,
        },
        required_level="YELLOW",
        ttl_seconds=ttl_seconds,
    )
    if job.grant_id:
        grant.lineage = [job.grant_id]
    fire_args = {
        "reminder_id": setting.reminder_id,
        "subscription_id": setting.subscription_id,
        "channel": setting.channel,
        "timezone": setting.timezone,
        "dedupe_key": setting.dedupe_key,
        "due_at": format_iso(next_due),
        "consent_at": format_iso(setting.consent_at),
    }
    next_job = create_job(
        session,
        ctx,
        job_type=JOB_TYPE,
        due_at=next_due,
        payload={**payload, "typed_args": fire_args, "due_at": format_iso(next_due)},
        task_id=job.task_id,
        grant_id=grant.grant_id,
        window_minutes=job.window_minutes or REMINDER_WINDOW_MINUTES,
        dedupe_key=next_key,
    )
    return job_view(next_job)


def _complete_job(session: Session, job: ScheduledJob) -> None:
    """按状态机把作业推进到 completed（leased 需先进入 running）。"""
    if job.state == "completed":
        return
    if job.state not in ("scheduled", "leased", "running", "awaiting_authorization"):
        return
    if job.state == "leased":
        assert_transition("job", job.state, "running")
        job.state = "running"
    assert_transition("job", job.state, "completed")
    mark_completed(session, job, note="reminder_fired")


def handle_reminder_job(session: Session, job: ScheduledJob, now: datetime | None = None):
    """调度器领域处理器（``JOB_HANDLERS["reminder.fire"]``）。

    返回 ``workers.scheduler`` 约定的结果：字符串或 ``{"outcome": ...}``，
    outcome ∈ completed / missed / failed。
    """
    result = fire_reminder(session, job, now=now)
    reason = result["reason"]
    if result["fired"] or reason == "already_fired":
        return {"outcome": "completed", "reason": reason, "reminder": result.get("reminder")}
    if reason == "out_of_window":
        return {"outcome": "missed", "reason": reason, "job_id": job.job_id}
    if reason in ("cancelled", "job_cancelled"):
        # 用户已取消：作业本身不再有效果，按无操作完成，避免把取消记录改写成失败。
        return {"outcome": "completed", "reason": reason, "no_op": True, "job_id": job.job_id}
    return {"outcome": "failed", "reason": reason, "job_id": job.job_id}


def run_due_reminders(session: Session, *, now: datetime | None = None, limit: int = 50) -> dict:
    """领域运行器：未接线调度器分派时，按到期/窗口/状态机契约执行提醒作业。

    与 ``workers.scheduler.run_due_jobs`` 使用同一批函数与同一窗口/幂等语义，
    因此两条路径都满足「重启后仍按调度执行且只生成一次」。
    """
    moment = to_utc(now) if now is not None else utcnow()
    jobs = session.execute(
        select(ScheduledJob)
        .where(
            ScheduledJob.job_type == JOB_TYPE,
            ScheduledJob.due_at <= moment,
            ScheduledJob.state.in_(("scheduled", "leased", "completed", "awaiting_authorization")),
        )
        .order_by(ScheduledJob.due_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    fired = 0
    already = 0
    cancelled = 0
    missed = 0
    failed = 0
    reminders: list[dict] = []
    for job in jobs:
        result = fire_reminder(session, job, now=moment)
        reason = result["reason"]
        if result["fired"]:
            fired += 1
            reminders.append(result["reminder"])
            _complete_job(session, job)
        elif reason == "already_fired":
            already += 1
            if result.get("reminder"):
                reminders.append(result["reminder"])
            _complete_job(session, job)
        elif reason in ("cancelled", "job_cancelled"):
            cancelled += 1
        elif reason == "out_of_window":
            missed += 1
            if job.state in ("scheduled", "leased"):
                mark_missed(session, job, note="reminder_out_of_window")
        else:
            failed += 1
    session.flush()
    return {
        "checked": len(jobs),
        "fired": fired,
        "already_fired": already,
        "cancelled": cancelled,
        "missed": missed,
        "failed": failed,
        "reminders": reminders,
        "observed_at": format_iso(moment),
    }


def pending_reminder_count(session: Session, owner_user_id: str, *, subscription_id: str | None = None) -> int:
    """仍处于 scheduled 的提醒设置数量（用于合同取消时如实展示未解决义务）。"""
    stmt = select(Reminder).where(Reminder.owner_user_id == owner_user_id, Reminder.status == "scheduled")
    if subscription_id:
        stmt = stmt.where(Reminder.subscription_id == subscription_id)
    return len(session.execute(stmt).scalars().all())


#: 供 workers.scheduler 的 job_type 分派点注册
JOB_HANDLERS = {JOB_TYPE: handle_reminder_job}