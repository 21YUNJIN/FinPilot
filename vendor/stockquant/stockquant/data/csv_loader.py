from __future__ import annotations

import csv
import pickle
from pathlib import Path
from typing import Dict, Iterable, List

from stockquant.domain import DuplicateBarError, MarketData, parse_date

FIELDS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "sector",
    "market_cap",
    "pe",
    "pb",
    "roe",
    "suspended",
    "limit_up",
    "limit_down",
    "is_st",
    "source",
    "raw_close",
    "eps",
    "bps",
    "revenue_growth",
    "profit_growth",
    "gross_margin",
    "forecast_growth",
    "forecast_positive",
    "holder_change",
    "available_at",
    "fundamentals_available_at",
]


# Bump when the MarketData schema or the loader's integrity checks change:
# a stale pickle would otherwise re-hide the very corruption the loader now
# rejects (duplicate keys, membership semantics, ...).
CACHE_VERSION = 3


def _cache_path(path: str) -> Path:
    p = Path(path)
    return p.with_suffix(p.suffix + f".v{CACHE_VERSION}.pkl")


def _legacy_cache_path(path: str) -> Path:
    p = Path(path)
    return p.with_suffix(p.suffix + ".pkl")


def load_csv(path: str, use_cache: bool = True) -> MarketData:
    """Load a long-format CSV into :class:`MarketData`.

    Building ~1.1M ``Bar`` objects in pure Python takes 30-140s, so the result is
    cached next to the CSV as a pickle keyed on the source file's mtime (a 7s
    load on a warm cache).  Set ``use_cache=False`` to force a rebuild.
    """
    cache = _cache_path(path)
    src = Path(path)
    if use_cache:
        try:
            if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
                with cache.open("rb") as handle:
                    market = pickle.load(handle)
                if isinstance(market, MarketData) and market.bars_by_date:
                    return market
        except Exception:
            pass
    rows: List[Dict[str, object]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"date", "symbol", "open", "high", "low", "close", "volume"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")
        for row in reader:
            row = dict(row)
            row["date"] = parse_date(str(row["date"]))
            rows.append(row)
    if not rows:
        raise ValueError(f"CSV contains no rows: {path}")
    try:
        market = MarketData.from_rows(rows, strict_duplicates=True)
    except DuplicateBarError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if use_cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(cache.suffix + ".tmp")
            with tmp.open("wb") as handle:
                pickle.dump(market, handle, protocol=4)
            tmp.replace(cache)
        except Exception:
            pass
    return market


def write_csv(rows: Iterable[Dict[str, object]], path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
