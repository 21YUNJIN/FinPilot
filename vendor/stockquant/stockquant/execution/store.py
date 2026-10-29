"""SQLite-backed state, audit log and strategy registry.

The execution stack historically persisted everything as JSON files, which is
fine for a single process but unsafe under concurrent daemons and impossible to
query.  This module provides a small SQLite foundation that is safe for
multi-process use (WAL mode + busy timeout) and keeps an append-only audit log:

* ``StateStore``           - key/value state + append-only audit log.
* ``AuditLog``             - a thin audit-log facade.
* ``SqliteStrategyRegistry`` - drop-in SQLite replacement for ``StrategyRegistry``.

It only uses the standard library so it runs under the dependency-free test
harness.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS strategy_versions (
    version_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    stage TEXT NOT NULL,
    config TEXT NOT NULL,
    metrics TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    parent_id TEXT,
    active INTEGER NOT NULL DEFAULT 0
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(AUDIT_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ---- key/value state -------------------------------------------------
    def set_state(self, key: str, value: object) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(key), json.dumps(value, ensure_ascii=False, default=str), _utc_now()),
            )

    def get_state(self, key: str, default: object = None) -> object:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (str(key),)).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def all_state(self) -> Dict[str, object]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM state").fetchall()
        return {key: json.loads(value) for key, value in rows}

    # ---- audit log -------------------------------------------------------
    def append_audit(self, event_type: str, payload: object) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO audit(ts, event_type, payload) VALUES(?,?,?)",
                (_utc_now(), str(event_type), json.dumps(payload, ensure_ascii=False, default=str)),
            )
            return int(cur.lastrowid)

    def query_audit(self, limit: int = 100, event_type: Optional[str] = None) -> List[Dict[str, object]]:
        with self._connect() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT id, ts, event_type, payload FROM audit WHERE event_type=? ORDER BY id DESC LIMIT ?",
                    (str(event_type), int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ts, event_type, payload FROM audit ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [
            {"id": r[0], "ts": r[1], "event_type": r[2], "payload": json.loads(r[3])}
            for r in rows
        ]

    def count_audit(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0])


class AuditLog:
    """Convenience facade over :class:`StateStore` for append-only audit events."""

    def __init__(self, store: StateStore):
        self.store = store

    def record(self, event_type: str, payload: object) -> int:
        return self.store.append_audit(event_type, payload)

    def entries(self, limit: int = 100, event_type: Optional[str] = None) -> List[Dict[str, object]]:
        return self.store.query_audit(limit=limit, event_type=event_type)


class SqliteStrategyRegistry:
    """SQLite-backed model/strategy registry with the same lifecycle as the JSON
    registry (backtest -> paper -> small_live -> live -> retired)."""

    STAGES = ["backtest", "paper", "small_live", "live", "retired"]

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(AUDIT_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row_to_record(row) -> Dict[str, object]:
        return {
            "version_id": row[0],
            "name": row[1],
            "stage": row[2],
            "config": json.loads(row[3]),
            "metrics": json.loads(row[4]),
            "created_at_utc": row[5],
            "parent_id": row[6],
            "active": bool(row[7]),
        }

    def register(
        self,
        name: str,
        config: Dict[str, object],
        metrics: Dict[str, float],
        stage: str = "backtest",
    ) -> Dict[str, object]:
        if stage not in self.STAGES:
            raise ValueError("invalid stage")
        raw = json.dumps(config, sort_keys=True, default=str).encode("utf-8")
        version_id = hashlib.sha256(raw).hexdigest()[:12]
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            if existing is not None:
                return self._row_to_record(existing)
            conn.execute(
                "INSERT INTO strategy_versions(version_id, name, stage, config, metrics, created_at_utc, parent_id, active) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    name,
                    stage,
                    json.dumps(config, ensure_ascii=False, default=str),
                    json.dumps(metrics, ensure_ascii=False, default=str),
                    _utc_now(),
                    None,
                    0,
                ),
            )
            row = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        return self._row_to_record(row)

    def active(self) -> Optional[Dict[str, object]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions WHERE active=1 ORDER BY created_at_utc DESC LIMIT 1"
            ).fetchone()
        return self._row_to_record(row) if row else None

    def promote(self, version_id: str, stage: str) -> Dict[str, object]:
        if stage not in self.STAGES:
            raise ValueError("invalid stage")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            if row is None:
                raise ValueError("version not found")
            current_stage = row[2]
            if stage in {"small_live", "live"} and current_stage not in {"backtest", "paper", "small_live", "live"}:
                raise ValueError("invalid lifecycle transition")
            conn.execute("UPDATE strategy_versions SET active=0 WHERE active=1")
            conn.execute(
                "UPDATE strategy_versions SET stage=?, active=? WHERE version_id=?",
                (stage, 1 if stage in {"small_live", "live"} else 0, version_id),
            )
            updated = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        return self._row_to_record(updated)

    def rollback(self) -> Dict[str, object]:
        with self._connect() as conn:
            live = conn.execute(
                "SELECT version_id FROM strategy_versions WHERE stage IN ('small_live','live') "
                "ORDER BY created_at_utc ASC"
            ).fetchall()
            if len(live) < 2:
                raise ValueError("no previous live version available")
            current_id = live[-1][0]
            previous_id = live[-2][0]
            conn.execute("UPDATE strategy_versions SET active=0 WHERE version_id=?", (current_id,))
            conn.execute("UPDATE strategy_versions SET active=1 WHERE version_id=?", (previous_id,))
            row = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions WHERE version_id=?",
                (previous_id,),
            ).fetchone()
        return self._row_to_record(row)

    def list_versions(self) -> List[Dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version_id, name, stage, config, metrics, created_at_utc, parent_id, active "
                "FROM strategy_versions ORDER BY created_at_utc ASC"
            ).fetchall()
        return [self._row_to_record(r) for r in rows]


def import_json_state(store: StateStore, json_path: str, key: Optional[str] = None) -> str:
    """Import a legacy JSON state file into the SQLite store.

    Returns the state key used.
    """
    path = Path(json_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    state_key = key or path.stem
    store.set_state(state_key, payload)
    return state_key
