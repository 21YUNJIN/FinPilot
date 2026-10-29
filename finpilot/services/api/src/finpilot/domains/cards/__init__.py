"""卡片领域（S4-01/02/04、T12/T13/T17/T18）。

申请状态与卡生命周期分离：``card.apply`` / ``card.set_limit`` / ``card.replace`` 只登记
``CardApplication``；只有运营角色 ``approve_application`` 批准时才创建有效卡或写入批准额度。

导出：
- ``TOOL_HANDLERS``：``{tool: Callable[[Session, AuthContext, Operation], dict]}``，执行器在
  ``submit_operation`` 成功之后、同一事务内调用，返回 DomainResult。
- ``JOB_HANDLERS``：调度器注册点（卡片当前无定时作业，显式登记空表）。
- 只读：``list_cards`` / ``card_status`` / ``list_applications``。
- 运营动作：``approve_application`` / ``auto_review`` / ``start_review``。
- 卡控：``effective_controls`` / ``can_release`` / ``control_block_reasons``。
- 合同对象视图：``card_view`` / ``application_view`` / ``control_view``。
"""

from __future__ import annotations

from .service import (
    APPLICATION_TRANSITIONS,
    CONTROL_TYPES,
    DOMAIN,
    DOMAIN_VERSION,
    JOB_HANDLERS,
    SIMULATED_DECISION_LABEL,
    SIMULATED_DECISION_VERSION,
    SIMULATED_MAX_APPROVED_LIMIT_MINOR,
    SOURCE,
    TERMS_VERSION,
    TOOL_HANDLERS,
    application_view,
    apply_card,
    approve_application,
    assert_application_transition,
    assert_card_transition,
    auto_review,
    can_release,
    card_blocked_reasons,
    card_status,
    card_view,
    control_block_reasons,
    control_view,
    effective_controls,
    list_applications,
    list_cards,
    masked_pan_for_test_card,
    replace_card,
    report_lost,
    request_limit_change,
    set_card_control,
    simulated_approval_rule,
    simulated_decision,
    start_review,
    withdraw_application,
)

__all__ = [
    "SOURCE",
    "DOMAIN",
    "DOMAIN_VERSION",
    "TERMS_VERSION",
    "TOOL_HANDLERS",
    "JOB_HANDLERS",
    "SIMULATED_DECISION_LABEL",
    "SIMULATED_DECISION_VERSION",
    "SIMULATED_MAX_APPROVED_LIMIT_MINOR",
    "APPLICATION_TRANSITIONS",
    "CONTROL_TYPES",
    "simulated_approval_rule",
    "simulated_decision",
    "apply_card",
    "request_limit_change",
    "set_card_control",
    "report_lost",
    "replace_card",
    "start_review",
    "auto_review",
    "approve_application",
    "withdraw_application",
    "list_cards",
    "card_status",
    "list_applications",
    "effective_controls",
    "can_release",
    "control_block_reasons",
    "card_view",
    "card_blocked_reasons",
    "application_view",
    "control_view",
    "masked_pan_for_test_card",
    "assert_application_transition",
    "assert_card_transition",
]