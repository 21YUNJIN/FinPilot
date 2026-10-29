from __future__ import annotations

import csv
import hashlib
import random
from pathlib import Path
from typing import Dict, Iterable, List, Set


def write_membership_csv(membership: Dict[str, Set[str]], path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "symbol"])
        for date in sorted(membership):
            for symbol in sorted(membership[date]):
                writer.writerow([date, symbol])


def load_membership_csv(path: str) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = str(row["date"])
            symbol = str(row["symbol"]).strip().upper()
            out.setdefault(date, set()).add(symbol)
    return out


def membership_union(membership: Dict[str, Set[str]]) -> Set[str]:
    out: Set[str] = set()
    for members in membership.values():
        out.update(members)
    return out


def deterministic_sample(symbols: Iterable[str], limit: int, seed: int = 20261003) -> List[str]:
    """Sample symbols without using performance information."""
    unique = sorted(set(symbols))
    scored = []
    for symbol in unique:
        raw = f"{seed}:{symbol}".encode("utf-8")
        score = int(hashlib.sha256(raw).hexdigest()[:16], 16)
        scored.append((score, symbol))
    scored.sort()
    return [symbol for _, symbol in scored[: max(0, limit)]]
