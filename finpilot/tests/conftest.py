"""共享测试夹具：真实 PostgreSQL、可注入时钟、标准种子。

约定：并发与事务语义测试必须跑真实 PostgreSQL（V5 第 8.2 节），
不得用 SQLite 替代并发保证。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "services" / "api" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 测试基线必须可复现：本机 .env（例如演示环境的 QUANT_BACKEND=remote、APP_MODE=live）
# 不得改变测试语义，否则单元测试会尝试联网请求量化服务而失败。
# 需要其它模式的用例在用例内用 monkeypatch.setenv 显式覆盖。
os.environ["APP_MODE"] = "mock"
os.environ["QUANT_BACKEND"] = "local_reference"

from finpilot import db as dbmod  # noqa: E402
from finpilot import models  # noqa: E402
from finpilot.config import get_settings  # noqa: E402
from finpilot.models import Base  # noqa: E402
from finpilot.timeutil import Clock, SHANGHAI, set_clock  # noqa: E402

REFERENCE_TIME = datetime(2026, 10, 22, 10, 0, 0, tzinfo=SHANGHAI)


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or get_settings().test_database_url


@pytest.fixture(scope="session")
def engine():
    url = _test_database_url()
    os.environ["DATABASE_URL"] = url
    eng = dbmod.configure_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def clean_db(engine):
    """每个测试从独立初态开始，避免案例互相污染余额与订单。

    设置 lock_timeout：并发测试进程若争用同一测试库，快速失败而不是无限等待。
    """
    table_names = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture(autouse=True)
def provider_adapters():
    """每个测试注册干净的本地提供方适配器，避免跨测试共享状态。

    生产装配由 finpilot.app.register_adapters 完成；测试必须走同一适配器协议。
    """
    from finpilot.adapters import bank_local
    from finpilot.kernel.provider_adapter import clear_adapters, register_adapter

    clear_adapters()
    bank_local.reset()
    register_adapter(bank_local.build_adapter())
    try:
        from finpilot.adapters.merchant.client import LocalMerchantAdapter

        register_adapter(LocalMerchantAdapter())
    except Exception:  # pragma: no cover - 商户适配器尚未装配
        pass
    yield
    bank_local.reset()


@pytest.fixture()
def clock() -> Clock:
    c = Clock(frozen_at=REFERENCE_TIME.astimezone(timezone.utc))
    set_clock(c)
    yield c
    set_clock(Clock())


@pytest.fixture()
def session(session_factory, clock) -> Session:
    s = session_factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def seed(session: Session) -> dict:
    """标准共享样例：user_demo_01 / account_salary / 同名联系人 / 9000 元余额。

    与 V5 第 4.3 节一致：账户余额 900000 分，应急金 800000 分，币种 CNY。
    演示用户带真实 scrypt 密码与已确认的 TOTP 因子（合成秘钥，仅测试环境）。
    """
    from finpilot.kernel.identity import encrypt_secret, hash_password

    password = "Passw0rd!demo"
    totp_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    now = REFERENCE_TIME.astimezone(timezone.utc)

    users = {}
    for uid, name, phone in (
        ("user_demo_01", "演示用户一", "13800000001"),
        ("user_demo_02", "演示用户二", "13800000002"),
        ("user_system", "清算系统账户", ""),
        ("user_operator_01", "模拟运营员", ""),
    ):
        roles = ["system"] if uid == "user_system" else (["operator"] if uid == "user_operator_01" else ["customer"])
        user = models.User(
            user_id=uid,
            status="active",
            display_name=name,
            phone_ref=phone,
            password_hash=hash_password(password) if roles != ["system"] else "",
            totp_secret_enc=encrypt_secret(totp_secret) if roles == ["customer"] else "",
            totp_confirmed_at=now if roles == ["customer"] else None,
            roles=roles,
            created_at=now,
        )
        session.add(user)
        users[uid] = user
    session.flush()

    accounts = {}
    for aid, owner, balance, name in (
        ("account_salary", "user_demo_01", 900_000, "工资卡"),
        ("account_savings", "user_demo_01", 1_500_000, "储蓄卡"),
        ("account_demo2", "user_demo_02", 500_000, "对方账户"),
        ("account_clearing", "user_system", 100_000_000, "清算账户"),
    ):
        acc = models.Account(
            account_id=aid,
            owner_user_id=owner,
            name=name,
            currency="CNY",
            posted_balance_minor=balance,
            created_at=now,
        )
        session.add(acc)
        accounts[aid] = acc

    session.add(
        models.ProtectionPolicy(
            policy_id="policy_demo",
            owner_user_id="user_demo_01",
            protected_floor_minor=800_000,
            account_scope=["account_salary"],
            currency="CNY",
            confirmed_at=now,
            updated_at=now,
        )
    )

    payees = {}
    for pid, name, phone, acct in (
        ("payee_zhangsan_1", "张三", "13900000001", "6222 **** 0001"),
        ("payee_zhangsan_2", "张三", "13900000002", "6222 **** 0002"),
        ("payee_lisi", "李四", "13900000003", "6222 **** 0003"),
    ):
        p = models.Payee(
            payee_id=pid,
            owner_user_id="user_demo_01",
            display_name=name,
            phone_ref=phone,
            destination_account_ref=acct,
            aliases=[],
            created_at=now,
        )
        session.add(p)
        payees[pid] = p

    session.commit()
    return {
        "users": users,
        "accounts": accounts,
        "payees": payees,
        "reference_time": REFERENCE_TIME,
        "user_id": "user_demo_01",
        "account_id": "account_salary",
        "password": password,
        "totp_secret": totp_secret,
    }


@pytest.fixture()
def ctx() -> "object":
    from finpilot.kernel.identity import AuthContext

    return AuthContext(user_id="user_demo_01", roles=("customer",), channel="app", session_id="sess_test")


@pytest.fixture()
def operator_ctx() -> "object":
    from finpilot.kernel.identity import AuthContext

    return AuthContext(user_id="user_operator_01", roles=("operator",), channel="operator", session_id="sess_op")


@pytest.fixture()
def client(api_client):
    return api_client


@pytest.fixture()
def api_client(seed):
    """FastAPI 测试客户端（应用装配完成后可用）。"""
    try:
        from fastapi.testclient import TestClient

        from finpilot.app import create_app
    except Exception as exc:  # pragma: no cover - 应用尚未装配时的显式跳过
        pytest.skip(f"应用未装配: {exc}")
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def frozen_tomorrow(clock) -> datetime:
    tomorrow = clock.now().astimezone(SHANGHAI) + timedelta(days=1)
    return tomorrow