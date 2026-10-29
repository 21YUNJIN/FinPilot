"""订阅与代扣领域（V5 第 13 节 S5-01—S5-04）。

对外提供：

- ``service``：周期识别、合同查询（官方 YELLOW 例外）、商户合同取消、两端合并视图；
- ``mandates``：银行代扣授权的查询与解除；
- ``reminders``：续费提醒的同意记录、持久调度、取消与到点逻辑提醒。

三者共同导出写工具处理器（``TOOL_HANDLERS``）、只读处理器（``READ_HANDLERS``）与
调度领域处理器（``JOB_HANDLERS``），由网关、执行器与 ``workers.scheduler`` 消费。
"""

from .mandates import (
    MANDATE_STATUS_LABELS,
    list_mandates,
    mandate_contract_view,
    mandate_status_view,
    mandate_view,
    resolve_mandate,
    revoke_mandate,
)
from .reminders import (
    JOB_TYPE as REMINDER_JOB_TYPE,
    REMINDER_WINDOW_MINUTES,
    cancel_reminder,
    create_reminder,
    fire_reminder,
    handle_reminder_job,
    list_reminders,
    normalize_period,
    reminder_contract_view,
    reminder_view,
    run_due_reminders,
    shift_period,
)
from .service import (
    INFERRED_NOTE,
    JOB_HANDLERS,
    PERIOD_ALIASES,
    PERIOD_LABELS,
    READ_HANDLERS,
    SUBSCRIPTION_QUERY_EXCEPTION,
    TOOL_HANDLERS,
    cancel_subscription,
    cancel_subscription_and_mandate,
    detect_recurring,
    list_subscriptions,
    subscription_contract_view,
    subscription_view,
    subscriptions_view,
)

__all__ = [
    "INFERRED_NOTE",
    "JOB_HANDLERS",
    "MANDATE_STATUS_LABELS",
    "PERIOD_ALIASES",
    "PERIOD_LABELS",
    "READ_HANDLERS",
    "REMINDER_JOB_TYPE",
    "REMINDER_WINDOW_MINUTES",
    "SUBSCRIPTION_QUERY_EXCEPTION",
    "TOOL_HANDLERS",
    "cancel_reminder",
    "cancel_subscription",
    "cancel_subscription_and_mandate",
    "create_reminder",
    "detect_recurring",
    "fire_reminder",
    "handle_reminder_job",
    "list_mandates",
    "list_reminders",
    "list_subscriptions",
    "mandate_contract_view",
    "mandate_status_view",
    "mandate_view",
    "normalize_period",
    "reminder_contract_view",
    "reminder_view",
    "resolve_mandate",
    "revoke_mandate",
    "run_due_reminders",
    "shift_period",
    "subscription_contract_view",
    "subscription_view",
    "subscriptions_view",
]