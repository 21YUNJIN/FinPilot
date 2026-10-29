"""理财规则领域（S3 理财操作与适当性）：测评、适当性、条款、比较。

对上层只暴露确定性接口，全部规则都带版本号，便于 T43 的"条款／版本变更后重跑"：

- ``assessment``：版本化问卷、评分、RiskProfile 有效期与当前状态。
- ``suitability.validate_domain``：申购前的硬约束校验（服务端权威）。
- ``terms``：产品条款、开放日、截止时间、到账与撤单、份额估算。
- ``compare.compare_products``：硬约束过滤 + 用户偏好排序（禁止收益保证措辞）。
- ``TOOL_HANDLERS``：按 ``domains/__init__.py`` 约定导出的领域处理器。

``wealth.compare`` 是 GREEN 只读工具，处理器天然幂等。``risk.assess``（写
``RiskProfile``）没有挂 ``operation_id`` 的列，做不到按操作幂等，因此**故意**
不放进 ``TOOL_HANDLERS``：调用方必须先用幂等键判定后再调用 ``submit_assessment``，
避免同一操作重复生成测评记录。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .assessment import (
    DISCLAIMER as ASSESSMENT_DISCLAIMER,
)
from .assessment import (
    QUESTIONNAIRE_VERSION,
    RISK_LEVELS,
    RISK_RANK,
    VALIDITY_DAYS,
    current_profile,
    profile_view,
    questionnaire,
    score_answers,
    submit_assessment,
)
from .compare import (
    COMPARE_VERSION,
    DEFAULT_PRIORITY,
    PREFERENCE_ALIASES,
    compare_products,
    ensure_no_guarantee_wording,
    normalize_preferences,
    rank_products,
)
from .suitability import VALIDATION_VERSION, validate_domain
from .terms import (
    TERMS_ENGINE_VERSION,
    can_cancel,
    estimate_shares,
    estimate_shares_range,
    expected_settlement_date,
    product_terms,
    settlement_explanation,
)

__all__ = [
    "ASSESSMENT_DISCLAIMER",
    "COMPARE_VERSION",
    "DEFAULT_PRIORITY",
    "PREFERENCE_ALIASES",
    "QUESTIONNAIRE_VERSION",
    "RISK_LEVELS",
    "RISK_RANK",
    "TERMS_ENGINE_VERSION",
    "TOOL_HANDLERS",
    "VALIDATION_VERSION",
    "VALIDITY_DAYS",
    "can_cancel",
    "compare_products",
    "current_profile",
    "ensure_no_guarantee_wording",
    "estimate_shares",
    "estimate_shares_range",
    "expected_settlement_date",
    "normalize_preferences",
    "product_terms",
    "profile_view",
    "questionnaire",
    "rank_products",
    "score_answers",
    "settlement_explanation",
    "submit_assessment",
    "validate_domain",
    "wealth_compare_handler",
]


def wealth_compare_handler(session: Any, ctx: Any, operation: Any) -> dict[str, Any]:
    """``wealth.compare`` 领域处理器：只读比较，无资金效果、天然幂等。"""
    if isinstance(operation, Mapping):
        nested = operation.get("typed_args")
        args: dict[str, Any] = dict(nested) if isinstance(nested, Mapping) else {}
        args.update({key: value for key, value in operation.items() if key != "typed_args"})
    else:
        args = dict(getattr(operation, "typed_args", None) or {})
    products = compare_products(
        session,
        ctx,
        budget_minor=args.get("budget_minor"),
        horizon_days=args.get("horizon_days"),
        preferences=args.get("preferences"),
    )
    eligible = [item for item in products if item["eligible"]]
    excluded = [item for item in products if not item["eligible"]]
    return {
        "type": "wealth_compare",
        "data": {
            "products": products,
            "eligible_count": len(eligible),
            "excluded_count": len(excluded),
            "excluded_reason_codes": {
                item["product_id"]: item["exclusion_reason_codes"] for item in excluded
            },
        },
        "source": "wealth_rules",
        "version": COMPARE_VERSION,
        "warnings": [
            {
                "code": reason["code"],
                "message": reason["detail"],
                "product_id": item["product_id"],
            }
            for item in excluded
            for reason in item["reasons"]
            if reason["code"].startswith("EXCLUDED_")
        ],
    }


#: 领域处理器注册表（只含只读、天然幂等的工具）
TOOL_HANDLERS = {"wealth.compare": wealth_compare_handler}