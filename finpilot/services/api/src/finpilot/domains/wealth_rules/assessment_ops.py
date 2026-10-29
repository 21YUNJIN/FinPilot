"""风险测评工具处理器（V5 S3-01、T09）。

`RiskProfile` 没有 operation_id 列，因此幂等键用"问卷版本 + 答案规范化哈希"表达：
同一份答案重复提交只保留一条有效测评，不会因重复投递产生多条记录。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...ids import canonical_hash
from ...kernel.identity import AuthContext
from ...models import Operation, RiskProfile
from ...timeutil import utcnow
from . import assessment


def submit_answers(session: Session, ctx: AuthContext, answers: dict, version: str | None = None) -> dict:
    """按答案提交测评（工具处理器与 HTTP 问卷入口共用）。

    幂等键用"问卷版本 + 答案规范化哈希"表达：同一份答案重复提交只保留一条有效测评。
    """
    ctx.require_customer()
    if not isinstance(answers, dict) or not answers:
        raise FinPilotError("PARAM_INVALID", "缺少问卷答案（answers）。")

    version = str(version or assessment.QUESTIONNAIRE_VERSION)
    answers_hash = canonical_hash({"version": version, "answers": answers})

    existing = session.execute(
        select(RiskProfile).where(
            RiskProfile.owner_user_id == ctx.user_id,
            RiskProfile.questionnaire_version == version,
            RiskProfile.is_active.is_(True),
        )
    ).scalars().first()
    if existing is not None and canonical_hash({"version": version, "answers": existing.answers}) == answers_hash:
        return _result(existing, note="幂等：同一份答案重复提交")

    result = assessment.submit_assessment(session, ctx, answers)
    profile = session.execute(
        select(RiskProfile)
        .where(RiskProfile.owner_user_id == ctx.user_id, RiskProfile.is_active.is_(True))
        .order_by(RiskProfile.assessed_at.desc())
    ).scalars().first()
    if profile is None:
        raise FinPilotError("SYSTEM_ERROR", "风险测评结果未落库。")
    return _result(profile, note=result.get("note", "测评已完成") if isinstance(result, dict) else "测评已完成")


def submit_risk_assessment(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    args = operation.typed_args or {}
    return submit_answers(session, ctx, args.get("answers") or {}, args.get("questionnaire_version"))


def _result(profile: RiskProfile, *, note: str) -> dict:
    return {
        "type": "risk_profile",
        "domain": "wealth_rules",
        "result_type": "risk_assessment",
        "data": {
            "profile_id": profile.profile_id,
            "questionnaire_version": profile.questionnaire_version,
            "risk_level": profile.risk_level,
            "score": profile.score,
            "assessed_at": profile.assessed_at.isoformat(),
            "expires_at": profile.expires_at.isoformat(),
            "answers_hash": canonical_hash({"version": profile.questionnaire_version, "answers": profile.answers}),
        },
        "source": "deterministic_questionnaire",
        "version": profile.questionnaire_version,
        "warnings": [] if profile.expires_at > utcnow() else ["测评即将或已经过期，申购前需更新"],
        "note": note,
        "observed_at": utcnow().isoformat(),
    }


TOOL_HANDLERS = {"risk.assess": submit_risk_assessment}
READ_HANDLERS: dict = {}