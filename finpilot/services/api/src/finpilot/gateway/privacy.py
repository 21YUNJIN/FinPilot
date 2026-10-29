"""隐私：最小发送、导出、删除与留存（V5 17.3、SEC-04、NFR-04）。

删除只清理聊天与可清理的任务数据，不删除已发生的金融事实与审计；
审计保留最小必要去标识摘要，绝不用删除审计掩盖已发生业务。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..envelope import ok
from ..errors import FinPilotError
from ..models import DataDeletionRequest, Operation, Task, TaskMessage
from ..security.audit import append_event
from ..security.trust_boundary import sanitize_for_model
from ..timeutil import format_iso, utcnow
from ..kernel.identity import AuthContext
from .deps import current_auth, get_db

router = APIRouter(prefix="/v1/privacy", tags=["privacy"])


class DeleteBody(BaseModel):
    scope: str = "chat"


@router.get("/retention")
def retention(ctx: AuthContext = Depends(current_auth)):
    settings = get_settings()
    return ok(
        {
            "chat_retention_days": settings.chat_retention_days,
            "audit_retention_days": settings.audit_retention_days,
            "notes": [
                "竞赛环境配置，不构成银行法定留存声明",
                "模型只接收任务所需的脱敏信息与对象代理 ID",
                "密码、OTP、TOTP 秘钥、完整卡号与真实证件不会发送给模型",
            ],
            "mode": settings.app_mode,
        }
    )


@router.get("/export")
def export(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    tasks = session.execute(select(Task).where(Task.owner_user_id == ctx.user_id)).scalars().all()
    messages = session.execute(
        select(TaskMessage).where(TaskMessage.task_id.in_([t.task_id for t in tasks]))
    ).scalars().all() if tasks else []
    operations = session.execute(select(Operation).where(Operation.owner_user_id == ctx.user_id)).scalars().all()

    raw = {
        "user_id": ctx.user_id,
        "exported_at": format_iso(utcnow()),
        "tasks": [
            {
                "task_id": t.task_id,
                "status": t.status,
                "revision": t.revision,
                "goal_summary": (t.goal_spec or {}).get("summary"),
                "created_at": format_iso(t.created_at),
            }
            for t in tasks
        ],
        "messages": [
            {"task_id": m.task_id, "role": m.role, "content": m.content, "created_at": format_iso(m.created_at)}
            for m in messages
        ],
        "operations": [
            {
                "operation_id": o.operation_id,
                "tool": o.tool,
                "status": o.status,
                "amount_minor": o.amount_minor,
                "created_at": format_iso(o.created_at),
            }
            for o in operations
        ],
    }
    sanitized, summary = sanitize_for_model(raw, allowed_fields=set(raw.keys()) | {"messages", "tasks", "operations"})
    # 这是返回给用户本人的导出：保留其自身 user_id，其余按最小发送口径脱敏
    sanitized["user_id"] = ctx.user_id
    append_event(
        session,
        event_type="privacy.export",
        actor=ctx.user_id,
        actor_role="customer",
        payload={"tasks": len(tasks), "operations": len(operations), "redaction": summary},
    )
    return ok({"export": sanitized, "redaction_summary": summary})


@router.post("/delete")
def delete_data(body: DeleteBody, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    if body.scope not in ("chat", "chat_and_closed_tasks"):
        raise FinPilotError("PARAM_INVALID", "不支持的删除范围。")
    tasks = session.execute(select(Task).where(Task.owner_user_id == ctx.user_id)).scalars().all()
    task_ids = [t.task_id for t in tasks]
    deleted_messages = 0
    if task_ids:
        result = session.execute(delete(TaskMessage).where(TaskMessage.task_id.in_(task_ids), TaskMessage.role.in_(("user", "assistant"))))
        deleted_messages = result.rowcount or 0

    deleted_tasks = 0
    if body.scope == "chat_and_closed_tasks":
        closed = [t for t in tasks if t.status in ("succeeded", "cancelled", "failed")]
        for task in closed:
            task.goal_spec = {"redacted": True, "summary": "用户请求删除的任务内容"}
            deleted_tasks += 1

    request = DataDeletionRequest(
        owner_user_id=ctx.user_id,
        scope=body.scope,
        status="completed",
        deleted_counts={"messages": deleted_messages, "task_goals_redacted": deleted_tasks},
        requested_at=utcnow(),
        completed_at=utcnow(),
    )
    session.add(request)
    append_event(
        session,
        event_type="privacy.delete",
        actor=ctx.user_id,
        actor_role="customer",
        payload={"scope": body.scope, "deleted": request.deleted_counts},
    )
    return ok(
        {
            "request_id": request.request_id,
            "deleted": request.deleted_counts,
            "note": "已发生的金融事实与审计记录按最小必要保留，不随聊天删除而消失",
        }
    )


@router.get("/deletion-requests")
def deletion_requests(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    rows = session.execute(
        select(DataDeletionRequest).where(DataDeletionRequest.owner_user_id == ctx.user_id)
    ).scalars().all()
    return ok(
        {
            "items": [
                {
                    "request_id": row.request_id,
                    "scope": row.scope,
                    "status": row.status,
                    "deleted": row.deleted_counts,
                    "requested_at": format_iso(row.requested_at),
                }
                for row in rows
            ]
        }
    )