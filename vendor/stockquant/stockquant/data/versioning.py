"""Data versioning and snapshot verification.

Reproducibility requires that every backtest can be tied to the exact bytes of
the data it consumed.  These helpers compute a SHA-256 digest and a small
manifest (row count, date range, byte size) for a CSV, write the manifest next
to the file, and later verify that the file has not changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence


def file_sha256(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def csv_shape(path: str, date_field: str = "date") -> Dict[str, object]:
    """Return row count and first/last values of ``date_field`` for a CSV."""
    rows = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if date_field not in (reader.fieldnames or []):
            raise ValueError(f"date field {date_field!r} not found in CSV header")
        for row in reader:
            rows += 1
            value = str(row.get(date_field) or "")
            if not first_date:
                first_date = value
            last_date = value
    return {"rows": rows, "first_date": first_date, "last_date": last_date}


def build_manifest(path: str, date_field: str = "date", note: str = "") -> Dict[str, object]:
    stat = os.stat(path)
    shape = csv_shape(path, date_field)
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        "size_bytes": stat.st_size,
        "date_field": date_field,
        "rows": shape["rows"],
        "first_date": shape["first_date"],
        "last_date": shape["last_date"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }


def write_manifest(path: str, manifest_path: Optional[str] = None, date_field: str = "date", note: str = "") -> str:
    manifest = build_manifest(path, date_field, note)
    dest = manifest_path or (str(path) + ".manifest.json")
    with open(dest, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return dest


def verify_manifest(path: str, manifest_path: Optional[str] = None) -> bool:
    """Return True if the file still matches its recorded SHA-256."""
    src = manifest_path or (str(path) + ".manifest.json")
    with open(src, encoding="utf-8") as handle:
        manifest = json.load(handle)
    return file_sha256(path) == manifest["sha256"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write or verify a data snapshot manifest.")
    parser.add_argument("csv_path")
    parser.add_argument("--verify", action="store_true", help="verify against the existing manifest")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.verify:
        ok = verify_manifest(args.csv_path)
        print(("OK: file unchanged" if ok else "MISMATCH: file has changed"))
        return 0 if ok else 1
    dest = write_manifest(args.csv_path, note=args.note)
    print(f"Wrote manifest: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
