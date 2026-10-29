"""AA 分摊领域（S1-04、T04/T27）。

- ``TOOL_HANDLERS['aa.create']``：执行器在 ``submit_operation`` 之后、同一事务内调用。
- ``create_aa_request``：按已确认实际支出用最大余数法分摊，合计严格等于总额；发起人自付份额单列。
- ``mark_share_paid``：参与人自己的转账成功后登记到账（不移动资金）。
- ``apply_refund_adjustment``：原消费退款 → 可追溯的分摊调整；已收款退款只记录待授权。
- ``aa_view``：总额、每人份额、已收／未收、调整记录与关联订单（``aa_state`` 为同一只读视图）。
- ``aa_contract_view``：投影为 ``contracts/schemas/aa_request.json`` 的合同对象。
"""

from __future__ import annotations

from .service import (
    AA_CANCELLED,
    AA_COLLECTING,
    AA_OPEN,
    AA_PARTIALLY_SETTLED,
    AA_SETTLED,
    COLLECT_ONLY_NOTICE,
    CONTRACT_KEYS,
    DOMAIN,
    DOMAIN_VERSION,
    SHARE_PAID,
    SHARE_PENDING,
    SOURCE,
    TERMS_VERSION,
    TOOL_HANDLERS,
    aa_contract_view,
    aa_state,
    aa_view,
    apply_refund_adjustment,
    create_aa_request,
    handle_aa_create,
    list_aa_requests,
    mark_share_paid,
    share_view,
)

__all__ = [
    "SOURCE",
    "DOMAIN",
    "DOMAIN_VERSION",
    "TERMS_VERSION",
    "COLLECT_ONLY_NOTICE",
    "TOOL_HANDLERS",
    "CONTRACT_KEYS",
    "SHARE_PENDING",
    "SHARE_PAID",
    "AA_OPEN",
    "AA_COLLECTING",
    "AA_PARTIALLY_SETTLED",
    "AA_SETTLED",
    "AA_CANCELLED",
    "create_aa_request",
    "handle_aa_create",
    "mark_share_paid",
    "apply_refund_adjustment",
    "aa_view",
    "aa_state",
    "aa_contract_view",
    "share_view",
    "list_aa_requests",
]