"""网关依赖：会话解析、异常到信封的转换、审计落盘。"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import new_session
from ..envelope import error_envelope
from ..errors import FinPilotError
from ..kernel.identity import AuthContext, authenticate

logger = logging.getLogger(__name__)


def get_db() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
        session.commit()
    except FinPilotError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise FinPilotError("UNAUTHENTICATED", "缺少登录凭证。")
    return authorization.split(" ", 1)[1].strip()


def current_auth(
    token: str = Depends(bearer_token),
    session: Session = Depends(get_db),
) -> AuthContext:
    return authenticate(session, token)


async def finpilot_error_handler(request: Request, exc: FinPilotError) -> JSONResponse:
    payload = error_envelope(exc)
    return JSONResponse(status_code=exc.http_status or 500, content=payload)


def error_response(exc: FinPilotError, *, task_id: str | None = None, operation_id: str | None = None) -> dict:
    return error_envelope(exc, task_id=task_id, operation_id=operation_id)