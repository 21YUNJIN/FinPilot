from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from stockquant.data.akshare_provider import FetchResult
from stockquant.data.membership import deterministic_sample, membership_union


DOLT_API = "https://www.dolthub.com/api/v1alpha1/chenditc/investment_data/master"


class DoltHubClient:
    def __init__(self, retries: int = 3, retry_sleep: float = 1.0, timeout: int = 120):
        self.retries = max(1, retries)
        self.retry_sleep = retry_sleep
        self.timeout = timeout

    def query(self, sql: str) -> List[Dict[str, object]]:
        url = DOLT_API + "?q=" + urllib.parse.quote(sql)
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "stockquant-research/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    payload = json.load(response)
                status = payload.get("query_execution_status")
                if status != "Success":
                    raise RuntimeError(payload.get("query_execution_message") or status)
                return list(payload.get("rows", []))
            except Exception as exc:
                last_error = exc
                time.sleep(self.retry_sleep * (attempt + 1))
        raise RuntimeError(f"DoltHub query failed: {last_error}")


def to_dolt_symbol(ts_code: str) -> str:
    code, _, exchange = ts_code.partition(".")
    return f"{exchange.upper()}{code}"


def _month_ranges(start: str, end: str) -> List[Tuple[str, str]]:
    start_year, start_month = int(start[:4]), int(start[5:7])
    end_year, end_month = int(end[:4]), int(end[5:7])
    out = []
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        if m == 12:
            next_y, next_m = y + 1, 1
        else:
            next_y, next_m = y, m + 1
        s = f"{y:04d}-{m:02d}-01"
        e = f"{next_y:04d}-{next_m:02d}-01"
        out.append((s, e))
        y, m = next_y, next_m
    return out


class DoltHubProvider:
    def __init__(self, client: Optional[DoltHubClient] = None):
        self.client = client or DoltHubClient()

    def fetch_membership(
        self,
        index_code: str = "399300.SZ",
        start: str = "2015-01-01",
        end: str = "2025-12-31",
        batch_dates: int = 3,
    ) -> Dict[str, Set[str]]:
        monthly_dates: List[str] = []
        start_year, end_year = int(start[:4]), int(end[:4])
        for year in range(start_year, end_year + 1):
            s = max(start, f"{year}-01-01")
            e = min(end, f"{year}-12-31")
            sql = (
                "select year(trade_date) as y, month(trade_date) as m, max(trade_date) as d "
                "from ts_index_weight "
                f"where index_code='{index_code}' and trade_date between '{s}' and '{e}' "
                "group by year(trade_date), month(trade_date) order by y,m"
            )
            rows = self.client.query(sql)
            print(f"membership dates {year}: {len(rows)} months")
            for row in rows:
                monthly_dates.append(str(row["d"]))
        membership: Dict[str, Set[str]] = {}
        for i in range(0, len(monthly_dates), batch_dates):
            chunk = monthly_dates[i:i + batch_dates]
            quoted = ",".join(f"'{d}'" for d in chunk)
            sql = (
                "select stock_code,trade_date,weight from ts_index_weight "
                f"where index_code='{index_code}' and trade_date in ({quoted}) "
                "order by trade_date,stock_code"
            )
            rows = self.client.query(sql)
            print(f"membership batch {i // batch_dates + 1}: {len(rows)} rows")
            for row in rows:
                date = str(row["trade_date"])
                symbol = str(row["stock_code"]).upper()
                membership.setdefault(date, set()).add(symbol)
        return membership

    def fetch_prices(
        self,
        symbols: Sequence[str],
        start: str,
        end: str,
        membership: Optional[Dict[str, Set[str]]] = None,
    ) -> FetchResult:
        rows: List[Dict[str, object]] = []
        errors: Dict[str, str] = {}
        for index, symbol in enumerate(symbols):
            if index == 0 or (index + 1) % 10 == 0:
                print(f"price fetch {index + 1}/{len(symbols)}")
            dolt_symbol = to_dolt_symbol(symbol)
            offset = 0
            symbol_rows: List[Dict[str, object]] = []
            try:
                while True:
                    sql = (
                        "select tradedate,high,low,open,close,adjclose,volume,amount "
                        "from final_a_stock_eod_price "
                        f"where symbol='{dolt_symbol}' and tradedate between '{start}' and '{end}' "
                        f"order by tradedate limit 1000 offset {offset}"
                    )
                    batch = self.client.query(sql)
                    if not batch:
                        break
                    for row in batch:
                        raw_close = float(row["close"])
                        adj_close = float(row["adjclose"])
                        factor = adj_close / raw_close if raw_close > 0 else 1.0
                        symbol_rows.append(
                            {
                                "date": str(row["tradedate"]),
                                "symbol": symbol,
                                "open": float(row["open"]) * factor,
                                "high": float(row["high"]) * factor,
                                "low": float(row["low"]) * factor,
                                "close": adj_close,
                                "raw_close": raw_close,
                                "volume": float(row["volume"] or 0.0),
                                "sector": "Unknown",
                                "market_cap": 0.0,
                                "pe": "",
                                "pb": "",
                                "roe": "",
                                "eps": "",
                                "bps": "",
                                "suspended": False,
                                "limit_up": False,
                                "limit_down": False,
                                "is_st": False,
                                "source": "dolt:final_a_stock_eod_price",
                                "available_at": f"{str(row['tradedate'])} 15:00:00",
                            }
                        )
                    if len(batch) < 1000:
                        break
                    offset += 1000
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
            if symbol_rows:
                symbol_rows.sort(key=lambda r: str(r["date"]))
                prev_close = None
                for row in symbol_rows:
                    px = float(row["open"])
                    if prev_close and prev_close > 0:
                        ret = px / prev_close - 1.0
                        row["limit_up"] = ret >= 0.095
                        row["limit_down"] = ret <= -0.095
                    prev_close = float(row["close"])
                rows.extend(symbol_rows)
        if not rows:
            return FetchResult([], {s: "" for s in symbols}, errors, ["No DoltHub price rows returned."], {})
        # Align missing sessions to suspensions within each symbol's observed range.
        all_dates = sorted({str(r["date"]) for r in rows})
        by_symbol: Dict[str, List[Dict[str, object]]] = {}
        for row in rows:
            by_symbol.setdefault(str(row["symbol"]), []).append(row)
        aligned: List[Dict[str, object]] = []
        for symbol, symbol_rows in by_symbol.items():
            by_date = {str(r["date"]): r for r in symbol_rows}
            dates = sorted(by_date)
            first, last = dates[0], dates[-1]
            prev = None
            for date in all_dates:
                if date < first or date > last:
                    continue
                if date in by_date:
                    prev = by_date[date]
                    aligned.append(prev)
                elif prev is not None:
                    fill = dict(prev)
                    fill.update(
                        {
                            "date": date,
                            "open": prev["close"],
                            "high": prev["close"],
                            "low": prev["close"],
                            "close": prev["close"],
                            "raw_close": prev["raw_close"],
                            "volume": 0.0,
                            "suspended": True,
                            "limit_up": False,
                            "limit_down": False,
                            "source": "dolt:calendar-fill",
                            "available_at": f"{date} 15:00:00",
                        }
                    )
                    aligned.append(fill)
        aligned.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))
        metadata = {
            "source": "dolt:chenditc/investment_data",
            "start": start,
            "end": end,
            "symbols_requested": len(symbols),
            "symbols_loaded": len(by_symbol),
            "rows": len(aligned),
            "errors": errors,
            "membership_snapshots": len(membership or {}),
        }
        warnings = [
            "DoltHub mirror contains historical index weights and delisted name data, but vendor consistency should still be audited.",
            "Adjusted open/high/low are reconstructed using adjclose/close ratio because only adjusted close is available.",
        ]
        return FetchResult(aligned, {s: "" for s in symbols}, errors, warnings, metadata)

    def build_dataset(
        self,
        index_code: str,
        start: str,
        end: str,
        sample_size: int = 60,
        seed: int = 20261003,
    ) -> Tuple[FetchResult, Dict[str, Set[str]]]:
        membership = self.fetch_membership(index_code=index_code, start=start, end=end)
        union = membership_union(membership)
        sample = deterministic_sample(union, limit=sample_size, seed=seed)
        result = self.fetch_prices(sample, start=start, end=end, membership=membership)
        result.metadata["membership_union"] = len(union)
        result.metadata["sample_size"] = len(sample)
        result.metadata["sample_seed"] = seed
        result.warnings.append(
            "Universe is a deterministic sample from the historical index union, not current index members."
        )
        return result, membership
