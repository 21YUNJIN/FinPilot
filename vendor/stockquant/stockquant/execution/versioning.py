from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class StrategyVersion:
    version_id: str
    name: str
    stage: str
    config: Dict[str, object]
    metrics: Dict[str, float]
    created_at_utc: str
    parent_id: Optional[str] = None
    active: bool = False


class StrategyRegistry:
    STAGES = ["backtest", "paper", "small_live", "live", "retired"]

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save([])

    def _load(self) -> List[Dict[str, object]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, rows: List[Dict[str, object]]) -> None:
        self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def register(self, name: str, config: Dict[str, object], metrics: Dict[str, float], stage: str = "backtest") -> Dict[str, object]:
        if stage not in self.STAGES:
            raise ValueError("invalid stage")
        rows = self._load()
        raw = json.dumps(config, sort_keys=True, default=str).encode("utf-8")
        version_id = hashlib.sha256(raw).hexdigest()[:12]
        if any(r["version_id"] == version_id for r in rows):
            return next(r for r in rows if r["version_id"] == version_id)
        record = {
            "version_id": version_id,
            "name": name,
            "stage": stage,
            "config": config,
            "metrics": metrics,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "parent_id": None,
            "active": False,
        }
        rows.append(record)
        self._save(rows)
        return record

    def active(self) -> Optional[Dict[str, object]]:
        rows = self._load()
        for row in reversed(rows):
            if row.get("active"):
                return row
        return None

    def promote(self, version_id: str, stage: str) -> Dict[str, object]:
        if stage not in self.STAGES:
            raise ValueError("invalid stage")
        rows = self._load()
        selected = None
        for row in rows:
            if row["version_id"] == version_id:
                selected = row
                break
        if selected is None:
            raise ValueError("version not found")
        if stage in {"small_live", "live"} and selected.get("stage") not in {"backtest", "paper", "small_live", "live"}:
            raise ValueError("invalid lifecycle transition")
        for row in rows:
            if row.get("active"):
                row["active"] = False
        selected["stage"] = stage
        selected["active"] = stage in {"small_live", "live"}
        self._save(rows)
        return selected

    def rollback(self) -> Dict[str, object]:
        rows = self._load()
        live_versions = [r for r in rows if r.get("stage") in {"small_live", "live"}]
        if len(live_versions) < 2:
            raise ValueError("no previous live version available")
        current = live_versions[-1]
        previous = live_versions[-2]
        current["active"] = False
        previous["active"] = True
        self._save(rows)
        return previous

    def list_versions(self) -> List[Dict[str, object]]:
        return self._load()
