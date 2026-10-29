"""备份与恢复演练工具（V5 NFR-02、T22、T55；DEL-02 运维脚本）。

做三件事，且每一件都留下可核对的证据（JSON 报告）：

1. **备份前**：用 ``finpilot.security.audit.signed_digest`` 记录审计链的"链头 + 事件数 +
   整链内容摘要 + HMAC 签名"，写到**备份文件之外**的 sidecar 文件。
   这样"整链重写"与"尾部删除"在恢复后可被检测（链内自检看不到尾部截断）。
2. **备份内不得含密钥**：默认 ``--exclude-table-data=sessions``（会话令牌哈希是凭证类数据，
   且恢复旧会话本身也不安全），并用 ``pg_restore -f -`` 把归档展开成 SQL 文本，
   扫描当前配置里的签名密钥/回调密钥/TOTP 加密密钥是否**逐字出现**在备份里——
   出现即判定备份不合格（fail-closed），而不是只写一句"应该没有密钥"。
3. **恢复后校验**：``verify_signed_digest``（审计摘要一致）+ ``conservation_check``
   （账本守恒：每笔事务分录之和为 0、账户余额与分录累计一致）+ 逐表内容摘要比对
   （备份时刻 vs 恢复后，逐表 md5）。

连接串处理
----------
接受 SQLAlchemy 形式的 URL（``postgresql+psycopg://user:pw@host:port/db``）；口令只通过
子进程环境变量 ``PGPASSWORD`` 传递，**绝不进入命令行参数**，报告里一律脱敏为 ``***``。

已知边界（如实披露）
--------------------
* 本工具是 ``pg_dump`` / ``pg_restore`` 的封装，不做增量/连续归档（PITR）；逻辑备份的
  一致性依赖 PostgreSQL 自身快照语义（本工具默认使用 ``-Fc`` 自定义格式 + 单事务恢复）。
* 内容摘要用 ``row_to_json(t)::text``（在 UTC 会话下渲染），因此**跨大版本** PostgreSQL
  恢复后若文本渲染变化会报差异；同版本演练（本机 17.6）无此问题。
* 密钥扫描是"当前配置的密钥是否出现在备份里"的定向检查，不是通用的密钥泄露扫描器；
  数据库里存的是 TOTP 密文的**密文**（``totp_secret_enc``），明文密钥不在库内。
* ``--exclude-table-data=sessions`` 使恢复后的库**没有**在线会话行：这是有意的
  （避免恢复出仍然有效的登录态），恢复后用户需要重新登录。

用法（仓库根目录，Windows PowerShell）::

    $env:PGPASSWORD="finpilot_dev_pw"
    & .\\venv\\Scripts\\python.exe deploy\\backup_restore.py check-tools --pg-bin G:\\finpilot_tools\\pgsql\\bin
    & .\\venv\\Scripts\\python.exe deploy\\backup_restore.py backup `
        --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy" `
        --output-dir backups --pg-bin G:\\finpilot_tools\\pgsql\\bin
    & .\\venv\\Scripts\\python.exe deploy\\backup_restore.py restore `
        --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy_restore" `
        --input backups\\finpilot-<ts>.dump `
        --audit-digest backups\\finpilot-<ts>.audit-digest.json `
        --manifest backups\\finpilot-<ts>.manifest.json `
        --pg-bin G:\\finpilot_tools\\pgsql\\bin
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "services" / "api" / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# 通用退出码（便于 CI / verify_install.ps1 判定）
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VERIFY_FAILED = 2
EXIT_TOOL_MISSING = 3
EXIT_COMMAND_FAILED = 4

#: 默认排除**数据**（保留表结构）的表：会话令牌哈希属于凭证类数据。
DEFAULT_EXCLUDE_TABLE_DATA: tuple[str, ...] = ("sessions",)

#: 密钥扫描的大小上限：超过则跳过并如实标注（不做静默截断）
SECRET_SCAN_MAX_BYTES = 256 * 1024 * 1024

#: Windows 本机便携版 PostgreSQL 的常见位置（可用 --pg-bin / FINPILOT_PG_BIN 覆盖）
_FALLBACK_PG_BINS: tuple[str, ...] = (
    "G:/finpilot_tools/pgsql/bin",
    "C:/Program Files/PostgreSQL/17/bin",
)


# ---------------------------------------------------------------------------
# 连接串解析与脱敏
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PgConn:
    """从 SQLAlchemy URL 解析出的 libpq 连接参数。"""

    host: str
    port: int
    user: str
    password: str
    dbname: str
    raw_url: str

    def redacted(self) -> str:
        scheme, _, rest = self.raw_url.partition("://")
        if "@" in rest:
            creds, _, tail = rest.rpartition("@")
            user = creds.split(":", 1)[0]
            rest = f"{user}:***@{tail}"
        return f"{scheme}://{rest}"

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """构造传给 pg_dump/pg_restore 的环境（口令只在这里出现，不进 argv）。"""
        env = dict(base or os.environ)
        env["PGHOST"] = self.host
        env["PGPORT"] = str(self.port)
        env["PGUSER"] = self.user
        env["PGDATABASE"] = self.dbname
        if self.password:
            env["PGPASSWORD"] = self.password
        env.setdefault("PGCLIENTENCODING", "UTF8")
        return env


def parse_database_url(url: str) -> PgConn:
    parts = urlsplit(url)
    if parts.scheme.split("+")[0] not in {"postgresql", "postgres"}:
        raise ValueError(f"仅支持 PostgreSQL URL，收到 scheme={parts.scheme!r}")
    if not parts.path or parts.path == "/":
        raise ValueError("URL 缺少数据库名")
    return PgConn(
        host=parts.hostname or "127.0.0.1",
        port=int(parts.port or 5432),
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        dbname=unquote(parts.path.lstrip("/")),
        raw_url=url,
    )


def _sqlalchemy_url(url: str) -> str:
    """脚本内部连库时统一走 psycopg3 驱动。"""
    scheme, _, rest = url.partition("://")
    if "+" not in scheme:
        return f"postgresql+psycopg://{rest}"
    return url


def _engine(url: str):
    from sqlalchemy import create_engine

    engine = create_engine(_sqlalchemy_url(url), pool_pre_ping=True, future=True)
    return engine


# ---------------------------------------------------------------------------
# pg 工具定位
# ---------------------------------------------------------------------------


def find_pg_tool(name: str, pg_bin: str | None = None) -> Path | None:
    """按 显式参数 → FINPILOT_PG_BIN → PATH → 常见安装位置 的顺序定位 pg 工具。"""
    exe = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = []
    if pg_bin:
        candidates.append(Path(pg_bin) / exe)
    env_bin = os.environ.get("FINPILOT_PG_BIN")
    if env_bin:
        candidates.append(Path(env_bin) / exe)
    which = shutil.which(name)
    if which:
        candidates.append(Path(which))
    for fallback in _FALLBACK_PG_BINS:
        candidates.append(Path(fallback) / exe)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def tool_version(path: Path) -> str:
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=60)
    except Exception as exc:  # pragma: no cover - 工具不可执行
        return f"unavailable: {type(exc).__name__}"
    return (result.stdout or result.stderr).strip() or f"returncode={result.returncode}"


def run_step(argv: Sequence[str], env: dict[str, str], *, timeout: int = 3600) -> dict[str, Any]:
    """执行外部命令并记录可核对的元数据（argv 已由调用方脱敏）。"""
    started = time.time()
    result = subprocess.run(list(argv), capture_output=True, text=True, env=env, timeout=timeout)
    return {
        "argv": [str(a) for a in argv],
        "returncode": result.returncode,
        "seconds": round(time.time() - started, 3),
        "stdout_tail": (result.stdout or "")[-2000:],
        "stderr_tail": (result.stderr or "")[-2000:],
    }


# ---------------------------------------------------------------------------
# 数据库侧校验
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def database_content_digest(url: str, tables: Iterable[str] | None = None) -> dict[str, Any]:
    """逐表内容摘要（md5），用于证明"恢复后数据与备份时刻一致"。

    摘要口径：按主键排序（无主键时按行文本排序）拼接 ``row_to_json(t)::text``；
    会话统一 ``SET TIME ZONE 'UTC'``，避免时区设置造成的文本渲染差异。
    """
    from sqlalchemy import inspect, text

    engine = _engine(url)
    digests: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    warnings: list[str] = []
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SET TIME ZONE 'UTC'")
            inspector = inspect(conn)
            names = sorted(tables) if tables is not None else sorted(
                inspector.get_table_names(schema="public")
            )
            for table in names:
                pk = inspector.get_pk_constraint(table, schema="public").get("constrained_columns") or []
                if pk:
                    order_by = ", ".join(f'"{col}"' for col in pk)
                else:
                    order_by = "row_to_json(t)::text"
                    warnings.append(f"{table}: 无主键，改用行文本排序")
                sql = (
                    "SELECT count(*), md5(coalesce(string_agg(row_to_json(t)::text, chr(10) "
                    f"ORDER BY {order_by}), '')) FROM public.\"{table}\" t"
                )
                count, digest = conn.execute(text(sql)).one()
                row_counts[table] = int(count)
                digests[table] = str(digest)
    finally:
        engine.dispose()

    overall = hashlib.sha256(
        json.dumps(digests, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "tables": digests,
        "row_counts": row_counts,
        "table_count": len(digests),
        "overall": overall,
        "warnings": warnings,
    }


def audit_signed_digest(url: str) -> dict[str, Any]:
    """读取审计链签名摘要；库中没有 ``audit_events`` 表时如实返回错误。"""
    from sqlalchemy.orm import sessionmaker

    from finpilot.security.audit import signed_digest

    engine = _engine(url)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            return dict(signed_digest(session))
    finally:
        engine.dispose()


def verify_audit_digest(url: str, digest: dict[str, Any]) -> dict[str, Any]:
    """恢复后比对审计摘要（检测整链重写与尾部删除）。"""
    from sqlalchemy.orm import sessionmaker

    from finpilot.security.audit import verify_signed_digest

    engine = _engine(url)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            return dict(verify_signed_digest(session, digest))
    finally:
        engine.dispose()


def ledger_conservation_check(url: str) -> dict[str, Any]:
    """账本守恒自检（``kernel.ledger.conservation_check``）。

    说明：该函数的"账户余额 = 初始余额 + 分录累计"部分只对进程内已登记的
    ``INITIAL_BALANCES`` 生效；脚本另行补充一条与进程状态无关的 SQL 校验
    （见 :func:`ledger_sql_invariants`），两者一起报告，避免只报一个"ok"。
    """
    from sqlalchemy.orm import sessionmaker

    from finpilot.kernel.ledger import conservation_check

    engine = _engine(url)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as session:
            return dict(conservation_check(session))
    finally:
        engine.dispose()


def ledger_sql_invariants(url: str) -> dict[str, Any]:
    """与进程状态无关的账本 SQL 校验（恢复后独立复核）。"""
    from sqlalchemy import text

    engine = _engine(url)
    try:
        with engine.connect() as conn:
            unbalanced = conn.execute(
                text(
                    "SELECT ledger_tx_id, sum(signed_amount_minor) AS total FROM ledger_entries "
                    "GROUP BY ledger_tx_id HAVING sum(signed_amount_minor) <> 0"
                )
            ).all()
            totals = conn.execute(
                text(
                    "SELECT account_id, sum(signed_amount_minor) AS total FROM ledger_entries "
                    "GROUP BY account_id"
                )
            ).all()
            accounts = conn.execute(
                text("SELECT account_id, posted_balance_minor FROM accounts")
            ).all()
        entry_totals = {row.account_id: int(row.total) for row in totals}
        return {
            "ok": not unbalanced,
            "unbalanced_transactions": [
                {"ledger_tx_id": row.ledger_tx_id, "total": int(row.total)} for row in unbalanced
            ],
            "accounts_with_entries": len(entry_totals),
            "accounts": len(accounts),
            "note": (
                "账户余额差额 = 初始余额（种子/开户写入，不产生分录），"
                "因此这里只校验「每笔事务分录之和为 0」与账户集合；"
                "余额级校验见 kernel.ledger.conservation_check（需要调用方登记初始余额）"
            ),
        }
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------


def _secret_values() -> dict[str, str]:
    """当前配置中需要确认"不进入备份"的密钥值（只用于扫描，不写入任何输出）。"""
    from finpilot.config import get_settings

    settings = get_settings()
    candidates = {
        "audit_signing_key": settings.audit_signing_key,
        "provider_callback_secret": settings.provider_callback_secret,
        "totp_encryption_key": settings.totp_encryption_key,
        "model_api_key": settings.model_api_key,
    }
    return {name: value for name, value in candidates.items() if value and len(value) >= 8}


def scan_backup_for_secrets(
    pg_restore: Path, conn: PgConn, dump_path: Path, secrets: dict[str, str]
) -> dict[str, Any]:
    """把归档展开成 SQL 文本，逐字扫描当前密钥是否出现在备份中。"""
    if not secrets:
        return {"scanned": False, "reason": "没有长度 >= 8 的已配置密钥可扫描", "hits": []}
    size = dump_path.stat().st_size
    if size > SECRET_SCAN_MAX_BYTES:
        return {
            "scanned": False,
            "reason": f"归档 {size} 字节超过扫描上限 {SECRET_SCAN_MAX_BYTES}，未扫描（如实标注，不静默跳过）",
            "hits": [],
        }
    result = subprocess.run(
        [str(pg_restore), "-f", "-", str(dump_path)],
        capture_output=True,
        env=conn.env(),
        timeout=1800,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    hits = [
        {"key_name": name, "occurrences": text.count(value)}
        for name, value in secrets.items()
        if value in text
    ]
    return {
        "scanned": True,
        "bytes_scanned": len(result.stdout),
        "keys_checked": sorted(secrets),
        "hits": hits,
        "pg_restore_returncode": result.returncode,
    }


def perform_backup(
    database_url: str,
    output_dir: str | Path,
    *,
    pg_bin: str | None = None,
    exclude_table_data: Sequence[str] = DEFAULT_EXCLUDE_TABLE_DATA,
    label: str | None = None,
    digest_out: str | Path | None = None,
    scan_secrets: bool = True,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """执行一次逻辑备份并输出 JSON 报告（含审计摘要、内容摘要、密钥扫描）。"""
    started = time.time()
    started_at = _utc_now_iso()
    conn = parse_database_url(database_url)
    report: dict[str, Any] = {
        "action": "backup",
        "ok": False,
        "started_at": started_at,
        "database": {
            "url": conn.redacted(),
            "host": conn.host,
            "port": conn.port,
            "dbname": conn.dbname,
            "user": conn.user,
        },
        "excluded_table_data": list(exclude_table_data),
        "steps": [],
        "warnings": [],
    }

    pg_dump = find_pg_tool("pg_dump", pg_bin)
    pg_restore = find_pg_tool("pg_restore", pg_bin)
    if pg_dump is None:
        report["error"] = "找不到 pg_dump；请用 --pg-bin 或 FINPILOT_PG_BIN 指定 PostgreSQL bin 目录"
        report["exit_code"] = EXIT_TOOL_MISSING
        return report
    report["pg_tools"] = {
        "pg_dump": str(pg_dump),
        "pg_dump_version": tool_version(pg_dump),
        "pg_restore": str(pg_restore) if pg_restore else None,
    }

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{label}-" if label else ""
    dump_path = out_dir / f"finpilot-{tag}{stamp}.dump"

    # 1) 备份前的审计链签名摘要（写在备份文件之外，才具备"尾部删除可检测"的意义）
    try:
        digest = audit_signed_digest(database_url)
        report["audit_digest"] = digest
    except Exception as exc:
        report["audit_digest"] = {"error": f"{type(exc).__name__}: {exc}"}
        report["warnings"].append("审计摘要读取失败：备份仍会生成，但恢复后无法核验审计链一致性")
        digest = None

    # 2) 备份前的内容摘要（备份时刻的数据指纹）
    try:
        report["content_digest"] = database_content_digest(database_url)
    except Exception as exc:
        report["content_digest"] = {"error": f"{type(exc).__name__}: {exc}"}
        report["warnings"].append("内容摘要计算失败（表结构可能不完整）")

    # 3) pg_dump -Fc
    argv = [
        str(pg_dump),
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--verbose",
        f"--file={dump_path}",
    ]
    for table in exclude_table_data:
        argv.append(f"--exclude-table-data=public.{table}")
    argv.append(conn.dbname)
    step = run_step(argv, conn.env())
    report["steps"].append({"name": "pg_dump", **step})
    if step["returncode"] != 0:
        report["error"] = f"pg_dump 失败（returncode={step['returncode']}）"
        report["exit_code"] = EXIT_COMMAND_FAILED
        _write_report(report, report_path, out_dir, stamp, tag, started)
        return report

    artifact = {
        "path": str(dump_path),
        "bytes": dump_path.stat().st_size,
        "sha256": _sha256_file(dump_path),
    }
    report["artifact"] = artifact

    # 4) 密钥扫描：备份里不得出现当前配置的密钥
    if scan_secrets and pg_restore is not None:
        scan = scan_backup_for_secrets(pg_restore, conn, dump_path, _secret_values())
        report["secret_scan"] = scan
        if scan.get("hits"):
            report["error"] = "备份中出现当前配置的密钥明文，判定备份不合格（fail-closed）"
            report["ok"] = False
            report["exit_code"] = EXIT_VERIFY_FAILED
            _write_report(report, report_path, out_dir, stamp, tag, started)
            return report
    else:
        report["secret_scan"] = {
            "scanned": False,
            "reason": "pg_restore 不可用或 --no-secret-scan",
            "hits": [],
        }

    # 5) sidecar：审计摘要 + 内容摘要 + 归档指纹（供恢复后比对）
    sidecar = {
        "generated_at": _utc_now_iso(),
        "database": report["database"],
        "artifact": artifact,
        "excluded_table_data": list(exclude_table_data),
        "audit_digest": report.get("audit_digest"),
        "content_digest": report.get("content_digest"),
        "note": "审计摘要必须保存到备份介质之外的独立位置，才可检测整链重写与尾部删除",
    }
    audit_path = Path(digest_out) if digest_out else dump_path.with_suffix(".audit-digest.json")
    manifest_path = dump_path.with_suffix(".manifest.json")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    report["audit_digest_path"] = str(audit_path)
    report["manifest_path"] = str(manifest_path)
    if digest is None:
        report["audit_digest"] = {"error": "unavailable"}

    report["ok"] = True
    report["exit_code"] = EXIT_OK
    _write_report(report, report_path, out_dir, stamp, tag, started)
    return report


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(
    report: dict[str, Any],
    report_path: str | Path | None,
    out_dir: Path,
    stamp: str,
    tag: str,
    started: float,
) -> None:
    report["finished_at"] = _utc_now_iso()
    report["duration_seconds"] = round(time.time() - started, 3)
    target = Path(report_path) if report_path else out_dir / f"finpilot-{tag}{stamp}.report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(target)


# ---------------------------------------------------------------------------
# 恢复 + 校验
# ---------------------------------------------------------------------------


def _admin_engine(url: str):
    """维护库连接：CREATE/DROP DATABASE 必须在自动提交模式下执行。"""
    from sqlalchemy import create_engine

    return create_engine(_sqlalchemy_url(url), isolation_level="AUTOCOMMIT", future=True)


def ensure_database_exists(
    admin_url: str,
    target_dbname: str,
    *,
    owner: str | None = None,
    drop_if_exists: bool = False,
) -> dict[str, Any]:
    """在维护库上创建目标数据库（供"恢复到一个全新库"演练使用）。"""
    from sqlalchemy import text

    engine = _admin_engine(admin_url)
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target_dbname}
            ).scalar_one_or_none()
            actions: list[str] = []
            if exists and drop_if_exists:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{target_dbname}" WITH (FORCE)'))
                actions.append("dropped")
                exists = None
            if not exists:
                clause = f' OWNER "{owner}"' if owner else ""
                conn.execute(text(f'CREATE DATABASE "{target_dbname}"{clause}'))
                actions.append("created")
            return {"dbname": target_dbname, "actions": actions, "exists": True}
    finally:
        engine.dispose()


def drop_database(admin_url: str, target_dbname: str) -> dict[str, Any]:
    from sqlalchemy import text

    engine = _admin_engine(admin_url)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target_dbname}" WITH (FORCE)'))
        return {"dbname": target_dbname, "dropped": True}
    finally:
        engine.dispose()


def perform_restore(
    database_url: str,
    dump_path: str | Path,
    *,
    pg_bin: str | None = None,
    audit_digest_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    clean: bool = True,
    no_owner: bool = True,
    single_transaction: bool = True,
    jobs: int = 1,
    expect_digest_match: bool = True,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """恢复归档并完成三项校验：审计摘要、账本守恒、逐表内容摘要一致。"""
    started = time.time()
    conn = parse_database_url(database_url)
    dump = Path(dump_path)
    report: dict[str, Any] = {
        "action": "restore",
        "ok": False,
        "started_at": _utc_now_iso(),
        "database": {
            "url": conn.redacted(),
            "host": conn.host,
            "port": conn.port,
            "dbname": conn.dbname,
            "user": conn.user,
        },
        "input": {"path": str(dump)},
        "steps": [],
        "warnings": [],
    }

    if not dump.is_file():
        report["error"] = f"归档不存在：{dump}"
        report["exit_code"] = EXIT_USAGE
        return report
    report["input"]["bytes"] = dump.stat().st_size
    report["input"]["sha256"] = _sha256_file(dump)

    pg_restore = find_pg_tool("pg_restore", pg_bin)
    if pg_restore is None:
        report["error"] = "找不到 pg_restore；请用 --pg-bin 或 FINPILOT_PG_BIN 指定 PostgreSQL bin 目录"
        report["exit_code"] = EXIT_TOOL_MISSING
        return report
    report["pg_tools"] = {"pg_restore": str(pg_restore), "pg_restore_version": tool_version(pg_restore)}

    sidecar: dict[str, Any] | None = None
    sidecar_path = Path(manifest_path) if manifest_path else None
    if sidecar_path is None:
        candidate = dump.with_suffix(".manifest.json")
        sidecar_path = candidate if candidate.is_file() else None
    if sidecar_path and sidecar_path.is_file():
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        report["manifest"] = {"path": str(sidecar_path)}
        expected_artifact = (sidecar.get("artifact") or {}).get("sha256")
        if expected_artifact and expected_artifact != report["input"]["sha256"]:
            report["warnings"].append(
                "归档 sha256 与 manifest 不一致：备份文件可能在传输中被改动"
            )
    if audit_digest_path is not None:
        audit_path = Path(audit_digest_path)
    else:
        candidate = dump.with_suffix(".audit-digest.json")
        audit_path = candidate if candidate.is_file() else None
    audit_sidecar = None
    if audit_path and Path(audit_path).is_file():
        audit_sidecar = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        report["audit_digest_source"] = str(audit_path)
    elif sidecar is not None:
        audit_sidecar = sidecar

    argv = [str(pg_restore), "--dbname", conn.dbname]
    if clean:
        argv += ["--clean", "--if-exists"]
    if no_owner:
        argv += ["--no-owner"]
    argv += ["--no-privileges"]
    if single_transaction:
        argv += ["--single-transaction", "--exit-on-error"]
    if jobs > 1:
        argv += ["--jobs", str(jobs)]
    argv.append(str(dump))
    step = run_step(argv, conn.env())
    report["steps"].append({"name": "pg_restore", **step})
    if step["returncode"] != 0:
        report["error"] = f"pg_restore 失败（returncode={step['returncode']}）"
        report["exit_code"] = EXIT_COMMAND_FAILED
        _write_report(report, report_path, dump.parent, _stamp(), "", started)
        return report

    # --- 校验 1：审计摘要一致 -------------------------------------------
    expected_audit = (audit_sidecar or {}).get("audit_digest") if audit_sidecar else None
    if isinstance(expected_audit, dict) and "signature" in expected_audit:
        try:
            report["audit_verification"] = verify_audit_digest(database_url, expected_audit)
        except Exception as exc:
            report["audit_verification"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        report["audit_verification"] = {
            "ok": False,
            "reason": "未提供可用的审计摘要 sidecar（缺少 signature 字段）",
        }
        report["warnings"].append("未核验审计摘要：请传 --audit-digest 或保留 <dump>.audit-digest.json")

    # --- 校验 2：账本守恒 ------------------------------------------------
    try:
        conservation = ledger_conservation_check(database_url)
    except Exception as exc:
        conservation = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        sql_invariants = ledger_sql_invariants(database_url)
    except Exception as exc:
        sql_invariants = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    report["ledger"] = {"conservation_check": conservation, "sql_invariants": sql_invariants}

    # --- 校验 3：逐表内容摘要（备份时刻 vs 恢复后） -----------------------
    expected_content = (audit_sidecar or {}).get("content_digest") if audit_sidecar else None
    actual_content = database_content_digest(database_url)
    report["content_digest"] = {"actual_overall": actual_content["overall"], "tables": actual_content["table_count"]}
    if isinstance(expected_content, dict) and expected_content.get("tables"):
        expected_tables: dict[str, str] = expected_content["tables"]
        actual_tables: dict[str, str] = actual_content["tables"]
        excluded = set((audit_sidecar or {}).get("excluded_table_data") or [])
        differing = {
            table: {"expected": expected_tables.get(table), "actual": actual_tables.get(table)}
            for table in sorted(set(expected_tables) | set(actual_tables))
            if expected_tables.get(table) != actual_tables.get(table)
        }
        report["content_digest"]["expected_overall"] = expected_content.get("overall")
        report["content_digest"]["excluded_table_data"] = sorted(excluded)
        report["content_digest"]["differing_tables"] = differing
        report["content_digest"]["match"] = not differing
    else:
        report["content_digest"]["match"] = None
        report["warnings"].append("未比对内容摘要：manifest 中缺少 content_digest")

    audit_ok = bool(report["audit_verification"].get("ok"))
    ledger_ok = bool(conservation.get("ok")) and bool(sql_invariants.get("ok"))
    content_ok = report["content_digest"].get("match") is True
    report["checks"] = {"audit_digest": audit_ok, "ledger_conservation": ledger_ok, "content_digest": content_ok}
    if expect_digest_match:
        report["ok"] = audit_ok and ledger_ok and content_ok
    else:
        report["ok"] = ledger_ok
    report["exit_code"] = EXIT_OK if report["ok"] else EXIT_VERIFY_FAILED
    if not report["ok"]:
        report["error"] = "恢复后校验未全部通过：" + json.dumps(report["checks"], ensure_ascii=False)
    _write_report(report, report_path, dump.parent, _stamp(), "", started)
    return report


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_check_tools(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"action": "check-tools", "ok": True, "tools": {}}
    for name in ("pg_dump", "pg_restore", "psql", "pg_isready"):
        path = find_pg_tool(name, args.pg_bin)
        report["tools"][name] = (
            {"path": str(path), "version": tool_version(path)} if path else {"path": None, "version": None}
        )
        if path is None and name in ("pg_dump", "pg_restore"):
            report["ok"] = False
    report["exit_code"] = EXIT_OK if report["ok"] else EXIT_TOOL_MISSING
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["exit_code"])


def _cmd_digest(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "action": "digest",
        "database": {"url": parse_database_url(args.database_url).redacted()},
        "content_digest": database_content_digest(args.database_url),
        "audit_digest": audit_signed_digest(args.database_url),
        "ledger": {
            "conservation_check": ledger_conservation_check(args.database_url),
            "sql_invariants": ledger_sql_invariants(args.database_url),
        },
        "ok": True,
        "exit_code": EXIT_OK,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_backup(args: argparse.Namespace) -> int:
    report = perform_backup(
        args.database_url,
        args.output_dir,
        pg_bin=args.pg_bin,
        exclude_table_data=tuple(args.exclude_table_data or []),
        label=args.label,
        digest_out=args.digest_out,
        scan_secrets=not args.no_secret_scan,
        report_path=args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report.get("exit_code", EXIT_OK))


def _cmd_restore(args: argparse.Namespace) -> int:
    report = perform_restore(
        args.database_url,
        args.input,
        pg_bin=args.pg_bin,
        audit_digest_path=args.audit_digest,
        manifest_path=args.manifest,
        clean=not args.no_clean,
        no_owner=not args.keep_owner,
        single_transaction=not args.no_single_transaction,
        jobs=args.jobs,
        expect_digest_match=not args.no_expect_digest,
        report_path=args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report.get("exit_code", EXIT_OK))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup_restore.py",
        description="FinPilot 备份/恢复 + 审计摘要与账本守恒校验（NFR-02、T22、T55）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pg-bin", default=None, help="PostgreSQL bin 目录（默认 FINPILOT_PG_BIN / PATH）")

    p_check = sub.add_parser("check-tools", parents=[common], help="检查 pg_dump/pg_restore/psql 是否可用")
    p_check.set_defaults(func=_cmd_check_tools)

    p_digest = sub.add_parser("digest", parents=[common], help="只打印内容摘要/审计摘要/账本自检")
    p_digest.add_argument("--database-url", required=True)
    p_digest.set_defaults(func=_cmd_digest)

    p_backup = sub.add_parser("backup", parents=[common], help="执行备份并写 JSON 报告")
    p_backup.add_argument("--database-url", required=True)
    p_backup.add_argument("--output-dir", default=str(REPO_ROOT / "backups"))
    p_backup.add_argument("--label", default=None)
    p_backup.add_argument("--digest-out", default=None, help="审计摘要 sidecar 输出路径（建议放在备份介质之外）")
    p_backup.add_argument("--report", default=None)
    p_backup.add_argument("--no-secret-scan", action="store_true", help="跳过备份内密钥扫描")
    p_backup.add_argument(
        "--exclude-table-data",
        action="append",
        default=list(DEFAULT_EXCLUDE_TABLE_DATA),
        help="排除数据的表（可重复；默认 sessions）",
    )
    p_backup.set_defaults(func=_cmd_backup)

    p_restore = sub.add_parser("restore", parents=[common], help="恢复备份并校验")
    p_restore.add_argument("--database-url", required=True)
    p_restore.add_argument("--input", required=True, help="pg_dump -Fc 归档路径")
    p_restore.add_argument("--audit-digest", default=None, help="备份时写出的审计摘要 sidecar")
    p_restore.add_argument("--manifest", default=None, help="备份 manifest（含内容摘要）")
    p_restore.add_argument("--report", default=None)
    p_restore.add_argument("--no-clean", action="store_true", help="不要 --clean --if-exists")
    p_restore.add_argument("--keep-owner", action="store_true", help="保留原 owner（默认 --no-owner）")
    p_restore.add_argument("--no-single-transaction", action="store_true")
    p_restore.add_argument("--jobs", type=int, default=1)
    p_restore.add_argument("--no-expect-digest", action="store_true", help="只校验账本，不要求摘要一致")
    p_restore.set_defaults(func=_cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())