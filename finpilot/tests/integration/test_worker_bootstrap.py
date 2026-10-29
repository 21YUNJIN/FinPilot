"""Worker 进程的提供方适配器注册回归（防止 async 通路再次整体失效）。

背景：Outbox / 调度器与 API 是不同进程，适配器注册表不共享。曾经只有 API 启动时注册，
Worker 投递时 ``get_adapter`` 抛 ``PROVIDER_CAPABILITY_MISSING``，表现为操作 failed、
任务停在 reconciling、``delivered=0`` 一直重试。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "services" / "api" / "src"


@pytest.fixture()
def clean_registry():
    from finpilot.kernel.provider_adapter import clear_adapters, register_adapter

    clear_adapters()
    yield
    # 让后续用例仍能拿到与 API 一致的注册状态
    clear_adapters()
    from finpilot.adapters import bank_local
    from finpilot.adapters.bank_http import HttpBankAdapter
    from finpilot.adapters.merchant.client import HttpMerchantAdapter, LocalMerchantAdapter

    for adapter in (bank_local.build_adapter(), HttpBankAdapter(), LocalMerchantAdapter(), HttpMerchantAdapter()):
        register_adapter(adapter)


def test_registry_registers_capable_adapters(clean_registry):
    from finpilot.adapters.registry import register_adapters
    from finpilot.kernel.provider_adapter import get_adapter, registered_adapters

    assert registered_adapters() == []

    status = register_adapters()

    assert status["bank_local"] == "registered"
    assert status["merchant_local"] == "registered"
    assert "bank_sandbox_v1" in status["registered"]
    assert "merchant_sandbox_v1" in status["registered"]
    # 真正会被 get_adapter 校验的能力位必须是 True，否则写入会被关闭
    for provider_id in ("bank_sandbox_v1", "merchant_sandbox_v1"):
        caps = get_adapter(provider_id).capabilities()
        assert caps["stable_key"] is True
        assert caps["query"] is True


def test_worker_entrypoint_registers_adapters_before_polling():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要 TEST_DATABASE_URL 指向一次性测试库")
    env = dict(os.environ)
    env.update(
        DATABASE_URL=database_url,
        PYTHONPATH=str(SRC),
        PYTHONUTF8="1",
        PROVIDER_MODE="http",
        APP_MODE="mock",
        QUANT_BACKEND="local_reference",
    )
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "worker_outbox.py"), "--once", "--log-level", "INFO"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, output
    assert "提供方适配器已注册" in output, output
    assert "bank_sandbox_v1" in output and "merchant_sandbox_v1" in output, output