from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

from stockquant.data import AkshareProvider, DoltHubProvider
from stockquant.data.akshare_provider import normalize_symbol
from stockquant.data.membership import load_membership_csv, write_membership_csv


CARRY_FIELDS = [
    "sector", "market_cap", "pe", "pb", "roe", "eps", "bps",
    "revenue_growth", "profit_growth", "gross_margin",
    "forecast_growth", "forecast_positive", "holder_change",
]


def _load_rows(path: str) -> List[Dict[str, object]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(rows: List[Dict[str, object]], path: str, fieldnames: List[str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))
    with target.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def update_market_dataset(
    data_path: str,
    membership_path: str,
    end_date: str,
    symbols_file: str = "",
    workers: int = 1,
) -> Dict[str, object]:
    existing = _load_rows(data_path)
    if not existing:
        raise ValueError("empty dataset")
    fieldnames = list(existing[0].keys())
    symbols = sorted({str(r["symbol"]).upper() for r in existing})
    if symbols_file and Path(symbols_file).exists():
        requested = [x.strip().upper() for x in Path(symbols_file).read_text(encoding="utf-8").splitlines() if x.strip()]
        symbols = sorted(set(symbols) | set(requested))
    old_last_date = max(str(r["date"]) for r in existing)
    start = (date.fromisoformat(old_last_date) - timedelta(days=7)).isoformat()
    provider = AkshareProvider(max_workers=workers, retries=3, retry_sleep=1.0, pause=0.15)
    symbols_map = {s: "" for s in symbols}
    fetched = provider.fetch_daily(symbols_map, start=start.replace("-", ""), end=end_date.replace("-", ""), adjust="hfq", align_calendar=True)
    fetched_rows = fetched.rows
    if not fetched_rows:
        raise RuntimeError(f"No live rows fetched: {fetched.errors}")
    old_by_symbol = {}
    for row in existing:
        old_by_symbol.setdefault(str(row["symbol"]), []).append(row)
    for rows in old_by_symbol.values():
        rows.sort(key=lambda r: str(r["date"]))
    for row in fetched_rows:
        symbol = str(row["symbol"])
        prior = [x for x in old_by_symbol.get(symbol, []) if str(x["date"]) <= str(row["date"])]
        if prior:
            latest = prior[-1]
            for field in CARRY_FIELDS:
                if str(row.get(field, "")) in {"", "None", "nan"}:
                    row[field] = latest.get(field, "")
        for field in fieldnames:
            row.setdefault(field, "")
    lookup = {(str(r["date"]), str(r["symbol"])): r for r in existing}
    for row in fetched_rows:
        lookup[(str(row["date"]), str(row["symbol"]))] = row
    merged = list(lookup.values())
    _write_rows(merged, data_path, fieldnames)

    membership = load_membership_csv(membership_path) if Path(membership_path).exists() else {}
    try:
        dolt = DoltHubProvider()
        live_membership = dolt.fetch_membership(
            index_code="399300.SZ",
            start=(date.fromisoformat(old_last_date) + timedelta(days=1)).isoformat(),
            end=end_date,
        )
        for d, members in live_membership.items():
            membership[d] = set(members)
    except Exception:
        pass
    try:
        current = AkshareProvider(max_workers=1).index_constituents(index_code="000300")
        if current:
            membership[end_date] = set(current.keys())
    except Exception:
        pass
    write_membership_csv(membership, membership_path)
    metadata = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "old_last_date": old_last_date,
        "new_last_date": max(str(r["date"]) for r in merged),
        "rows_added_or_replaced": len(fetched_rows),
        "symbols_loaded": len({str(r["symbol"]) for r in fetched_rows}),
        "errors": fetched.errors,
        "membership_snapshots": len(membership),
        "warnings": fetched.warnings,
    }
    Path(data_path).with_name(Path(data_path).stem + "_live_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return metadata
