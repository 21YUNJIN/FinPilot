"""会话与强验证入口。

人类确认/验证端点与模型工具目录严格分离：这些路径不在 agent 可调用集合内。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..envelope import ok
from ..models import User
from ..security.audit import append_event
from ..timeutil import format_iso, utcnow
from .deps import current_auth, get_db

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=1, max_length=128)
    channel: str = Field(default="app", pattern="^(app|im|operator)$")


class TotpConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@router.post("/sessions")
def create_session_endpoint(body: LoginRequest, session: Session = Depends(get_db)):
    from ..kernel.identity import create_session

    token, ctx = create_session(session, body.user_id, body.password, body.channel)
    user = session.get(User, ctx.user_id)
    try:
        append_event(
            session,
            event_type="auth.session_created",
            actor=ctx.user_id,
            actor_role="customer",
            payload={"channel": body.channel, "session_id": ctx.session_id},
        )
    except Exception:  # pragma: no cover - 审计不可用不阻断登录
        pass
    return ok(
        {
            "token": token,
            "session_id": ctx.session_id,
            "channel": ctx.channel,
            "user": {
                "user_id": ctx.user_id,
                "display_name": user.display_name if user else "",
                "roles": list(ctx.roles),
                "has_strong_factor": bool(user and user.totp_confirmed_at),
            },
        },
        adapter="finpilot-api",
    )


@router.get("/me")
def me(ctx=Depends(current_auth), session: Session = Depends(get_db)):
    user = session.get(User, ctx.user_id)
    return ok(
        {
            "user_id": ctx.user_id,
            "display_name": user.display_name if user else "",
            "roles": list(ctx.roles),
            "channel": ctx.channel,
            "has_strong_factor": bool(user and user.totp_confirmed_at),
            "strong_factor_confirmed_at": format_iso(user.totp_confirmed_at) if user and user.totp_confirmed_at else None,
        }
    )


@router.post("/totp/enroll")
def enroll(ctx=Depends(current_auth), session: Session = Depends(get_db)):
    from ..kernel.identity import enroll_totp

    data = enroll_totp(session, ctx)
    # 秘钥只在注册流程返回一次，用于写入独立验证器；不进入对话、日志与模型上下文
    return ok(data, extra_provenance={"sensitive": "secret_returned_once"})


@router.post("/totp/confirm")
def confirm(body: TotpConfirmRequest, ctx=Depends(current_auth), session: Session = Depends(get_db)):
    from ..kernel.identity import confirm_totp

    verified = confirm_totp(session, ctx, body.code)
    if not verified:
        from ..errors import FinPilotError

        raise FinPilotError("CHALLENGE_FAILED", "验证码不正确。")
    return ok({"confirmed": True, "confirmed_at": format_iso(utcnow())})


@router.post("/logout")
def logout(ctx=Depends(current_auth), session: Session = Depends(get_db)):
    from ..kernel.identity import revoke_session

    revoke_session(session, ctx.session_id)
    return ok({"logged_out": True, "session_id": ctx.session_id})