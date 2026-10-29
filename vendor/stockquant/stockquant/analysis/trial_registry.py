"""Persistent experiment registry for honest multiple-testing accounting.

The Deflated Sharpe Ratio needs the FULL number of strategy variants ever tried
on the data, not just the candidates a single walk-forward happened to evaluate.
A hard-coded ``--declared-trials`` is easy to under-declare, which flatters the
DSR.  This registry appends a hash of every candidate configuration to a local
JSONL file and returns the cumulative distinct count, so trials grow honestly
as the researcher explores.

Path is resolved relative to the repository root (``data/state``), matching the
runtime state the dashboard and assistant already use.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

STATE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "state"


def _registry_path() -> Path:
    return STATE_DIR / "trial_registry.jsonl"


def _signature_hash(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def record_trials(signatures: Iterable[str]) -> int:
    """Append unseen signatures and return the number newly recorded."""
    path = _registry_path()
    seen = set()
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    seen.add(json.loads(line)["hash"])
                except Exception:
                    continue
    except Exception:
        pass
    new = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for sig in signatures:
            h = _signature_hash(str(sig))
            if h in seen:
                continue
            handle.write(json.dumps({"hash": h, "signature": str(sig)}, ensure_ascii=False) + "\n")
            seen.add(h)
            new += 1
    return new


def cumulative_trials() -> int:
    """Total distinct trials recorded so far (0 if none)."""
    path = _registry_path()
    if not path.exists():
        return 0
    seen = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["hash"])
            except Exception:
                continue
    except Exception:
        return 0
    return len(seen)
