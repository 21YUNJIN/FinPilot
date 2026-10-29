"""风险测评问卷与风险等级（V5 第 11.1 节 S3-01、T09）。

这是**版本化的模拟规则**，不是任何银行的认证测评；问卷、评分与 R1—R5 映射
全部随 ``QUESTIONNAIRE_VERSION`` 冻结，历史测评结果按当时的版本解释。

维度（至少 6 维，各 1 道单选题）：

1. 投资经验 ``investment_experience``
2. 承受损失能力 ``loss_tolerance``
3. 风险态度 ``risk_attitude``
4. 投资期限 ``investment_horizon``
5. 流动性需求 ``liquidity_need``
6. 资金用途 ``capital_purpose``

评分：每题 0—4 分，合计 0—24 分；总分映射 R1—R5，再用"承受损失能力"给上限封顶
（说明文字会写清封顶原因）。默认有效期 1 年；过期后只允许一般信息查询。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...models import RiskProfile
from ...timeutil import format_iso, utcnow

QUESTIONNAIRE_VERSION = "2026.10"
VALIDITY_DAYS = 365
RISK_LEVELS: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5")
RISK_RANK: dict[str, int] = {level: idx + 1 for idx, level in enumerate(RISK_LEVELS)}
MAX_SCORE = 24

DISCLAIMER = (
    "本测评为本项目版本化的模拟规则，用于演示适当性约束，"
    "不代表任何银行的正式风险测评结论。"
)

DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("investment_experience", "投资经验"),
    ("loss_tolerance", "承受损失能力"),
    ("risk_attitude", "风险态度"),
    ("investment_horizon", "投资期限"),
    ("liquidity_need", "流动性需求"),
    ("capital_purpose", "资金用途"),
)
DIMENSION_LABELS: dict[str, str] = dict(DIMENSIONS)

#: 总分区间 → 风险等级（左闭右闭）。
SCORE_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 4, "R1"),
    (5, 9, "R2"),
    (10, 14, "R3"),
    (15, 19, "R4"),
    (20, 24, "R5"),
)

#: 承受损失能力得分 → 风险等级上限（能力不足时不允许被态度拉高）。
LOSS_TOLERANCE_CAP: dict[int, str] = {0: "R1", 1: "R2", 2: "R3", 3: "R4", 4: "R5"}

_LOSS_TOLERANCE_BAND: tuple[tuple[int, str], ...] = ((0, "low"), (1, "low"), (2, "medium"), (3, "medium"), (4, "high"))


def _question(
    dimension: str,
    text: str,
    options: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "question_id": f"q_{dimension}",
        "dimension": dimension,
        "dimension_label": DIMENSION_LABELS[dimension],
        "text": text,
        "options": [
            {"option_id": option_id, "label": label, "score": score}
            for score, (option_id, label) in enumerate(options)
        ],
    }


QUESTIONS: tuple[dict[str, Any], ...] = (
    _question(
        "investment_experience",
        "您过去主要接触过哪些投资品？",
        (
            ("exp_none", "没有投资经验，只使用过存款"),
            ("exp_deposit", "只买过货币基金或短期存款类产品"),
            ("exp_bond", "买过债券、债券基金或银行理财"),
            ("exp_mixed", "买过股票、混合基金或可转债"),
            ("exp_multi", "有 3 年以上股票、基金、衍生品等多类资产经验"),
        ),
    ),
    _question(
        "loss_tolerance",
        "如果投资出现浮亏，您最多能承受多大比例？",
        (
            ("loss_none", "不能接受任何本金波动"),
            ("loss_5", "最多 5%"),
            ("loss_10", "最多 10%"),
            ("loss_20", "最多 20%"),
            ("loss_20plus", "可以承受超过 20% 的波动"),
        ),
    ),
    _question(
        "risk_attitude",
        "您对风险与收益的态度更接近哪一种？",
        (
            ("att_none", "完全回避风险，收益低也可以"),
            ("att_low", "以保值为先，只接受很小的波动"),
            ("att_mid", "愿意承担一定波动换取稳健增值"),
            ("att_high", "为较高收益可以接受较大波动"),
            ("att_very_high", "追求高收益，能接受本金显著波动"),
        ),
    ),
    _question(
        "investment_horizon",
        "这笔资金预计可以投资多长时间？",
        (
            ("hz_1m", "1 个月以内"),
            ("hz_3m", "3 个月以内"),
            ("hz_1y", "1 年以内"),
            ("hz_3y", "1—3 年"),
            ("hz_3yplus", "3 年以上"),
        ),
    ),
    _question(
        "liquidity_need",
        "您在投资期内对资金流动性的需求是？",
        (
            ("lq_anytime", "随时可能需要全额用款"),
            ("lq_often", "经常需要部分取用"),
            ("lq_sometimes", "偶尔需要部分取用"),
            ("lq_rare", "很少需要提前取用"),
            ("lq_none", "投资期内不需要动用"),
        ),
    ),
    _question(
        "capital_purpose",
        "这笔资金的主要用途是？",
        (
            ("cp_daily", "日常生活备用金"),
            ("cp_near", "近期确定的大额支出（如购房、学费）"),
            ("cp_stable", "稳健增值，兼顾随时可用"),
            ("cp_long", "子女教育、养老等长期储备"),
            ("cp_growth", "长期资本增值，可承受波动"),
        ),
    ),
)

QUESTIONS_BY_ID: dict[str, dict[str, Any]] = {q["question_id"]: q for q in QUESTIONS}
_QUESTION_KEY_ALIASES: dict[str, str] = {}
for _q in QUESTIONS:
    _QUESTION_KEY_ALIASES[_q["question_id"]] = _q["question_id"]
    _QUESTION_KEY_ALIASES[_q["dimension"]] = _q["question_id"]


def questionnaire() -> dict[str, Any]:
    """返回版本化问卷（含每题选项与分值），供接口与前端共用。"""
    return {
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "validity_days": VALIDITY_DAYS,
        "max_score": MAX_SCORE,
        "dimensions": [{"dimension": key, "label": label} for key, label in DIMENSIONS],
        "questions": [dict(q) for q in QUESTIONS],
        "score_bands": [{"min": lo, "max": hi, "risk_level": level} for lo, hi, level in SCORE_BANDS],
        "loss_tolerance_cap": {
            str(score): level for score, level in LOSS_TOLERANCE_CAP.items()
        },
        "disclaimer": DISCLAIMER,
    }


def _band_level(score: int) -> str:
    for low, high, level in SCORE_BANDS:
        if low <= score <= high:
            return level
    return RISK_LEVELS[-1]


def _loss_band(score: int) -> str:
    for threshold, band in reversed(_LOSS_TOLERANCE_BAND):
        if score >= threshold:
            return band
    return "low"


def score_answers(answers: Mapping[str, Any]) -> dict[str, Any]:
    """按版本化规则评分：返回总分、风险等级、各维度得分与可读原因。"""
    if not isinstance(answers, Mapping):
        raise FinPilotError("PARAM_INVALID", "answers 必须是对象。")

    normalized: dict[str, str] = {}
    unknown: list[str] = []
    for raw_key, raw_value in answers.items():
        key = _QUESTION_KEY_ALIASES.get(str(raw_key).strip().lower()) or _QUESTION_KEY_ALIASES.get(str(raw_key).strip())
        if key is None:
            unknown.append(str(raw_key))
            continue
        normalized[key] = str(raw_value).strip()

    if unknown:
        raise FinPilotError(
            "PARAM_INVALID",
            "问卷包含未登记的题目。",
            details={"reason_codes": ["unknown_question"], "unknown": sorted(unknown)},
        )

    missing = [q["question_id"] for q in QUESTIONS if q["question_id"] not in normalized]
    if missing:
        raise FinPilotError(
            "PARAM_INVALID",
            "问卷未答完，无法评分。",
            details={"reason_codes": ["missing_answers"], "missing": missing},
        )

    dimension_scores: dict[str, Any] = {}
    total = 0
    reasons: list[str] = []
    for question in QUESTIONS:
        dimension = question["dimension"]
        option_id = normalized[question["question_id"]]
        option = next((o for o in question["options"] if o["option_id"] == option_id), None)
        if option is None:
            raise FinPilotError(
                "PARAM_INVALID",
                "选项不在该题允许范围内。",
                details={
                    "reason_codes": ["unknown_option"],
                    "question_id": question["question_id"],
                    "option_id": option_id,
                },
            )
        total += option["score"]
        dimension_scores[dimension] = {
            "dimension": dimension,
            "label": DIMENSION_LABELS[dimension],
            "question_id": question["question_id"],
            "option_id": option_id,
            "option_label": option["label"],
            "score": option["score"],
            "max_score": len(question["options"]) - 1,
        }
        reasons.append(
            f"{DIMENSION_LABELS[dimension]}：{option['label']}（{option['score']}/{len(question['options']) - 1} 分）"
        )

    raw_level = _band_level(total)
    loss_score = dimension_scores["loss_tolerance"]["score"]
    cap = LOSS_TOLERANCE_CAP[loss_score]
    risk_level = RISK_LEVELS[min(RISK_RANK[raw_level], RISK_RANK[cap]) - 1]
    caps_applied: list[str] = []
    if RISK_RANK[cap] < RISK_RANK[raw_level]:
        caps_applied.append(f"loss_tolerance_cap:{raw_level}->{risk_level}")
        reasons.append(
            f"承受损失能力上限为 {cap}（该项 {loss_score}/4 分），因此风险等级由 {raw_level} 下调为 {risk_level}。"
        )

    reasons.append(f"总分 {total}/{MAX_SCORE} 映射 {raw_level}，最终风险等级 {risk_level}。")

    return {
        "score": total,
        "max_score": MAX_SCORE,
        "raw_risk_level": raw_level,
        "risk_level": risk_level,
        "dimension_scores": dimension_scores,
        "reasons": reasons,
        "caps_applied": caps_applied,
        "max_loss_tolerance": _loss_band(loss_score),
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "disclaimer": DISCLAIMER,
    }


def submit_assessment(session: Session, ctx: Any, answers: Mapping[str, Any]) -> RiskProfile:
    """提交测评：写 RiskProfile（默认有效期 1 年），并把历史测评置为非活动。"""
    ctx.require_customer()
    result = score_answers(answers)
    now = utcnow()

    previous = list(
        session.execute(
            select(RiskProfile)
            .where(RiskProfile.owner_user_id == ctx.user_id, RiskProfile.is_active.is_(True))
            .order_by(RiskProfile.assessed_at.desc(), RiskProfile.profile_id.desc())
        ).scalars()
    )
    version = 1
    for profile in previous:
        profile.is_active = False
        version = max(version, profile.version + 1)

    profile = RiskProfile(
        owner_user_id=ctx.user_id,
        questionnaire_version=QUESTIONNAIRE_VERSION,
        answers={str(k): str(v) for k, v in answers.items()},
        score=result["score"],
        risk_level=result["risk_level"],
        max_loss_tolerance=result["max_loss_tolerance"],
        assessed_at=now,
        expires_at=now + timedelta(days=VALIDITY_DAYS),
        version=version,
        is_active=True,
    )
    session.add(profile)
    session.flush()
    return profile


def profile_view(profile: RiskProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "profile_id": profile.profile_id,
        "owner_user_id": profile.owner_user_id,
        "questionnaire_version": profile.questionnaire_version,
        "answers": dict(profile.answers or {}),
        "score": profile.score,
        "risk_level": profile.risk_level,
        "max_loss_tolerance": profile.max_loss_tolerance,
        "assessed_at": format_iso(profile.assessed_at),
        "expires_at": format_iso(profile.expires_at),
        "version": profile.version,
        "is_active": profile.is_active,
    }


def latest_profile(session: Session, user_id: str) -> RiskProfile | None:
    return session.execute(
        select(RiskProfile)
        .where(RiskProfile.owner_user_id == user_id)
        .order_by(RiskProfile.assessed_at.desc(), RiskProfile.version.desc(), RiskProfile.profile_id.desc())
    ).scalars().first()


def active_profile(session: Session, user_id: str) -> RiskProfile | None:
    """最新一条活动测评；没有活动记录时回退到最新一条（用于展示过期／被取代状态）。"""
    active = session.execute(
        select(RiskProfile)
        .where(RiskProfile.owner_user_id == user_id, RiskProfile.is_active.is_(True))
        .order_by(RiskProfile.assessed_at.desc(), RiskProfile.version.desc(), RiskProfile.profile_id.desc())
    ).scalars().first()
    return active or latest_profile(session, user_id)


def current_profile(session: Session, ctx: Any) -> dict[str, Any]:
    """当前测评状态：``valid`` / ``expired`` / ``inactive`` / ``none``。

    只有 ``status == "valid"`` 时 ``risk_level`` 才可用于适当性判定；
    过期测评仍会回显分数，但 ``usable`` 为 False，避免被下游当成有效。
    """
    ctx.require_customer()
    now = utcnow()
    profile = active_profile(session, ctx.user_id)
    if profile is None:
        return {
            "status": "none",
            "usable": False,
            "risk_level": None,
            "score": None,
            "max_loss_tolerance": None,
            "questionnaire_version": QUESTIONNAIRE_VERSION,
            "assessed_at": None,
            "expires_at": None,
            "days_remaining": None,
            "profile_id": None,
            "profile": None,
            "reason_codes": ["profile_not_found"],
            "disclaimer": DISCLAIMER,
        }

    expired = profile.expires_at <= now
    if expired:
        status = "expired"
    elif not profile.is_active:
        status = "inactive"
    else:
        status = "valid"

    usable = status == "valid"
    days_remaining = None
    if not expired:
        days_remaining = max(0, (profile.expires_at - now).days)

    reason_codes = {
        "valid": ["profile_valid"],
        "expired": ["profile_expired", "assessment_required"],
        "inactive": ["profile_superseded", "assessment_required"],
    }[status]

    return {
        "status": status,
        "usable": usable,
        "risk_level": profile.risk_level if usable else None,
        "score": profile.score,
        "max_loss_tolerance": profile.max_loss_tolerance if usable else None,
        "questionnaire_version": profile.questionnaire_version,
        "assessed_at": format_iso(profile.assessed_at),
        "expires_at": format_iso(profile.expires_at),
        "days_remaining": days_remaining,
        "profile_id": profile.profile_id,
        "profile": profile_view(profile),
        "reason_codes": reason_codes,
        "stale_note": None
        if usable
        else f"测评状态为 {status}；按 S3-01 只能做一般信息查询，申购前必须完成有效测评。",
        "disclaimer": DISCLAIMER,
    }


def profile_expires_at(assessed_at: datetime) -> datetime:
    return assessed_at + timedelta(days=VALIDITY_DAYS)


__all__ = [
    "DIMENSION_LABELS",
    "DIMENSIONS",
    "DISCLAIMER",
    "LOSS_TOLERANCE_CAP",
    "MAX_SCORE",
    "QUESTIONS",
    "QUESTIONNAIRE_VERSION",
    "RISK_LEVELS",
    "RISK_RANK",
    "SCORE_BANDS",
    "VALIDITY_DAYS",
    "active_profile",
    "current_profile",
    "latest_profile",
    "profile_expires_at",
    "profile_view",
    "questionnaire",
    "score_answers",
    "submit_assessment",
]