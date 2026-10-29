"""运行配置。

所有阈值都是本项目可追溯的工程阈值（V5 第 6、8、17、18 节），不是赛事官方评分线。
配置文件不包含真实生产密钥；示例值仅用于本地竞赛演示环境。
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数据库
    database_url: str = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot"
    test_database_url: str = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_lock_timeout_ms: int = 20_000
    db_idle_tx_timeout_ms: int = 60_000

    # 运行模式
    app_mode: str = "mock"
    environment: str = "local"

    # Bank wealth risk: remote is the integrated runtime; local_reference is an explicit baseline.
    quant_backend: str = "remote"
    stockquant_service_url: str = "http://127.0.0.1:8770"
    stockquant_service_token: str = ""
    stockquant_timeout_seconds: float = 3.0

    # 模型适配器
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    model_temperature: float = 0.0
    model_max_retries: int = 2
    model_timeout_seconds: int = 30

    # 会话与验证
    session_ttl_minutes: int = 120
    totp_encryption_key: str = "dev_only_totp_key_change_me_0123456789abcdef"
    audit_signing_key: str = "dev_only_audit_signing_key_change_me"
    provider_callback_secret: str = "dev_only_provider_callback_secret"

    # 业务阈值
    transfer_daily_yellow_limit_minor: int = 100_000
    challenge_ttl_seconds: int = 120
    challenge_max_attempts: int = 5
    challenge_lock_seconds: int = 900
    grant_exact_ttl_seconds: int = 300
    reservation_idle_ttl_seconds: int = 900
    schedule_window_minutes: int = 10
    planner_max_nodes: int = 24
    planner_max_candidates: int = 128
    planner_solve_timeout_ms: int = 2000
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_cooldown_seconds: int = 60
    safety_lock_refusal_threshold: int = 5
    safety_lock_window_seconds: int = 60

    # 沙箱
    sandbox_enabled: bool = True
    sandbox_cpu_seconds: int = 5
    sandbox_wall_seconds: int = 10
    sandbox_memory_mb: int = 512
    sandbox_output_bytes: int = 1_048_576
    sandbox_max_processes: int = 32

    # 留存
    audit_retention_days: int = 90
    chat_retention_days: int = 30

    # 独立提供方沙箱
    provider_mode: str = "local"
    bank_sandbox_base_url: str = "http://127.0.0.1:58101"
    merchant_sandbox_base_url: str = "http://127.0.0.1:58102"
    bank_sandbox_state: str = str(REPO_ROOT / ".state/bank_sandbox.sqlite3")
    merchant_sandbox_state: str = str(REPO_ROOT / ".state/merchant_sandbox.sqlite3")
    provider_callback_base_url: str = "http://127.0.0.1:58000"

    @property
    def is_live_model(self) -> bool:
        return self.app_mode == "live" and bool(self.model_base_url)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """测试或脚本在修改环境变量后重新读取配置。"""
    get_settings.cache_clear()
    return get_settings()


def env_override(name: str, value: str | None) -> None:
    """显式覆盖环境变量（评测运行器与测试夹具使用）。"""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    reload_settings()
