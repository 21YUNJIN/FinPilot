"""适配器包：把外部系统隔在稳定业务键与查询语义之后。"""

from __future__ import annotations

from . import bank_local

__all__ = ["bank_local"]