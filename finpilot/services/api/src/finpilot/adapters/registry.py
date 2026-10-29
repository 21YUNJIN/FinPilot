"""提供方适配器注册表（API、常驻 Worker、容器入口共用同一份注册逻辑）。

为什么单独抽出来：``app.register_adapters`` 只在 API 进程启动时执行，而 Outbox 投递
和定时任务是在**另一个进程**里跑的。如果那个进程没有注册适配器，
``kernel.provider_adapter.get_adapter`` 会以 ``PROVIDER_CAPABILITY_MISSING`` 关闭写入，
表现为"任务一直 reconciling、outbox 一直 retried 但 delivered=0"。
"""

from __future__ import annotations

from typing import Any


def register_adapters() -> dict[str, Any]:
    """注册当前环境下可用的提供方适配器，返回状态供健康检查与 Worker 启动日志使用。

    单个适配器导入失败不会阻断其余适配器（依赖可选），但会如实记录 ``unavailable``。
    """
    from ..kernel.provider_adapter import register_adapter, registered_adapters

    status: dict[str, Any] = {}
    try:
        from . import bank_local

        register_adapter(bank_local.build_adapter())
        status["bank_local"] = "registered"
    except Exception as exc:  # pragma: no cover - 依赖可选
        status["bank_local"] = f"unavailable: {type(exc).__name__}"
    try:
        from .bank_http import HttpBankAdapter

        register_adapter(HttpBankAdapter())
        status["bank_http"] = "registered"
    except Exception as exc:  # pragma: no cover - 依赖可选
        status["bank_http"] = f"unavailable: {type(exc).__name__}"
    try:
        from .merchant.client import LocalMerchantAdapter

        register_adapter(LocalMerchantAdapter())
        status["merchant_local"] = "registered"
    except Exception as exc:  # pragma: no cover - 依赖可选
        status["merchant_local"] = f"unavailable: {type(exc).__name__}"
    try:
        from .merchant.client import HttpMerchantAdapter

        register_adapter(HttpMerchantAdapter())
        status["merchant_http"] = "registered"
    except Exception as exc:  # pragma: no cover - 依赖可选
        status["merchant_http"] = f"unavailable: {type(exc).__name__}"
    status["registered"] = registered_adapters()
    return status


def bootstrap_worker_adapters() -> dict[str, Any]:
    """Worker 启动时调用：注册适配器并返回状态（调用方负责写日志）。"""
    return register_adapters()