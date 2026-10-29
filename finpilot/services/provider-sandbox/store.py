"""独立提供方沙箱：状态库（Python 标准库 ``sqlite3``，不使用 ORM）。

设计要点（对应 V5 第 7.2、8.2、16.2 节与 API-04、T21、T54）：

- **进程与存储完全独立**：本模块不 import ``finpilot``（主实现用 PostgreSQL）。银行沙箱与商户
  沙箱各自一个 SQLite 文件，默认路径见 ``DEFAULT_BANK_STATE`` / ``DEFAULT_MERCHANT_STATE``，
  由 ``BANK_SANDBOX_STATE`` / ``MERCHANT_SANDBOX_STATE`` 覆盖。
- **稳定业务键幂等**：``operations.stable_operation_ref`` 是主键，``provider_ref`` 唯一；
  重复提交同键同参返回既有记录，同键异参抛 ``IdempotencyConflict``，绝不产生第二个业务效果。
- **业务效果只发生一次**：``effects`` 表对 ``(stable_operation_ref, effect_kind)`` 建唯一约束，
  重复推进/重复回调只能命中 ``INSERT OR IGNORE`` 的 0 行，数据库层面堵死二次结算。
- **事件版本单调**：``events`` 对 ``(stable_operation_ref, event_version)`` 唯一；每次状态变化分配
  递增版本，迟到事件用旧版本重投，接收方据 event_version 判定“旧回执不得覆盖新状态”。
- **投递日志独立于业务状态**：``deliveries`` 记录每次（含重复、迟到、失败）投递的签名头与结果，
  投递失败不回滚业务状态，便于验证“应答丢失但外部已受理”。
- 写事务统一 ``BEGIN IMMEDIATE`` + 进程内可重入锁，读走独立连接；WAL + busy_timeout 支持多进程。

本模块只做存取，不做 HTTP；参数校验与故障注入在 ``bank.py`` / ``merchant.py`` / ``faults.py``。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:  # 以脚本目录（services/provider-sandbox）在 sys.path 上导入
    import signing  # type: ignore
except ImportError:  # pragma: no cover - 以包方式导入时的回退
    from . import signing  # type: ignore

SCHEMA_VERSION = "1"
STATE_DIR = ".state"
DEFAULT_BANK_STATE = "bank_sandbox.sqlite3"
DEFAULT_MERCHANT_STATE = "merchant_sandbox.sqlite3"
BANK_STATE_ENV = "BANK_SANDBOX_STATE"
MERCHANT_STATE_ENV = "MERCHANT_SANDBOX_STATE"

# 提供方状态枚举（与主实现 models.OPERATION_STATUSES 对齐，但不共享代码）
OPERATION_STATUSES = ("accepted", "pending", "succeeded", "failed", "cancelled")
MERCHANT_STATUSES = ("accepted", "pending", "confirmed", "rejected", "cancelled", "failed")

FAULT_DEFAULTS: dict[str, Any] = {
    "delay_ms": 0,
    "drop_response": False,
    "unavailable": False,
    "reject_next": 0,
    "query_fail_times": 0,
}


class StoreError(RuntimeError):
    """状态库层错误（不携带内部 SQL 细节，可安全转为 500/503）。"""


class IdempotencyConflict(StoreError):
    """同一稳定业务键携带了不同参数。"""

    def __init__(self, stable_operation_ref: str) -> None:
        super().__init__(f"stable_operation_ref reused with different parameters: {stable_operation_ref}")
        self.stable_operation_ref = stable_operation_ref


class OperationNotFound(StoreError):
    def __init__(self, stable_operation_ref: str) -> None:
        super().__init__(f"unknown stable_operation_ref: {stable_operation_ref}")
        self.stable_operation_ref = stable_operation_ref


# ---------------------------------------------------------------------------
# 时间与 ID
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """``services/provider-sandbox/store.py`` → 仓库根目录。"""
    return Path(__file__).resolve().parents[2]


def default_state_path(env_var: str, filename: str) -> Path:
    """状态库路径：``{env_var}`` 优先，否则 ``<仓库根>/.state/<filename>``。

    即银行默认 ``G:/比赛/finpilot/.state/bank_sandbox.sqlite3``，
    商户默认 ``G:/比赛/finpilot/.state/merchant_sandbox.sqlite3``（两者必须是不同文件）。
    """
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw).expanduser()
    return repo_root() / STATE_DIR / filename


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    value = moment or utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def canonical_json(value: Any) -> str:
    return signing.canonical_json(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return {} if default is None else default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - 只可能是人为改库
        return {} if default is None else default


# ---------------------------------------------------------------------------
# 状态库
# ---------------------------------------------------------------------------


class SandboxStore:
    """一个提供方一份独立 SQLite 状态库。"""

    def __init__(
        self,
        path: str | Path,
        *,
        provider_id: str,
        prefix: str,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.provider_id = provider_id
        self.prefix = prefix
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.RLock()

    # -- 连接 ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：进程内 RLock + 库级 ``BEGIN IMMEDIATE``。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                conn.close()
                raise
            try:
                yield conn
            except BaseException:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - 事务已结束时无操作可回滚
                    pass
                conn.close()
                raise
            else:
                try:
                    # ``executescript`` 会先隐式提交，故必须先判断事务是否仍然活跃
                    if conn.in_transaction:
                        conn.execute("COMMIT")
                finally:
                    conn.close()

    # -- Schema ------------------------------------------------------------

    def initialize(self, *, quotes: Sequence[Mapping[str, Any]] | None = None) -> "SandboxStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.write() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('provider_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.provider_id,),
            )
            if quotes:
                for quote in quotes:
                    self._insert_quote(conn, quote)
        return self

    @staticmethod
    def _insert_quote(conn: sqlite3.Connection, quote: Mapping[str, Any]) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO quotes(
                quote_id, merchant_id, merchant_name, category, offer_code, title,
                amount_minor, fee_minor, cancel_fee_minor, refundable, inventory,
                terms_version, quote_ttl_seconds, untrusted_text, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                quote["quote_id"],
                quote["merchant_id"],
                quote.get("merchant_name", ""),
                quote["category"],
                quote["offer_code"],
                quote["title"],
                int(quote["amount_minor"]),
                int(quote.get("fee_minor", 0)),
                int(quote.get("cancel_fee_minor", 0)),
                1 if quote.get("refundable", True) else 0,
                int(quote.get("inventory", 0)),
                quote.get("terms_version", "terms-2026.10"),
                int(quote.get("quote_ttl_seconds", 1800)),
                quote.get("untrusted_text", ""),
                now_iso(),
            ),
        )

    # -- 故障注入配置 -------------------------------------------------------

    def read_faults(self, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        def _read(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT config_json FROM faults WHERE scope='default'").fetchone()
            config = dict(FAULT_DEFAULTS)
            if row is not None:
                config.update(_loads(row["config_json"], {}))
            return config

        if conn is not None:
            return _read(conn)
        with self.read() as c:
            return _read(c)

    def write_faults(self, config: Mapping[str, Any]) -> dict[str, Any]:
        normalised = {k: config.get(k, FAULT_DEFAULTS[k]) for k in FAULT_DEFAULTS}
        with self.write() as conn:
            conn.execute(
                "INSERT INTO faults(scope, config_json, updated_at) VALUES('default', ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET config_json=excluded.config_json, updated_at=excluded.updated_at",
                (_dumps(normalised), now_iso()),
            )
        return normalised

    def consume_counter(self, name: str, conn: sqlite3.Connection | None = None) -> bool:
        """消耗型故障计数（reject_next / query_fail_times）：真正扣减一次。

        ``conn`` 由调用方传入时复用其写事务（SQLite 不支持同库嵌套写事务）。
        """
        if conn is not None:
            return self._consume_counter(conn, name)
        with self.write() as own:
            return self._consume_counter(own, name)

    def _consume_counter(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute("SELECT config_json FROM faults WHERE scope='default'").fetchone()
        config = dict(FAULT_DEFAULTS)
        if row is not None:
            config.update(_loads(row["config_json"], {}))
        remaining = int(config.get(name) or 0)
        if remaining <= 0:
            return False
        config[name] = remaining - 1
        conn.execute(
            "INSERT INTO faults(scope, config_json, updated_at) VALUES('default', ?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET config_json=excluded.config_json, updated_at=excluded.updated_at",
            (_dumps(config), now_iso()),
        )
        return True

    # -- 操作 ---------------------------------------------------------------

    def get_operation(self, conn: sqlite3.Connection, stable_operation_ref: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM operations WHERE stable_operation_ref=?", (stable_operation_ref,)
        ).fetchone()
        return _operation_row(row) if row is not None else None

    def require_operation(self, conn: sqlite3.Connection, stable_operation_ref: str) -> dict[str, Any]:
        op = self.get_operation(conn, stable_operation_ref)
        if op is None:
            raise OperationNotFound(stable_operation_ref)
        return op

    @staticmethod
    def request_hash(request: Mapping[str, Any]) -> str:
        return signing.sha256_hex(canonical_json(request))

    def assert_same_request(self, operation: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        """同稳定业务键必须同参数：否则抛 ``IdempotencyConflict``（同键异参不得复用）。"""
        if operation["request_hash"] != self.request_hash(request):
            raise IdempotencyConflict(str(operation["stable_operation_ref"]))

    def list_operations(
        self,
        conn: sqlite3.Connection,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM operations"
        params: list[Any] = []
        if statuses:
            wanted = list(statuses)
            sql += " WHERE status IN (" + ",".join("?" for _ in wanted) + ")"
            params.extend(wanted)
        sql += " ORDER BY created_at, rowid LIMIT ?"
        params.append(int(limit))
        return [_operation_row(row) for row in conn.execute(sql, params).fetchall()]

    def create_operation(
        self,
        conn: sqlite3.Connection,
        *,
        stable_operation_ref: str,
        request: Mapping[str, Any],
        status: str,
        data: Mapping[str, Any] | None = None,
        failure_code: str | None = None,
        reason: str = "",
        accepted_at: str | None = None,
    ) -> dict[str, Any]:
        """登记操作；同键同参返回既有记录（幂等），同键异参抛 ``IdempotencyConflict``。"""
        request_hash = self.request_hash(request)
        existing = self.get_operation(conn, stable_operation_ref)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict(stable_operation_ref)
            return existing
        moment = now_iso()
        provider_ref = new_id(self.prefix)
        conn.execute(
            """
            INSERT INTO operations(
                stable_operation_ref, provider_id, provider_ref, status, request_hash, request_json,
                data_json, settled, settled_at, accepted_at, cancelled_at, failure_code, reason,
                last_event_version, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,0,NULL,?,NULL,?,?,0,?,?)
            """,
            (
                stable_operation_ref,
                self.provider_id,
                provider_ref,
                status,
                request_hash,
                _dumps(dict(request)),
                _dumps(dict(data or {})),
                accepted_at,
                failure_code,
                reason,
                moment,
                moment,
            ),
        )
        created = self.get_operation(conn, stable_operation_ref)
        assert created is not None  # 刚插入的行必然存在
        return created

    def update_operation(
        self,
        conn: sqlite3.Connection,
        stable_operation_ref: str,
        *,
        data: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        allowed = {
            "status",
            "settled",
            "settled_at",
            "accepted_at",
            "cancelled_at",
            "failure_code",
            "reason",
            "last_event_version",
        }
        unknown = set(fields) - allowed
        if unknown:  # pragma: no cover - 编程错误
            raise StoreError(f"unsupported operation fields: {sorted(unknown)}")
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            params.append(int(value) if isinstance(value, bool) else value)
        if data is not None:
            assignments.append("data_json=?")
            params.append(_dumps(dict(data)))
        assignments.append("updated_at=?")
        params.append(now_iso())
        params.append(stable_operation_ref)
        conn.execute(f"UPDATE operations SET {', '.join(assignments)} WHERE stable_operation_ref=?", params)
        return self.require_operation(conn, stable_operation_ref)

    def mark_settled(
        self,
        conn: sqlite3.Connection,
        stable_operation_ref: str,
        *,
        status: str,
        settled_at: str | None = None,
        failure_code: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """条件更新：只有 ``settled=0`` 的行会被推进，返回是否由本次调用推进。"""
        cur = conn.execute(
            """
            UPDATE operations
               SET settled=1, status=?, settled_at=?, failure_code=COALESCE(?, failure_code),
                   reason=COALESCE(?, reason), updated_at=?
             WHERE stable_operation_ref=? AND settled=0
            """,
            (status, settled_at or now_iso(), failure_code, reason, now_iso(), stable_operation_ref),
        )
        return cur.rowcount == 1

    def apply_effect(
        self,
        conn: sqlite3.Connection,
        stable_operation_ref: str,
        effect_kind: str,
        *,
        amount_minor: int = 0,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        """登记业务效果；``(键, 类型)`` 唯一，重复调用返回 ``False``（未产生第二次效果）。"""
        cur = conn.execute(
            "INSERT OR IGNORE INTO effects(effect_id, stable_operation_ref, effect_kind, amount_minor, "
            "detail_json, applied_at) VALUES(?,?,?,?,?,?)",
            (
                new_id("eff"),
                stable_operation_ref,
                effect_kind,
                int(amount_minor),
                _dumps(dict(detail or {})),
                now_iso(),
            ),
        )
        return cur.rowcount == 1

    def effects(self, conn: sqlite3.Connection, stable_operation_ref: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT effect_kind, amount_minor, detail_json, applied_at FROM effects "
            "WHERE stable_operation_ref=? ORDER BY applied_at, rowid",
            (stable_operation_ref,),
        ).fetchall()
        return [
            {
                "effect_kind": row["effect_kind"],
                "amount_minor": int(row["amount_minor"]),
                "detail": _loads(row["detail_json"], {}),
                "applied_at": row["applied_at"],
            }
            for row in rows
        ]

    # -- 事件 ---------------------------------------------------------------

    def next_event_version(self, conn: sqlite3.Connection, stable_operation_ref: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_version), 0) AS v FROM events WHERE stable_operation_ref=?",
            (stable_operation_ref,),
        ).fetchone()
        return int(row["v"]) + 1

    def record_event(
        self,
        conn: sqlite3.Connection,
        *,
        operation: Mapping[str, Any],
        status: str,
        event_type: str,
        extra: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """生成一条状态事件（版本递增、含 payload_hash），等待投递。"""
        key = str(operation["stable_operation_ref"])
        version = self.next_event_version(conn, key)
        moment = occurred_at or now_iso()
        payload: dict[str, Any] = {
            "event_id": new_id(f"evt_{self.prefix}"),
            "provider_id": self.provider_id,
            "operation_id": key,  # 稳定业务键：跨系统对齐的唯一业务身份
            "stable_operation_ref": key,
            "provider_ref": str(operation["provider_ref"]),
            "event_type": event_type,
            "event_version": version,
            "status": status,
            "occurred_at": moment,
            "settled": bool(operation.get("settled")),
            "sequence_provider": self.provider_id,
            "schema_version": "1.0",
        }
        payload.update(dict(extra or {}))
        payload = signing.canonical_payload(payload)
        conn.execute(
            """
            INSERT INTO events(
                event_id, provider_id, stable_operation_ref, provider_ref, event_type, event_version,
                status, occurred_at, payload_hash, payload_json, delivery_state, delivery_attempts, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',0,?)
            """,
            (
                payload["event_id"],
                self.provider_id,
                key,
                payload["provider_ref"],
                event_type,
                version,
                status,
                moment,
                payload["payload_hash"],
                _dumps(payload),
                now_iso(),
            ),
        )
        conn.execute(
            "UPDATE operations SET last_event_version=?, updated_at=? WHERE stable_operation_ref=?",
            (version, now_iso(), key),
        )
        return payload

    def pending_events(
        self,
        conn: sqlite3.Connection,
        *,
        stable_operation_ref: str | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE delivery_state='pending'"
        params: list[Any] = []
        if stable_operation_ref is not None:
            sql += " AND stable_operation_ref=?"
            params.append(stable_operation_ref)
        sql += " ORDER BY event_version " + ("DESC" if descending else "ASC") + ", rowid " + (
            "DESC" if descending else "ASC"
        )
        return [_event_row(row) for row in conn.execute(sql, params).fetchall()]

    def event_by_version(
        self, conn: sqlite3.Connection, stable_operation_ref: str, event_version: int
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM events WHERE stable_operation_ref=? AND event_version=?",
            (stable_operation_ref, int(event_version)),
        ).fetchone()
        return _event_row(row) if row is not None else None

    def latest_event(self, conn: sqlite3.Connection, stable_operation_ref: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM events WHERE stable_operation_ref=? ORDER BY event_version DESC, rowid DESC LIMIT 1",
            (stable_operation_ref,),
        ).fetchone()
        return _event_row(row) if row is not None else None

    def latest_delivered_event(
        self, conn: sqlite3.Connection, stable_operation_ref: str | None = None
    ) -> dict[str, Any] | None:
        sql = (
            "SELECT e.* FROM events e JOIN deliveries d ON d.event_id = e.event_id "
            "WHERE d.delivered=1"
        )
        params: list[Any] = []
        if stable_operation_ref is not None:
            sql += " AND e.stable_operation_ref=?"
            params.append(stable_operation_ref)
        sql += " ORDER BY d.created_at DESC, d.rowid DESC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        return _event_row(row) if row is not None else None

    def events_for(self, conn: sqlite3.Connection, stable_operation_ref: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM events WHERE stable_operation_ref=? ORDER BY event_version, rowid",
            (stable_operation_ref,),
        ).fetchall()
        return [_event_row(row) for row in rows]

    def mark_event_delivered(
        self, conn: sqlite3.Connection, event_id: str, *, delivered: bool
    ) -> None:
        conn.execute(
            "UPDATE events SET delivery_attempts=delivery_attempts+1, "
            "delivery_state=CASE WHEN ? THEN 'delivered' ELSE delivery_state END WHERE event_id=?",
            (1 if delivered else 0, event_id),
        )

    # -- 投递日志 -----------------------------------------------------------

    def record_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        event: Mapping[str, Any],
        kind: str,
        attempt: int,
        target_url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        delivery_id = new_id("dlv")
        delivered = bool(result.get("delivered"))
        created_at = now_iso()
        conn.execute(
            """
            INSERT INTO deliveries(
                delivery_id, event_id, stable_operation_ref, provider_id, provider_ref, event_version,
                kind, attempt, target_url, headers_json, payload_json, delivered, http_status, error, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                delivery_id,
                event["event_id"],
                event["stable_operation_ref"],
                event["provider_id"],
                event["provider_ref"],
                int(event["event_version"]),
                kind,
                int(attempt),
                target_url,
                _dumps(dict(headers)),
                _dumps(dict(payload)),
                1 if delivered else 0,
                result.get("http_status"),
                result.get("error"),
                created_at,
            ),
        )
        return {
            "delivery_id": delivery_id,
            "event_id": event["event_id"],
            "stable_operation_ref": event["stable_operation_ref"],
            "provider_id": event["provider_id"],
            "provider_ref": event["provider_ref"],
            "event_version": int(event["event_version"]),
            "kind": kind,
            "attempt": int(attempt),
            "target_url": target_url,
            "headers": dict(headers),
            "payload": dict(payload),
            "delivered": delivered,
            "http_status": result.get("http_status"),
            "error": result.get("error"),
            "signature": headers.get(signing.SIGNATURE_HEADER),
            "nonce": headers.get(signing.NONCE_HEADER),
            "timestamp": headers.get(signing.TIMESTAMP_HEADER),
            "payload_hash": payload.get("payload_hash"),
            "created_at": created_at,
        }

    def deliveries(
        self,
        conn: sqlite3.Connection,
        *,
        stable_operation_ref: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM deliveries"
        params: list[Any] = []
        if stable_operation_ref is not None:
            sql += " WHERE stable_operation_ref=?"
            params.append(stable_operation_ref)
        sql += " ORDER BY created_at, rowid LIMIT ?"
        params.append(int(limit))
        return [
            {
                "delivery_id": row["delivery_id"],
                "event_id": row["event_id"],
                "stable_operation_ref": row["stable_operation_ref"],
                "provider_ref": row["provider_ref"],
                "event_version": int(row["event_version"]),
                "kind": row["kind"],
                "attempt": int(row["attempt"]),
                "target_url": row["target_url"],
                "headers": _loads(row["headers_json"], {}),
                "payload": _loads(row["payload_json"], {}),
                "delivered": bool(row["delivered"]),
                "http_status": row["http_status"],
                "error": row["error"],
                "created_at": row["created_at"],
            }
            for row in conn.execute(sql, params).fetchall()
        ]

    def delivery_attempts(self, conn: sqlite3.Connection, event_id: str) -> int:
        row = conn.execute("SELECT COUNT(*) AS n FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
        return int(row["n"])

    # -- 事件投递（Outbox：投递失败不改业务状态，事件保持 pending 待重投） ----

    def emit_pending(
        self,
        conn: sqlite3.Connection,
        *,
        stable_operation_ref: str | None = None,
        duplicate: int = 0,
        replay_event_version: int | None = None,
        descending: bool = False,
        secret: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """投递待发事件，并按需重复投递、重投历史版本（迟到/乱序）。

        - ``descending=True``：把同一操作的多个待发事件按版本从大到小投递，模拟乱序到达。
        - ``replay_event_version``：重投某个历史版本（迟到事件），接收方必须按 event_version
          判定“旧回执不得覆盖新状态”。
        - ``duplicate``：对同一 ``event_id`` 再投递 N 次（真重复投递，接收方按 event_id 去重）。
        """
        target = signing.callback_url(self.provider_id, base_url)
        emitted: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any]] = []

        for event in self.pending_events(
            conn, stable_operation_ref=stable_operation_ref, descending=descending
        ):
            deliveries.append(self._deliver(conn, event, kind="primary", target=target, secret=secret))
            emitted.append(event)

        replayed: dict[str, Any] | None = None
        if replay_event_version is not None:
            if stable_operation_ref is None:
                raise StoreError("replay_event_version requires stable_operation_ref")
            replayed = self.event_by_version(conn, stable_operation_ref, int(replay_event_version))
            if replayed is None:
                raise OperationNotFound(f"{stable_operation_ref}#v{replay_event_version}")
            deliveries.append(self._deliver(conn, replayed, kind="late", target=target, secret=secret))
            emitted.append(replayed)

        duplicates_sent = 0
        count = max(0, int(duplicate))
        if count:
            anchor = replayed or (emitted[-1] if emitted else None)
            if anchor is None:
                anchor = self.latest_delivered_event(conn, stable_operation_ref)
            if anchor is None:
                raise StoreError("no delivered event available to duplicate")
            for _ in range(count):
                deliveries.append(self._deliver(conn, anchor, kind="duplicate", target=target, secret=secret))
                duplicates_sent += 1

        return {
            "target_url": target,
            "events": emitted,
            "deliveries": deliveries,
            "duplicates_sent": duplicates_sent,
            "delivered_count": sum(1 for d in deliveries if d["delivered"]),
        }

    def _deliver(
        self,
        conn: sqlite3.Connection,
        event: Mapping[str, Any],
        *,
        kind: str,
        target: str,
        secret: str | None = None,
    ) -> dict[str, Any]:
        signed = signing.signed_callback(
            event["payload"], provider_id=self.provider_id, secret=secret, delivery_kind=kind
        )
        result = signing.deliver_callback(target, signed.headers, signed.payload)
        attempt = self.delivery_attempts(conn, str(event["event_id"])) + 1
        self.mark_event_delivered(conn, str(event["event_id"]), delivered=bool(result.get("delivered")))
        return self.record_delivery(
            conn,
            event=event,
            kind=kind,
            attempt=attempt,
            target_url=target,
            headers=signed.headers,
            payload=signed.payload,
            result=result,
        )

    # -- 报价（商户沙箱） ---------------------------------------------------

    def list_quotes(
        self,
        conn: sqlite3.Connection,
        *,
        merchant_id: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM quotes WHERE 1=1"
        params: list[Any] = []
        if merchant_id:
            sql += " AND merchant_id=?"
            params.append(merchant_id)
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY category, merchant_id, offer_code"
        return [_quote_row(row) for row in conn.execute(sql, params).fetchall()]

    def get_quote(self, conn: sqlite3.Connection, quote_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM quotes WHERE quote_id=?", (quote_id,)).fetchone()
        return _quote_row(row) if row is not None else None

    def update_quote(self, conn: sqlite3.Connection, quote_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"amount_minor", "fee_minor", "cancel_fee_minor", "refundable", "inventory", "terms_version"}
        unknown = set(fields) - allowed
        if unknown:
            raise StoreError(f"unsupported quote fields: {sorted(unknown)}")
        if not fields:
            return self.get_quote(conn, quote_id)
        assignments = ", ".join(f"{key}=?" for key in fields)
        params = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        params.append(now_iso())
        params.append(quote_id)
        conn.execute(f"UPDATE quotes SET {assignments}, updated_at=? WHERE quote_id=?", params)
        return self.get_quote(conn, quote_id)

    def reserve_inventory(self, conn: sqlite3.Connection, quote_id: str, quantity: int) -> bool:
        """条件扣减库存：库存不足时返回 ``False``，绝不超卖。"""
        cur = conn.execute(
            "UPDATE quotes SET inventory=inventory-?, updated_at=? WHERE quote_id=? AND inventory>=?",
            (int(quantity), now_iso(), quote_id, int(quantity)),
        )
        return cur.rowcount == 1

    def release_inventory(self, conn: sqlite3.Connection, quote_id: str, quantity: int) -> bool:
        cur = conn.execute(
            "UPDATE quotes SET inventory=inventory+?, updated_at=? WHERE quote_id=?",
            (int(quantity), now_iso(), quote_id),
        )
        return cur.rowcount == 1

    # -- 汇总 ---------------------------------------------------------------

    def counts(self, conn: sqlite3.Connection) -> dict[str, int]:
        def _count(table: str) -> int:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            return int(row["n"])

        return {
            "operations": _count("operations"),
            "events": _count("events"),
            "deliveries": _count("deliveries"),
            "effects": _count("effects"),
        }

    def stats(self) -> dict[str, Any]:
        with self.read() as conn:
            counts = self.counts(conn)
        return {
            "provider_id": self.provider_id,
            "state_path": str(self.path),
            "exists": self.path.exists(),
            "schema_version": SCHEMA_VERSION,
            **counts,
            "observed_at": now_iso(),
        }


# ---------------------------------------------------------------------------
# 行 → dict
# ---------------------------------------------------------------------------


def _operation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "stable_operation_ref": row["stable_operation_ref"],
        "provider_id": row["provider_id"],
        "provider_ref": row["provider_ref"],
        "status": row["status"],
        "request_hash": row["request_hash"],
        "request": _loads(row["request_json"], {}),
        "data": _loads(row["data_json"], {}),
        "settled": bool(row["settled"]),
        "settled_at": row["settled_at"],
        "accepted_at": row["accepted_at"],
        "cancelled_at": row["cancelled_at"],
        "failure_code": row["failure_code"],
        "reason": row["reason"] or "",
        "last_event_version": int(row["last_event_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "provider_id": row["provider_id"],
        "stable_operation_ref": row["stable_operation_ref"],
        "provider_ref": row["provider_ref"],
        "event_type": row["event_type"],
        "event_version": int(row["event_version"]),
        "status": row["status"],
        "occurred_at": row["occurred_at"],
        "payload_hash": row["payload_hash"],
        "payload": _loads(row["payload_json"], {}),
        "delivery_state": row["delivery_state"],
        "delivery_attempts": int(row["delivery_attempts"]),
        "created_at": row["created_at"],
    }


def _quote_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "quote_id": row["quote_id"],
        "merchant_id": row["merchant_id"],
        "merchant_name": row["merchant_name"],
        "category": row["category"],
        "offer_code": row["offer_code"],
        "title": row["title"],
        "amount_minor": int(row["amount_minor"]),
        "fee_minor": int(row["fee_minor"]),
        "cancel_fee_minor": int(row["cancel_fee_minor"]),
        "refundable": bool(row["refundable"]),
        "inventory": int(row["inventory"]),
        "terms_version": row["terms_version"],
        "quote_ttl_seconds": int(row["quote_ttl_seconds"]),
        "untrusted_text": row["untrusted_text"] or "",
        "updated_at": row["updated_at"],
    }


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    stable_operation_ref TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    provider_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    settled INTEGER NOT NULL DEFAULT 0,
    settled_at TEXT,
    accepted_at TEXT,
    cancelled_at TEXT,
    failure_code TEXT,
    reason TEXT NOT NULL DEFAULT '',
    last_event_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_operations_status ON operations(status);

CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY,
    stable_operation_ref TEXT NOT NULL REFERENCES operations(stable_operation_ref) ON DELETE CASCADE,
    effect_kind TEXT NOT NULL,
    amount_minor INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    applied_at TEXT NOT NULL,
    UNIQUE (stable_operation_ref, effect_kind)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    stable_operation_ref TEXT NOT NULL,
    provider_ref TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (stable_operation_ref, event_version)
);

CREATE INDEX IF NOT EXISTS ix_events_delivery ON events(delivery_state);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    stable_operation_ref TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_ref TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    target_url TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_deliveries_operation ON deliveries(stable_operation_ref);

CREATE TABLE IF NOT EXISTS faults (
    scope TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    quote_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    merchant_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    offer_code TEXT NOT NULL,
    title TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    fee_minor INTEGER NOT NULL DEFAULT 0,
    cancel_fee_minor INTEGER NOT NULL DEFAULT 0,
    refundable INTEGER NOT NULL DEFAULT 1,
    inventory INTEGER NOT NULL DEFAULT 0,
    terms_version TEXT NOT NULL DEFAULT 'terms-2026.10',
    quote_ttl_seconds INTEGER NOT NULL DEFAULT 1800,
    untrusted_text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_quotes_merchant ON quotes(merchant_id, category);
"""


__all__ = [
    "BANK_STATE_ENV",
    "DEFAULT_BANK_STATE",
    "DEFAULT_MERCHANT_STATE",
    "FAULT_DEFAULTS",
    "IdempotencyConflict",
    "MERCHANT_STATE_ENV",
    "MERCHANT_STATUSES",
    "OPERATION_STATUSES",
    "OperationNotFound",
    "SCHEMA_VERSION",
    "STATE_DIR",
    "SandboxStore",
    "StoreError",
    "canonical_json",
    "default_state_path",
    "iso",
    "new_id",
    "now_iso",
    "repo_root",
    "utcnow",
]