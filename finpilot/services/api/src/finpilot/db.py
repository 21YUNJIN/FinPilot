"""数据库连接、会话与锁工具（V5 第 8.2 节）。

- 目标数据库为 PostgreSQL；事务、行锁与并发语义按真实数据库实现。
- 固定锁顺序：账户按 account_id 升序加锁，日额度桶按业务日加锁，避免死锁。
- 序列化/唯一冲突由上层用同一 operation_id 重试，不能使用旧读数继续。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

RETRYABLE_SQLSTATES = {"40001", "40P01"}  # serialization_failure, deadlock_detected


def get_engine(database_url: str | None = None) -> Engine:
    global _engine, _session_factory
    if database_url is None and _engine is not None:
        return _engine
    settings = get_settings()
    url = database_url or settings.database_url
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        future=True,
        connect_args={"application_name": "finpilot-api"},
    )

    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - 驱动回调
        """统一会话默认：UTC 时区、锁等待上限、空闲事务上限。

        锁等待与空闲事务上限避免"某处忘记提交"把连接池与迁移/清理操作永久卡住；
        超时后调用方可按 is_retryable 判定并用同一 operation_id 重试。
        """
        settings_local = get_settings()
        with dbapi_connection.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(f"SET lock_timeout = '{int(settings_local.db_lock_timeout_ms)}ms'")
            cur.execute(
                f"SET idle_in_transaction_session_timeout = '{int(settings_local.db_idle_tx_timeout_ms)}ms'"
            )

    if database_url is None:
        _engine = engine
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def configure_engine(database_url: str) -> Engine:
    """显式配置全局引擎（测试与评测运行器使用）。"""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
    return get_engine(database_url)


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


def new_session() -> Session:
    return get_session_factory()()


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """事务作用域：正常提交，异常回滚。"""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, OperationalError) and getattr(exc, "orig", None) is not None:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        return sqlstate in RETRYABLE_SQLSTATES
    return False


# --- 锁工具 -------------------------------------------------------------


def lock_accounts(session: Session, account_ids: Sequence[str]) -> None:
    """按固定顺序锁定账户行，防止跨账户并发绕过保护组与余额校验。"""
    unique_sorted = sorted({a for a in account_ids if a})
    if not unique_sorted:
        return
    session.execute(
        text("SELECT account_id FROM accounts WHERE account_id = ANY(:ids) ORDER BY account_id FOR UPDATE"),
        {"ids": unique_sorted},
    )


def lock_user_row(session: Session, user_id: str) -> None:
    session.execute(text("SELECT user_id FROM users WHERE user_id = :uid FOR UPDATE"), {"uid": user_id})


def lock_grant(session: Session, grant_id: str) -> None:
    session.execute(text("SELECT grant_id FROM grants WHERE grant_id = :gid FOR UPDATE"), {"gid": grant_id})


ADVISORY_NAMESPACE = 0x46494E50  # "FINP"


def advisory_xact_lock(session: Session, key: str) -> None:
    """事务级 advisory lock：串行化同一逻辑键（日额度桶、同一用户写路径）。"""
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": ADVISORY_NAMESPACE, "key": key},
    )


def healthcheck() -> dict[str, Any]:
    with session_scope() as session:
        version = session.execute(text("SELECT version()")).scalar_one()
        return {"ok": True, "database": str(version).split(",")[0]}


__all__ = [
    "IntegrityError",
    "get_engine",
    "configure_engine",
    "get_session_factory",
    "new_session",
    "session_scope",
    "lock_accounts",
    "lock_user_row",
    "lock_grant",
    "advisory_xact_lock",
    "is_retryable",
    "healthcheck",
]