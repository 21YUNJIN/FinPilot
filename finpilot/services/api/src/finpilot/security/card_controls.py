"""卡控叠加规则的对外引用点（V5 S4-03、T13）。

实现落在领域模块 `finpilot.domains.cards.service`（与卡业务同一处，避免两套规则），
本模块只做转发，保证 INTERFACES 与 module-manifest 中声明的导入路径可用：

    from finpilot.security.card_controls import effective_controls, can_release
"""

from __future__ import annotations

from typing import Any

__all__ = ["effective_controls", "can_release", "block_reasons"]


def effective_controls(controls: list[Any]) -> dict:
    """叠加后的限制视图：用户锁、渠道锁、风控冻结与限额可同时存在，互不清除。"""
    from ..domains.cards.service import effective_controls as _impl

    return _impl(controls)


def can_release(control: Any, actor_role: str) -> bool:
    """只有有权解除的主体才能解除：风控冻结仅运营／风控角色，其余为客户本人。"""
    from ..domains.cards.service import can_release as _impl

    return _impl(control, actor_role)


def block_reasons(card: Any, *, channel: str = "online", amount_minor: int = 0) -> list[str]:
    """返回阻止交易的卡控原因码（与策略层同一实现）。"""
    from ..security.policy import card_control_block_reason

    class _Session:
        pass

    raise NotImplementedError("请通过 security.policy.card_control_block_reason(session, card, ...) 获取")