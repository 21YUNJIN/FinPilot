from __future__ import annotations

import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _stable_id(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def append_experiment(
    path: str,
    kind: str,
    config: Dict[str, Any],
    metrics: Dict[str, float],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append an auditable experiment record as JSON Lines."""
    payload: Dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "config": config,
        "metrics": metrics,
        "extra": extra or {},
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    }
    payload["run_id"] = _stable_id(payload)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload
