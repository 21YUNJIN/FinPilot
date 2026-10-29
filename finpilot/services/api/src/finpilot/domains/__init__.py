"""领域实现：卡片、AA 分摊等业务领域。

约定（与 docs/INTERFACES.md 第 2、5 节一致）：

- 领域只通过 ``kernel`` 提供的接口产生资金效果；``cards`` 与 ``aa`` 两个领域当前都不写账本
  （卡片只改申请与卡状态，AA 只登记应收与调整），其他领域的资金口径见各自模块说明。
- 每个写领域导出 ``TOOL_HANDLERS: dict[str, Callable[[Session, AuthContext, Operation], dict]]``，
  由编排执行器在 ``submit_operation`` 成功之后、同一事务内调用。
- 处理器必须幂等：先按 ``operation_id`` 查已存在结果，再决定是否新建业务记录，
  返回 ``DomainResult`` 结构 ``{"type", "data", "source", "version", "warnings"}``。
- 只读查询函数命名固定，供网关与 A 客户端直接调用（不 Consumer operation）。
"""

from __future__ import annotations

__all__ = ["cards", "aa"]