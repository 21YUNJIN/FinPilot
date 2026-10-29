"""Alembic 迁移运行环境（V5 DATA-04、NFR-02）。

连接来源优先级（高 → 低）
------------------------
1. 命令行覆盖：``alembic -x db_url=postgresql+psycopg://... upgrade head``
2. 进程环境变量 / ``.env``：``finpilot.config.get_settings().database_url``
   （即 ``DATABASE_URL``，默认 ``postgresql+psycopg://finpilot:...@127.0.0.1:55432/finpilot``）
3. 都没有时使用 :mod:`finpilot.config` 的默认值。

本文件**不读取** ``alembic.ini`` 的 ``sqlalchemy.url``：配置里不保存口令，
迁移链与运行应用永远使用同一份连接口径，避免"迁移建库 A、应用连库 B"。

其他约定
--------
* ``target_metadata = finpilot.models.Base.metadata``：迁移的唯一事实来源是
  ``services/api/src/finpilot/models.py``；任何表结构改动先改该文件再 autogenerate。
* ``compare_type=True``：列类型变化必须被 ``alembic check`` 检出。
* ``compare_server_default=False``：本项目对 ``server_default=func.now()`` 的
  渲染形式（``now()`` 与 ``CURRENT_TIMESTAMP``）随数据库版本反射差异较大，
  开启会持续产生假差异；该口径在 ``tests/integration/test_migrations.py`` 中
  由"迁移建表集合 == Base.metadata 表集合"与 ``alembic check`` 共同保证。
* 离线模式（``--sql``）同样可用，用于生成审计用的建库 SQL 文本。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# --- 让 migrations/ 在未安装 editable 包时也能导入 finpilot -----------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "services" / "api" / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from finpilot.config import get_settings  # noqa: E402
from finpilot.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: ``alembic check`` / autogenerate 的类型比较口径。
COMPARE_TYPE = True
#: 见模块 docstring：服务端默认值不参与比较（避免 ``now()`` 形式差异造成假差异）。
COMPARE_SERVER_DEFAULT = False
#: 迁移版本表名（与 Alembic 默认一致，显式写出便于运维检索）。
VERSION_TABLE = "alembic_version"


def _x_args() -> dict[str, str]:
    """``-x key=value`` 形式的命令行覆盖参数。"""
    return {str(k): str(v) for k, v in context.get_x_argument(as_dictionary=True).items()}


def resolve_database_url() -> str:
    """按模块 docstring 的优先级解析连接串。"""
    args = _x_args()
    url = args.get("db_url") or args.get("sqlalchemy_url")
    if url:
        return url
    # get_settings() 已经合并了环境变量与仓库根 .env
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "未解析到数据库连接串：请传 -x db_url=... 或设置 DATABASE_URL。"
        )
    return url


def _engine_kwargs() -> dict:
    """NullPool：迁移是一次性进程，不保留连接池（也避免与长跑工作进程抢连接）。"""
    return {"poolclass": pool.NullPool, "future": True}


def _configure_kwargs() -> dict:
    return {
        "target_metadata": target_metadata,
        "compare_type": COMPARE_TYPE,
        "compare_server_default": COMPARE_SERVER_DEFAULT,
        "version_table": VERSION_TABLE,
        "include_schemas": False,
    }


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 文本，不建立连接（``alembic upgrade head --sql``）。"""
    context.configure(
        url=resolve_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：建立真实连接并执行迁移。"""
    section = config.get_section(config.config_ini_section, {}) or {}
    section = dict(section)
    section["sqlalchemy.url"] = resolve_database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", **_engine_kwargs())

    with connectable.connect() as connection:
        context.configure(connection=connection, **_configure_kwargs())
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()