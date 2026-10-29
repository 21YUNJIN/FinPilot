"""Cross-process file locking for JSON state files.

The dashboard and the assistant daemons can write the same ``*.json`` state
files concurrently.  A read-modify-write cycle without a lock can lose updates
(a double-click on "我买了" from two tabs).  This module wraps ``flock`` so any
mutation becomes atomic with respect to other processes using the same helper.

Falls back to a no-op when ``fcntl`` is unavailable (e.g. Windows), matching the
dependency-light philosophy.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
    HAS_FCNTL = True
except Exception:  # pragma: no cover - non-POSIX
    HAS_FCNTL = False


@contextmanager
def file_lock(path, timeout: float = 5.0) -> Iterator[None]:
    """Exclusive lock keyed on ``<path>.lock`` for the duration of the block."""
    if not HAS_FCNTL:
        yield
        return
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_text(path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
