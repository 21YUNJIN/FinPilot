from __future__ import annotations

import json
import tarfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from stockquant.data.akshare_provider import FetchResult


DEFAULT_QLIB_ARCHIVE = (
    "https://github.com/chenditc/investment_data/releases/download/2026-10-03/qlib_bin.tar.gz"
)
REQUIRED_FIELDS = ("open", "high", "low", "close", "adjclose", "volume", "amount", "factor")


def qlib_symbol(ts_code: str) -> str:
    code, _, exchange = ts_code.partition(".")
    return f"{exchange.lower()}{code}"


class QlibArchiveProvider:
    """Extract selected stock features from a public Qlib binary archive.

    The archive is streamed, so a caller must download through the full tar
    stream once even though only a small subset of feature files is retained.
    """

    def __init__(self, archive_url: str = DEFAULT_QLIB_ARCHIVE):
        self.archive_url = archive_url

    def extract_features(self, symbols: Sequence[str], cache_dir: str) -> Dict[str, str]:
        cache = Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        selected = {qlib_symbol(s): s for s in symbols}
        expected = {f"{sym}/{field}.day.bin" for sym in selected for field in REQUIRED_FIELDS}
        found: Dict[str, str] = {}
        calendar_path = cache / "day.txt"
        if calendar_path.exists() and all((cache / "features" / sym / f"{field}.day.bin").exists() for sym in selected for field in REQUIRED_FIELDS):
            return {sym: str(cache / "features" / sym) for sym in selected}
        local_archive = Path(self.archive_url)
        if local_archive.exists():
            tf_context = tarfile.open(str(local_archive), mode="r:gz")
            response = None
        else:
            req = urllib.request.Request(self.archive_url, headers={"User-Agent": "stockquant-research/1.0"})
            response = urllib.request.urlopen(req, timeout=900)
            tf_context = tarfile.open(fileobj=response, mode="r|gz")
        try:
            tf = tf_context
            for member in tf:
                name = member.name
                if name == "qlib_bin/calendars/day.txt":
                    target = calendar_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tf.extractfile(member)
                    if source:
                        target.write_bytes(source.read())
                    continue
                prefix = "qlib_bin/features/"
                if not name.startswith(prefix) or not name.endswith(".day.bin"):
                    continue
                rel = name[len(prefix):]
                sym, filename = rel.split("/", 1)
                if sym not in selected:
                    continue
                target = cache / "features" / sym / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tf.extractfile(member)
                if source:
                    target.write_bytes(source.read())
                found[rel] = str(target)
                if expected.issubset(set(found)):
                    break
        finally:
            tf_context.close()
            if response is not None:
                response.close()
        return {sym: str(cache / "features" / sym) for sym in selected}

    def build_market_rows(self, symbols: Sequence[str], cache_dir: str) -> Tuple[List[Dict[str, object]], Dict[str, str]]:
        try:
            import numpy as np
        except Exception as exc:
            raise RuntimeError("Qlib archive parser requires numpy") from exc
        cache = Path(cache_dir)
        self.extract_features(symbols, cache_dir)
        calendar = [line.strip() for line in (cache / "day.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
        rows: List[Dict[str, object]] = []
        errors: Dict[str, str] = {}
        for ts_code in symbols:
            sym = qlib_symbol(ts_code)
            values: Dict[str, Dict[str, float]] = {}
            try:
                for field in REQUIRED_FIELDS:
                    path = cache / "features" / sym / f"{field}.day.bin"
                    raw = np.fromfile(str(path), dtype="<f4")
                    if raw.size < 2:
                        raise ValueError(f"empty feature: {field}")
                    start_idx = int(raw[0])
                    arr = raw[1:]
                    values[field] = {
                        calendar[start_idx + i]: float(v)
                        for i, v in enumerate(arr)
                        if 0 <= start_idx + i < len(calendar)
                    }
                dates = sorted(set(values["close"]))
                prev_close = None
                for date in dates:
                    close_adj = values["close"][date]
                    factor = values["factor"].get(date, 1.0)
                    open_px = values["open"][date]
                    high_px = values["high"][date]
                    low_px = values["low"][date]
                    volume = values["volume"].get(date, 0.0)
                    if not all(np.isfinite(v) for v in (close_adj, factor, open_px, high_px, low_px, volume)):
                        continue
                    if min(close_adj, open_px, high_px, low_px) <= 0:
                        continue
                    raw_close = close_adj / factor if factor else close_adj
                    limit_up = False
                    limit_down = False
                    if prev_close and prev_close > 0:
                        ret = open_px / prev_close - 1.0
                        code = ts_code.split(".")[0]
                        rate = 0.20 if code.startswith(("300", "688")) else 0.05 if False else 0.10
                        limit_up = ret >= rate * 0.98
                        limit_down = ret <= -rate * 0.98
                    rows.append(
                        {
                            "date": date,
                            "symbol": ts_code,
                            "open": open_px,
                            "high": high_px,
                            "low": low_px,
                            "close": close_adj,
                            "raw_close": raw_close,
                            "volume": volume,
                            "sector": "Unknown",
                            "market_cap": 0.0,
                            "pe": "",
                            "pb": "",
                            "roe": "",
                            "eps": "",
                            "bps": "",
                            "revenue_growth": "",
                            "profit_growth": "",
                            "gross_margin": "",
                    "forecast_growth": "",
                    "forecast_positive": "",
                    "holder_change": "",
                            "suspended": False,
                            "limit_up": limit_up,
                            "limit_down": limit_down,
                            "is_st": False,
                            "source": "qlib:investment_data",
                            "available_at": f"{date} 15:00:00",
                        }
                    )
                    prev_close = close_adj
            except Exception as exc:
                errors[ts_code] = f"{type(exc).__name__}: {exc}"
        rows.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))
        return rows, errors


def build_fetch_result(rows: List[Dict[str, object]], symbols: Sequence[str], errors: Dict[str, str], cache_dir: str) -> FetchResult:
    metadata = {
        "source": "qlib:investment_data",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "cache_dir": cache_dir,
        "symbols_requested": len(symbols),
        "symbols_loaded": len({str(r["symbol"]) for r in rows}),
        "rows": len(rows),
        "errors": errors,
    }
    warnings = [
        "Qlib archive contains public community data; vendor consistency should still be audited.",
        "Historical membership must be applied separately via membership CSV.",
    ]
    return FetchResult(rows, {s: "" for s in symbols}, errors, warnings, metadata)
