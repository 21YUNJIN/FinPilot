"""备份恢复演练（V5 NFR-02、T22、T55；DEL-02 运维脚本的可执行验收）。

演练链条（全部在**真实 PostgreSQL** 上执行，不使用 SQLite 替代）：

    建库 → Alembic 迁移 → 播种（data/synthetic/seed_app.json）
      → 跑一笔真实转账（内核记账 + Outbox 投递 + 审计追加）
      → 备份（pg_dump -Fc + 审计签名摘要 + 内容摘要 + 备份内密钥扫描）
      → 改动源库数据（再跑一笔转账）
      → 恢复到**另一个数据库**（pg_restore）
      → 校验：审计摘要一致 / 账本守恒 / 逐表内容摘要与备份时刻一致

跳过口径（如实执行，绝不伪造通过）
----------------------------------
* 本机没有 ``pg_dump``/``pg_restore``（且未设置 ``FINPILOT_PG_BIN``）→ 相关用例
  ``pytest.skip`` 并说明原因；
* ``TEST_DATABASE_URL`` 不是 PostgreSQL → 整文件 skip。本机实测：``pg_dump`` 与
  ``pg_restore`` 均为 PostgreSQL 17.6，演练真实跑通（见 README 与工作包报告）。

运行::

    $env:TEST_DATABASE_URL="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"
    $env:FINPILOT_PG_BIN="G:\\finpilot_tools\\pgsql\\bin"
    & ".\\venv\\Scripts\\python.exe" -m pytest tests/integration/test_backup_restore.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import backup_restore as br  # noqa: E402

from finpilot.kernel.grants import create_exact_grant  # noqa: E402
from finpilot.kernel.identity import AuthContext  # noqa: E402
from finpilot.kernel.submission import OperationDraft, prepare_operation, submit_operation  # noqa: E402
from finpilot.security.audit import append_event, verify_signed_digest  # noqa: E402
from finpilot.security.scope import build_scope  # noqa: E402
from finpilot.seed import DEFAULT_SEED_JSON, load_seed_file, seed_database  # noqa: E402
from finpilot.workers.outbox import drain  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.slow]

DRILL_USER = "user_demo_01"
DRILL_ACCOUNT = "account_salary"
DRILL_PAYEE = "payee_lisi"
DRILL_AMOUNT_MINOR = 30_000  # YELLOW 区间（<= 100000 分），且不触碰 800000 分应急金底线
MUTATION_AMOUNT_MINOR = 12_000


# --- 夹具 -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_db():
    """覆盖 conftest 的 PostgreSQL 自动夹具：本模块自行控制 schema 与数据库生命周期。

    注意**不覆盖** ``provider_adapters``：转账要经真实 Outbox 投递，必须有适配器。
    """
    yield


@pytest.fixture(scope="module", autouse=True)
def registered_provider_adapters():
    """模块级注册本地提供方适配器。

    conftest 的 ``provider_adapters`` 是**函数级**夹具，而本模块的 ``drill_state``
    是模块级——模块级夹具先于函数级夹具执行。若依赖 conftest 的夹具，演练转账会在
    "没有适配器"的状态下进入 Outbox，被如实判为 failed（本文件早期版本就是这样失败的）。
    因此这里显式按模块注册，保证演练跑在真实适配器上。
    """
    from finpilot.adapters import bank_local
    from finpilot.kernel.provider_adapter import clear_adapters, register_adapter

    clear_adapters()
    bank_local.reset()
    register_adapter(bank_local.build_adapter())
    yield
    bank_local.reset()


def _database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        from finpilot.config import get_settings

        url = get_settings().test_database_url
    return url


@pytest.fixture(scope="module")
def source_url() -> str:
    url = _database_url()
    if not url.startswith("postgresql"):
        pytest.skip(f"备份恢复演练需要真实 PostgreSQL，当前 TEST_DATABASE_URL={url!r}")
    return url


@pytest.fixture(scope="module")
def pg_bin() -> str | None:
    return os.environ.get("FINPILOT_PG_BIN")


@pytest.fixture(scope="module")
def pg_tools(pg_bin):
    """本机是否具备 pg_dump/pg_restore；不具备则显式 skip（并说明原因）。"""
    dump = br.find_pg_tool("pg_dump", pg_bin)
    restore = br.find_pg_tool("pg_restore", pg_bin)
    if dump is None or restore is None:
        pytest.skip(
            "本机未找到 pg_dump/pg_restore（可设 FINPILOT_PG_BIN 指向 PostgreSQL bin 目录）——"
            "备份恢复演练未执行，不得记为通过"
        )
    return {"pg_dump": dump, "pg_restore": restore}


@pytest.fixture(scope="module")
def restore_db_name(source_url) -> str:
    return f"{br.parse_database_url(source_url).dbname}_restore"


@pytest.fixture(scope="module")
def restore_url(source_url, restore_db_name, pg_tools) -> str:
    conn = br.parse_database_url(source_url)
    url = f"postgresql+psycopg://{conn.user}:{conn.password}@{conn.host}:{conn.port}/{restore_db_name}"
    admin_url = f"postgresql+psycopg://{conn.user}:{conn.password}@{conn.host}:{conn.port}/postgres"
    br.ensure_database_exists(admin_url, restore_db_name, owner=conn.user, drop_if_exists=True)
    yield url
    try:
        br.drop_database(admin_url, restore_db_name)
    except Exception as exc:  # pragma: no cover - 清理失败不应掩盖测试结论
        print(f"WARNING: 未能删除演练库 {restore_db_name}: {type(exc).__name__}: {exc}")


def _alembic_cfg(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.cmd_opts = Namespace(x=[f"db_url={url}"])
    return cfg


def _drop_schema(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO public"))


def _balances(url: str) -> dict[str, int]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT account_id, posted_balance_minor FROM accounts ORDER BY account_id")
            ).all()
        return {row.account_id: int(row.posted_balance_minor) for row in rows}
    finally:
        engine.dispose()


def _scalar(url: str, sql: str) -> object:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar_one()
    finally:
        engine.dispose()


def _operation_diagnostics(url: str, operation_id: str) -> dict:
    """转账失败时的归因信息（状态、错误码、Outbox 投递结果、状态迁移事件）。"""
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            operation = conn.execute(
                text(
                    "SELECT status, error_code, provider_id, provider_order_ref FROM operations "
                    "WHERE operation_id = :op"
                ),
                {"op": operation_id},
            ).one_or_none()
            outbox = conn.execute(
                text("SELECT event_type, delivery_state, attempts, last_error FROM outbox")
            ).all()
            events = conn.execute(
                text("SELECT from_status, to_status, reason FROM operation_events ORDER BY created_at")
            ).all()
        return {
            "operation": tuple(operation) if operation else None,
            "outbox": [tuple(row) for row in outbox],
            "operation_events": [tuple(row) for row in events],
        }
    finally:
        engine.dispose()


def _execute_transfer(url: str, *, operation_id: str, amount_minor: int) -> str:
    """跑一笔真实转账：prepare → 精确授权 → submit → Outbox 投递。"""
    engine = create_engine(url, future=True)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        ctx = AuthContext(user_id=DRILL_USER, roles=("customer",), channel="app", session_id="sess_drill")
        with factory() as session:
            draft = OperationDraft(
                tool="transfer.submit",
                typed_args={
                    "payee_id": DRILL_PAYEE,
                    "source_account_id": DRILL_ACCOUNT,
                    "amount_minor": amount_minor,
                    "fee_minor": 0,
                },
                operation_id=operation_id,
                task_id="task_drill",
                plan_id="plan_drill",
                plan_revision=1,
                source_account_id=DRILL_ACCOUNT,
                amount_minor=amount_minor,
                fee_minor=0,
            )
            operation = prepare_operation(session, ctx, draft)
            scope = build_scope(
                operation.tool,
                operation.typed_args,
                task_id=operation.task_id,
                plan_id=operation.plan_id,
                plan_revision=operation.plan_revision,
                parameters_hash=operation.parameters_hash,
                operation_id=operation.operation_id,
                required_level="YELLOW",
            )
            grant = create_exact_grant(
                session,
                ctx,
                plan_id=operation.plan_id,
                plan_revision=operation.plan_revision,
                scope=scope,
                required_level="YELLOW",
            )
            session.flush()
            submit_operation(session, ctx, operation_id=operation.operation_id, grant_id=grant.grant_id)
            session.commit()
            drain(session)
            session.commit()
            status = session.execute(
                text("SELECT status FROM operations WHERE operation_id = :op"), {"op": operation_id}
            ).scalar_one()
        assert status == "succeeded", (
            f"演练转账未成功：{operation_id} 状态 {status}；"
            f"归因={_operation_diagnostics(url, operation_id)}"
        )
        return str(status)
    finally:
        engine.dispose()


def _append_drill_audit_events(url: str) -> int:
    """追加审计事件（网关在真实请求里做的事），使审计摘要覆盖非空链。"""
    engine = create_engine(url, future=True)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            append_event(
                session,
                event_type="drill.transfer_submitted",
                actor=DRILL_USER,
                actor_role="customer",
                aggregate_id="op_drill_001",
                operation_id="op_drill_001",
                payload={
                    "tool": "transfer.submit",
                    "amount_minor": DRILL_AMOUNT_MINOR,
                    "source_account_id": DRILL_ACCOUNT,
                    "result": "succeeded",
                },
            )
            append_event(
                session,
                event_type="drill.backup_started",
                actor="ops_script",
                actor_role="operator",
                payload={"artifact": "finpilot-drill.dump", "excluded_table_data": ["sessions"]},
            )
            session.commit()
            return int(session.execute(text("SELECT count(*) FROM audit_events")).scalar_one())
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def drill_state(source_url):
    """模块级共享的演练初态：迁移 + 播种 + 一笔转账 + 两条审计事件。

    结束时把源库恢复为"已迁移到 head、无业务数据"的状态，便于后续 `finpilot.seed`
    在干净库上给出完整的装载计数。
    """
    engine = create_engine(source_url, future=True)
    _drop_schema(engine)
    command.upgrade(_alembic_cfg(source_url), "head")

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        counts = seed_database(session, load_seed_file(DEFAULT_SEED_JSON))

    _execute_transfer(source_url, operation_id="op_drill_001", amount_minor=DRILL_AMOUNT_MINOR)
    audit_rows = _append_drill_audit_events(source_url)

    state = {
        "seed_counts": counts,
        "balances_after_transfer": _balances(source_url),
        "ledger_tx_count": int(_scalar(source_url, "SELECT count(*) FROM ledger_transactions")),
        "ledger_entry_count": int(_scalar(source_url, "SELECT count(*) FROM ledger_entries")),
        "audit_events": audit_rows,
        "alembic_revision": str(_scalar(source_url, "SELECT version_num FROM alembic_version")),
    }
    yield state

    _drop_schema(engine)
    command.upgrade(_alembic_cfg(source_url), "head")
    engine.dispose()


# --- 用例 -----------------------------------------------------------------


def test_drill_preconditions_are_real(source_url, drill_state):
    """前置条件必须是真实的：种子装载、转账成功、账本与审计都有内容。"""
    assert drill_state["seed_counts"]["billing_transactions"] > 1000, "同步种子未装载"
    assert drill_state["ledger_tx_count"] >= 1, "转账没有产生账本事务"
    assert drill_state["ledger_entry_count"] >= 2, "双重记账至少两条分录"
    assert drill_state["audit_events"] >= 2, "审计链为空，摘要校验将失去意义"
    assert drill_state["alembic_revision"] == "0001"
    # 900000 - 30000 = 870000 分（手算期望）
    assert drill_state["balances_after_transfer"][DRILL_ACCOUNT] == 870_000
    assert drill_state["balances_after_transfer"]["account_clearing"] == 30_000


def test_backup_then_restore_into_new_database(source_url, restore_url, drill_state, tmp_path, pg_bin, pg_tools):
    """T22/T55 主演练：备份 → 改动数据 → 恢复到新库 → 三项校验全部通过。"""
    backup_dir = tmp_path / "backups"

    # 1) 备份（含审计摘要、内容摘要、密钥扫描）
    backup = br.perform_backup(
        source_url,
        backup_dir,
        pg_bin=pg_bin,
        label="drill",
        report_path=backup_dir / "backup-report.json",
    )
    assert backup["ok"] is True, backup.get("error")
    assert backup["steps"][0]["returncode"] == 0
    artifact = Path(backup["artifact"]["path"])
    assert artifact.is_file() and backup["artifact"]["bytes"] > 0
    assert backup["secret_scan"]["scanned"] is True, "密钥扫描必须真实执行（本机 pg_restore 可用）"
    assert backup["secret_scan"]["hits"] == [], "备份中出现了当前配置的密钥"
    assert backup["audit_digest"]["event_count"] == drill_state["audit_events"]
    digest_at_backup = backup["content_digest"]["overall"]

    # 2) 改动源库数据（再跑一笔转账），使源库与备份不再相同
    _execute_transfer(source_url, operation_id="op_drill_002", amount_minor=MUTATION_AMOUNT_MINOR)
    mutated = _balances(source_url)
    assert mutated[DRILL_ACCOUNT] == 870_000 - MUTATION_AMOUNT_MINOR, "改动未生效，演练无意义"
    assert mutated != drill_state["balances_after_transfer"]

    # 3) 恢复到**另一个数据库**（全新库，验证备份自包含）
    restore = br.perform_restore(
        restore_url,
        artifact,
        pg_bin=pg_bin,
        audit_digest_path=backup["audit_digest_path"],
        manifest_path=backup["manifest_path"],
        report_path=backup_dir / "restore-report.json",
    )
    assert restore["ok"] is True, restore.get("error")
    assert restore["checks"] == {
        "audit_digest": True,
        "ledger_conservation": True,
        "content_digest": True,
    }, restore["checks"]

    # 4) 恢复后的库 == 备份时刻的库（逐表摘要一致，无差异表）
    assert restore["content_digest"]["differing_tables"] == {}
    assert restore["content_digest"]["actual_overall"] == digest_at_backup

    # 5) 改动被回滚：恢复后的余额回到备份时刻
    restored_balances = _balances(restore_url)
    assert restored_balances == drill_state["balances_after_transfer"]
    assert _scalar(restore_url, "SELECT count(*) FROM ledger_transactions") == drill_state["ledger_tx_count"]
    assert _scalar(restore_url, "SELECT count(*) FROM operations") == 1
    assert _scalar(restore_url, "SELECT version_num FROM alembic_version") == "0001"

    # 6) 审计摘要与账本守恒（恢复后独立复核）
    assert restore["audit_verification"]["ok"] is True
    assert restore["audit_verification"]["chain_matches"] is True
    assert restore["ledger"]["conservation_check"]["ok"] is True
    assert restore["ledger"]["sql_invariants"]["ok"] is True

    # 7) 备份产物清单（供证据留档）
    sidecars = sorted(p.name for p in backup_dir.iterdir())
    assert any(name.endswith(".dump") for name in sidecars)
    assert any(name.endswith(".audit-digest.json") for name in sidecars)
    assert any(name.endswith(".manifest.json") for name in sidecars)


def test_backup_excludes_session_token_data(source_url, tmp_path, pg_bin, pg_tools):
    """备份内不得含凭证类数据：``sessions.token_hash`` 不进入归档（数据被排除）。"""
    token_hash = "deadbeef" * 8  # 64 位十六进制，与 token_hash 列宽一致
    engine = create_engine(source_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sessions (session_id, user_id, channel, token_hash, created_at, expires_at) "
                    "VALUES ('sess_backup_probe', :uid, 'app', :token, now(), now() + interval '2 hours')"
                ),
                {"uid": DRILL_USER, "token": token_hash},
            )
        backup = br.perform_backup(source_url, tmp_path / "excl", pg_bin=pg_bin, label="excl")
        assert backup["ok"] is True, backup.get("error")
        assert backup["excluded_table_data"] == ["sessions"]

        dump = Path(backup["artifact"]["path"])
        sql_text = subprocess.run(
            [str(pg_tools["pg_restore"]), "-f", "-", str(dump)],
            capture_output=True,
            env=br.parse_database_url(source_url).env(),
            timeout=300,
        )
        rendered = sql_text.stdout.decode("utf-8", errors="replace")
        assert "CREATE TABLE public.sessions" in rendered, "表结构应保留（只排除数据）"
        assert token_hash not in rendered, "会话令牌哈希出现在备份中：凭证类数据未被排除"
        # 对照：未排除的表的数据确实在归档里（证明上面不是"整体没数据"的假阴性）
        assert "COPY public.users" in rendered
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM sessions WHERE session_id = 'sess_backup_probe'"))
        engine.dispose()


def test_restored_audit_digest_detects_tampering(source_url, restore_url, drill_state, tmp_path, pg_bin, pg_tools):
    """反向对照：篡改恢复后的审计链，摘要校验必须报 CHAIN_CHANGED_SINCE_DIGEST。"""
    backup_dir = tmp_path / "tamper"
    backup = br.perform_backup(source_url, backup_dir, pg_bin=pg_bin, label="tamper")
    assert backup["ok"] is True, backup.get("error")
    restore = br.perform_restore(
        restore_url,
        backup["artifact"]["path"],
        pg_bin=pg_bin,
        audit_digest_path=backup["audit_digest_path"],
        manifest_path=backup["manifest_path"],
    )
    assert restore["audit_verification"]["ok"] is True, "前置条件：未篡改时应通过"

    engine = create_engine(restore_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_events WHERE seq = (SELECT max(seq) FROM audit_events)"))
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            result = verify_signed_digest(session, backup["audit_digest"])
        assert result["ok"] is False, "尾部删除后审计摘要仍通过：校验无效"
        assert "CHAIN_CHANGED_SINCE_DIGEST" in result["reason_codes"]
    finally:
        engine.dispose()


def test_backup_cli_path_produces_json_report(source_url, tmp_path, pg_bin, pg_tools):
    """运维文档里的 CLI 路径必须真的能跑（子进程执行，输出 JSON 报告）。"""
    out_dir = tmp_path / "cli"
    argv = [
        sys.executable,
        str(DEPLOY_DIR / "backup_restore.py"),
        "backup",
        "--database-url",
        source_url,
        "--output-dir",
        str(out_dir),
        "--label",
        "cli",
    ]
    if pg_bin:
        argv += ["--pg-bin", pg_bin]
    result = subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f"CLI 备份失败：\n{result.stdout}\n{result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["action"] == "backup" and payload["ok"] is True
    assert payload["report_path"] and Path(payload["report_path"]).is_file()