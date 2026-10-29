"""运营接口：接管工单与模拟银行审批（独立角色，全部动作留审计）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..envelope import ok
from ..errors import FinPilotError
from ..kernel.identity import AuthContext
from ..orchestration.handoff import case_handoff, list_cases, operator_action
from ..security.audit import append_event
from .deps import current_auth, get_db

router = APIRouter(prefix="/operator", tags=["operator"])


class CaseActionBody(BaseModel):
    action: str
    payload: dict = {}


@router.get("/cases")
def get_cases(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db), status: str | None = None):
    return ok({"items": list_cases(session, ctx, status=status)})


@router.post("/cases/{case_id}/actions")
def act_on_case(
    case_id: str,
    body: CaseActionBody,
    ctx: AuthContext = Depends(current_auth),
    session: Session = Depends(get_db),
):
    result = operator_action(session, ctx, case_id=case_id, action=body.action, payload=body.payload)
    append_event(
        session,
        event_type="operator.action",
        actor=ctx.user_id,
        actor_role="operator",
        payload={"case_id": case_id, "action": body.action, "result": result},
    )
    return ok(result)


@router.get("/cases/{case_id}/handoff")
def get_handoff(case_id: str, ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from ..models import OperatorCase

    case = session.get(OperatorCase, case_id)
    if case is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该工单。")
    if not ctx.is_operator:
        raise FinPilotError("FORBIDDEN", "该操作需要独立运营角色。")
    return ok(case_handoff(session, ctx, task_id=case.task_id))


@router.get("/card-applications")
def pending_card_applications(ctx: AuthContext = Depends(current_auth), session: Session = Depends(get_db)):
    from sqlalchemy import select

    from ..models import CardApplication

    if not ctx.is_operator:
        raise FinPilotError("FORBIDDEN", "该操作需要独立运营角色。")
    rows = session.execute(
        select(CardApplication).where(CardApplication.application_status.in_(("submitted", "under_review")))
    ).scalars().all()
    return ok(
        {
            "items": [
                {
                    "application_id": row.application_id,
                    "owner_user_id": row.owner_user_id,
                    "kind": row.kind,
                    "requested_limit_minor": row.requested_limit_minor,
                    "application_status": row.application_status,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }
    )