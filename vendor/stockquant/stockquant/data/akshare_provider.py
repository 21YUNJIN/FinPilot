from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from stockquant.data.csv_loader import write_csv


@dataclass
class FetchResult:
    rows: List[Dict[str, object]]
    symbols: Dict[str, str]
    errors: Dict[str, str]
    warnings: List[str]
    metadata: Dict[str, object]


def normalize_symbol(code: str, exchange: str = "") -> str:
    code = str(code).strip().lower().replace("sh", "").replace("sz", "").replace("bj", "")
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        raise ValueError(f"invalid A-share code: {code}")
    if exchange:
        if "上海" in exchange or "Shanghai" in exchange:
            return f"{code}.SH"
        if "北京" in exchange or "Beijing" in exchange:
            return f"{code}.BJ"
        return f"{code}.SZ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _code_only(symbol: str) -> str:
    return symbol.split(".")[0]


def _limit_rate(symbol: str, is_st: bool = False) -> float:
    code = _code_only(symbol)
    if is_st:
        return 0.05
    if code.startswith(("688", "300")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


def _stamp_fundamental_availability(row: Dict[str, object], announce_date: object) -> None:
    """Record when the announcement-derived fields on ``row`` became knowable.

    Several enrichment passes (financials -> forecasts -> holder counts) write to
    the same row, so keep the *latest* announcement date: a single stamp must
    cover every announcement-sourced field on that row, and taking the max is the
    conservative choice.  Date-only stamps are read by the factor engine as
    "published after the close", i.e. usable from the next session.
    """
    text = str(announce_date or "").strip()[:10]
    if not text:
        return
    existing = str(row.get("fundamentals_available_at") or "")
    row["fundamentals_available_at"] = max(existing, text)


class AkshareProvider:
    """Free A-share data adapter.

    The adapter intentionally maps only fields that AKShare exposes reliably.
    Historical fundamental fields are left empty rather than being replaced by
    current-day values, which would introduce look-ahead bias.
    """

    def __init__(self, max_workers: int = 2, retries: int = 3, retry_sleep: float = 1.5, pause: float = 0.3):
        try:
            import akshare as ak  # noqa: F401
        except Exception as exc:
            raise RuntimeError("AkshareProvider requires: pip install akshare pandas") from exc
        self.max_workers = max(1, max_workers)
        self.retries = max(1, retries)
        self.retry_sleep = max(0.0, retry_sleep)
        self.pause = max(0.0, pause)

    def index_constituents(self, index_code: str = "000300") -> Dict[str, str]:
        import akshare as ak

        df = ak.index_stock_cons_csindex(symbol=index_code)
        if df is None or df.empty:
            return {}
        mapping: Dict[str, str] = {}
        for _, row in df.iterrows():
            exchange = str(row.get("交易所", ""))
            symbol = normalize_symbol(str(row.get("成分券代码", "")), exchange)
            name = str(row.get("成分券名称", ""))
            mapping[symbol] = name
        return mapping

    def _fetch_one(
        self,
        symbol: str,
        name: str,
        start: str,
        end: str,
        adjust: str,
    ) -> Tuple[str, List[Dict[str, object]]]:
        import time
        import akshare as ak

        code = _code_only(symbol)
        prefixed = ("sh" if code.startswith(("6", "9")) else "bj" if code.startswith(("4", "8")) else "sz") + code
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                try:
                    df = ak.stock_zh_a_daily(
                        symbol=prefixed,
                        start_date=start,
                        end_date=end,
                        adjust=adjust,
                    )
                    volume_multiplier = 1.0
                except Exception:
                    df = ak.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=start,
                        end_date=end,
                        adjust=adjust,
                    )
                    volume_multiplier = 100.0
                if df is None or df.empty:
                    raise ValueError("empty result")
                break
            except Exception as exc:
                last_error = exc
                time.sleep(self.retry_sleep * (attempt + 1))
        else:
            raise RuntimeError(str(last_error) if last_error else "fetch failed")

        raw_close_by_date: Dict[str, float] = {}
        try:
            raw_df = ak.stock_zh_a_daily(
                symbol=prefixed,
                start_date=start,
                end_date=end,
                adjust="",
            )
            if raw_df is not None and not raw_df.empty and "date" in raw_df.columns:
                for _, raw_row in raw_df.iterrows():
                    raw_date = raw_row["date"]
                    raw_date = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date)[:10]
                    raw_close_by_date[raw_date] = float(raw_row["close"])
        except Exception:
            raw_close_by_date = {}

        # Normalize either the English `stock_zh_a_daily` schema or the Chinese
        # `stock_zh_a_hist` schema into one internal format.
        is_st = "ST" in name.upper()
        rate = _limit_rate(symbol, is_st)
        rows: List[Dict[str, object]] = []
        prev_close: Optional[float] = None
        for _, row in df.iterrows():
            if "date" in df.columns:
                date_value = row["date"]
                if hasattr(date_value, "isoformat"):
                    date = date_value.isoformat()
                else:
                    date = str(date_value)[:10]
                open_px = float(row["open"])
                high_px = float(row["high"])
                low_px = float(row["low"])
                close_px = float(row["close"])
                volume = float(row.get("volume", 0.0) or 0.0)
                outstanding = float(row.get("outstanding_share", 0.0) or 0.0)
                # Market cap must use the RAW price: multiplying an adjusted (hfq)
                # close by shares outstanding inflates it by the cumulative
                # adjustment factor (115x for 000001.SZ in 2018, 200x for some
                # names), which corrupts the size factor, the cap-weighted
                # benchmark and any market-cap filter.
                raw_px = raw_close_by_date.get(date)
                market_cap = (raw_px if raw_px and raw_px > 0 else close_px) * outstanding
            else:
                date = str(row["日期"])[:10]
                open_px = float(row["开盘"])
                high_px = float(row["最高"])
                low_px = float(row["最低"])
                close_px = float(row["收盘"])
                volume = float(row.get("成交量", 0.0) or 0.0) * volume_multiplier
                market_cap = 0.0
            limit_up = False
            limit_down = False
            if prev_close and prev_close > 0:
                open_ret = open_px / prev_close - 1.0
                limit_up = open_ret >= rate * 0.98
                limit_down = open_ret <= -rate * 0.98
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": open_px,
                    "high": high_px,
                    "low": low_px,
                    "close": close_px,
                    "volume": volume,
                    "sector": "Unknown",
                    "market_cap": market_cap,
                    "pe": "",
                    "pb": "",
                    "roe": "",
                    "suspended": False,
                    "limit_up": limit_up,
                    "limit_down": limit_down,
                    "is_st": is_st,
                    "source": f"akshare:{adjust or 'raw'}",
                    "raw_close": raw_close_by_date.get(date, close_px),
                    "eps": "",
                    "bps": "",
                    "revenue_growth": "",
                    "profit_growth": "",
                    "gross_margin": "",
                    "forecast_growth": "",
                    "forecast_positive": "",
                    "holder_change": "",
                    "available_at": f"{date} 15:00:00",
                }
            )
            prev_close = close_px
        if self.pause:
            time.sleep(self.pause)
        return symbol, rows

    def performance_events(self, start: str, end: str, annual_only: bool = True) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, str]]:
        import akshare as ak

        reports: List[str] = []
        start_year = int(start[:4])
        end_year = int(end[:4])
        for year in range(start_year - 2, end_year + 1):
            if annual_only:
                reports.append(f"{year}1231")
            else:
                reports.extend([f"{year}0331", f"{year}0630", f"{year}0930", f"{year}1231"])
        by_symbol: Dict[str, List[Dict[str, object]]] = {}
        errors: Dict[str, str] = {}
        for period in reports:
            try:
                df = ak.stock_yjbb_em(date=period)
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    code = str(row.get("股票代码", "")).zfill(6)
                    try:
                        symbol = normalize_symbol(code)
                    except Exception:
                        continue
                    announce = str(row.get("最新公告日期", ""))[:10]
                    if not announce or announce == "nan":
                        continue
                    industry = str(row.get("所处行业") or "Unknown").strip()
                    try:
                        roe_pct = float(row.get("净资产收益率"))
                        roe = roe_pct / 100.0
                    except Exception:
                        roe = None
                    try:
                        eps = float(row.get("每股收益"))
                    except Exception:
                        eps = None
                    try:
                        bps = float(row.get("每股净资产"))
                    except Exception:
                        bps = None
                    try:
                        revenue_growth = float(row.get("营业总收入-同比增长")) / 100.0
                    except Exception:
                        revenue_growth = None
                    try:
                        profit_growth = float(row.get("净利润-同比增长")) / 100.0
                    except Exception:
                        profit_growth = None
                    try:
                        gross_margin = float(row.get("销售毛利率")) / 100.0
                    except Exception:
                        gross_margin = None
                    by_symbol.setdefault(symbol, []).append(
                        {
                            "report_period": period,
                            "announce_date": announce,
                            "industry": industry,
                            "roe": roe,
                            "eps": eps,
                            "bps": bps,
                            "revenue_growth": revenue_growth,
                            "profit_growth": profit_growth,
                            "gross_margin": gross_margin,
                        }
                    )
            except Exception as exc:
                errors[period] = f"{type(exc).__name__}: {exc}"
            count = len(df) if df is not None and not df.empty else 0
            print(f"financial report {period}: {count} rows")
        for symbol in by_symbol:
            by_symbol[symbol].sort(key=lambda x: (str(x["announce_date"]), str(x["report_period"])))
        return by_symbol, errors

    def enrich_with_financials(
        self,
        result: FetchResult,
        start: str,
        end: str,
        annual_only: bool = True,
    ) -> FetchResult:
        events, errors = self.performance_events(start, end, annual_only=annual_only)
        if not events:
            result.warnings.append("No AKShare performance-report events were loaded; ROE and industry remain missing.")
            result.metadata["financial_errors"] = errors
            return result
        by_symbol_rows: Dict[str, List[Dict[str, object]]] = {}
        for row in result.rows:
            by_symbol_rows.setdefault(str(row["symbol"]), []).append(row)
        for symbol, rows in by_symbol_rows.items():
            symbol_events = events.get(symbol, [])
            if not symbol_events:
                continue
            rows.sort(key=lambda r: str(r["date"]))
            idx = 0
            current = None
            for row in rows:
                date = str(row["date"])
                while idx < len(symbol_events) and str(symbol_events[idx]["announce_date"]) <= date:
                    current = symbol_events[idx]
                    idx += 1
                if current is not None:
                    if current.get("roe") is not None:
                        row["roe"] = current["roe"]
                    if current.get("eps") is not None:
                        row["eps"] = current["eps"]
                    if current.get("bps") is not None:
                        row["bps"] = current["bps"]
                    if current.get("revenue_growth") is not None:
                        row["revenue_growth"] = current["revenue_growth"]
                    if current.get("profit_growth") is not None:
                        row["profit_growth"] = current["profit_growth"]
                    if current.get("gross_margin") is not None:
                        row["gross_margin"] = current["gross_margin"]
                    raw_close = float(row.get("raw_close") or row.get("close") or 0.0)
                    eps = current.get("eps")
                    bps = current.get("bps")
                    if raw_close > 0 and eps is not None and float(eps) > 0:
                        row["pe"] = raw_close / float(eps)
                    if raw_close > 0 and bps is not None and float(bps) > 0:
                        row["pb"] = raw_close / float(bps)
                    _stamp_fundamental_availability(row, current.get("announce_date"))
                    industry = str(current.get("industry") or "")
                    if industry and industry != "None":
                        row["sector"] = industry
        result.metadata["financial_events_symbols"] = len(events)
        result.metadata["financial_errors"] = errors
        result.warnings = [
            w for w in result.warnings
            if "historical PIT fundamentals" not in w
            and "Sector is set to Unknown" not in w
        ]
        result.warnings.append(
            "AKShare ROE, industry, EPS and BPS are joined by the latest announcement date returned by Eastmoney; PE/PB use annual cumulative EPS/BPS and unadjusted close, not institutional TTM data."
        )
        result.warnings.append(
            "Industry labels reflect the latest announced report available at each date and may lag actual reclassifications."
        )
        return result

    @staticmethod
    def _forecast_signal(value: object) -> float:
        text = str(value or "")
        if any(k in text for k in ("预增", "略增", "扭亏", "续盈")):
            return 1.0
        if any(k in text for k in ("预减", "略减", "首亏", "续亏")):
            return -1.0
        return 0.0

    def fetch_forecast_events(self, start: str, end: str) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, str]]:
        import akshare as ak

        start_year, end_year = int(start[:4]), int(end[:4])
        periods = []
        for year in range(start_year - 1, end_year + 1):
            periods.extend([f"{year}0331", f"{year}0630", f"{year}0930", f"{year}1231"])
        by_symbol: Dict[str, List[Dict[str, object]]] = {}
        errors: Dict[str, str] = {}
        for period in periods:
            try:
                df = ak.stock_yjyg_em(date=period)
                if df is None or df.empty:
                    continue
                preferred = df[df["预测指标"].astype(str).str.contains("归属于上市公司股东的净利润", na=False)]
                use = preferred if not preferred.empty else df
                for _, row in use.iterrows():
                    code = str(row.get("股票代码", "")).zfill(6)
                    try:
                        symbol = normalize_symbol(code)
                    except Exception:
                        continue
                    announce = str(row.get("公告日期", ""))[:10]
                    if not announce or announce == "nan":
                        continue
                    try:
                        growth = float(row.get("业绩变动幅度")) / 100.0
                    except Exception:
                        growth = None
                    signal = self._forecast_signal(row.get("预告类型"))
                    by_symbol.setdefault(symbol, []).append(
                        {
                            "announce_date": announce,
                            "report_period": period,
                            "forecast_growth": growth,
                            "forecast_positive": signal,
                        }
                    )
            except Exception as exc:
                errors[period] = f"{type(exc).__name__}: {exc}"
            count = len(df) if df is not None and not df.empty else 0
            print(f"forecast report {period}: {count} rows")
        for symbol in by_symbol:
            by_symbol[symbol].sort(key=lambda x: (str(x["announce_date"]), str(x["report_period"])))
        return by_symbol, errors

    def enrich_with_forecasts(self, result: FetchResult, start: str, end: str) -> FetchResult:
        events, errors = self.fetch_forecast_events(start, end)
        if not events:
            result.warnings.append("No earnings forecast events were loaded.")
            result.metadata["forecast_errors"] = errors
            return result
        by_symbol_rows: Dict[str, List[Dict[str, object]]] = {}
        for row in result.rows:
            by_symbol_rows.setdefault(str(row["symbol"]), []).append(row)
        for symbol, rows in by_symbol_rows.items():
            symbol_events = events.get(symbol, [])
            if not symbol_events:
                continue
            rows.sort(key=lambda r: str(r["date"]))
            idx = 0
            current = None
            for row in rows:
                date = str(row["date"])
                while idx < len(symbol_events) and str(symbol_events[idx]["announce_date"]) <= date:
                    current = symbol_events[idx]
                    idx += 1
                if current is not None:
                    if current.get("forecast_growth") is not None:
                        row["forecast_growth"] = current["forecast_growth"]
                    row["forecast_positive"] = current["forecast_positive"]
                    _stamp_fundamental_availability(row, current.get("announce_date"))
        result.metadata["forecast_events_symbols"] = len(events)
        result.metadata["forecast_errors"] = errors
        result.warnings.append("Earnings forecasts are joined by announcement date; forecast revisions and cancellations may require additional handling.")
        return result

    def _fetch_holder_one(self, symbol: str) -> Tuple[str, List[Dict[str, object]]]:
        import time
        import akshare as ak

        last_error = None
        for attempt in range(self.retries):
            try:
                df = ak.stock_zh_a_gdhs_detail_em(symbol=symbol.split(".")[0])
                if df is None or df.empty:
                    return symbol, []
                events = []
                for _, row in df.iterrows():
                    announce = str(row.get("股东户数公告日期", ""))[:10]
                    if not announce or announce == "nan":
                        continue
                    try:
                        change_pct = float(str(row.get("股东户数-增减比例", "")).replace("%", ""))
                        holder_change = -change_pct / 100.0
                    except Exception:
                        holder_change = None
                    events.append({"announce_date": announce, "holder_change": holder_change})
                events.sort(key=lambda x: str(x["announce_date"]))
                return symbol, events
            except Exception as exc:
                last_error = exc
                time.sleep(self.retry_sleep * (attempt + 1))
        raise RuntimeError(str(last_error) if last_error else "holder fetch failed")

    def enrich_with_holders(self, result: FetchResult, symbols: Optional[Iterable[str]] = None, workers: int = 4) -> FetchResult:
        selected = list(symbols or sorted({str(r["symbol"]) for r in result.rows}))
        events: Dict[str, List[Dict[str, object]]] = {}
        errors: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self._fetch_holder_one, symbol): symbol for symbol in selected}
            for done, future in enumerate(as_completed(futures), start=1):
                symbol = futures[future]
                try:
                    _, symbol_events = future.result()
                    events[symbol] = symbol_events
                except Exception as exc:
                    errors[symbol] = f"{type(exc).__name__}: {exc}"
                if done == 1 or done % 10 == 0:
                    print(f"holder fetch {done}/{len(selected)}")
        by_symbol_rows: Dict[str, List[Dict[str, object]]] = {}
        for row in result.rows:
            by_symbol_rows.setdefault(str(row["symbol"]), []).append(row)
        for symbol, rows in by_symbol_rows.items():
            symbol_events = events.get(symbol, [])
            if not symbol_events:
                continue
            rows.sort(key=lambda r: str(r["date"]))
            idx = 0
            current = None
            for row in rows:
                date = str(row["date"])
                while idx < len(symbol_events) and str(symbol_events[idx]["announce_date"]) <= date:
                    current = symbol_events[idx]
                    idx += 1
                if current is not None and current.get("holder_change") is not None:
                    row["holder_change"] = current["holder_change"]
                    _stamp_fundamental_availability(row, current.get("announce_date"))
        result.metadata["holder_events_symbols"] = len(events)
        result.metadata["holder_errors"] = errors
        result.warnings.append("Shareholder counts are joined by announcement date; negative change ratios are converted to a positive concentration score.")
        return result

    def fetch_daily(
        self,
        symbols: Dict[str, str],
        start: str,
        end: str,
        adjust: str = "qfq",
        align_calendar: bool = True,
    ) -> FetchResult:
        rows: List[Dict[str, object]] = []
        errors: Dict[str, str] = {}
        by_symbol: Dict[str, List[Dict[str, object]]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._fetch_one, symbol, name, start, end, adjust): symbol
                for symbol, name in symbols.items()
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    _, symbol_rows = future.result()
                    if symbol_rows:
                        by_symbol[symbol] = symbol_rows
                    else:
                        errors[symbol] = "empty result"
                except Exception as exc:
                    errors[symbol] = f"{type(exc).__name__}: {exc}"

        if align_calendar and by_symbol:
            all_dates = sorted({str(r["date"]) for symbol_rows in by_symbol.values() for r in symbol_rows})
            for symbol, symbol_rows in by_symbol.items():
                by_date = {str(r["date"]): r for r in symbol_rows}
                own_dates = sorted(by_date)
                first, last = own_dates[0], own_dates[-1]
                prev_close: Optional[float] = None
                for date in all_dates:
                    if date < first or date > last:
                        continue
                    if date in by_date:
                        rows.append(by_date[date])
                        prev_close = float(by_date[date]["close"])
                    elif prev_close is not None:
                        rows.append(
                            {
                                "date": date,
                                "symbol": symbol,
                                "open": prev_close,
                                "high": prev_close,
                                "low": prev_close,
                                "close": prev_close,
                                "volume": 0.0,
                                "sector": "Unknown",
                                "market_cap": 0.0,
                                "pe": "",
                                "pb": "",
                                "roe": "",
                                "suspended": True,
                                "limit_up": False,
                                "limit_down": False,
                                "is_st": False,
                                "source": "akshare:calendar-fill",
                                "raw_close": prev_close,
                                "eps": "",
                                "bps": "",
                                "revenue_growth": "",
                                "profit_growth": "",
                                "gross_margin": "",
                    "revenue_growth": "",
                    "profit_growth": "",
                    "gross_margin": "",
                                "available_at": f"{date} 15:00:00",
                            }
                        )
            rows.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))
        else:
            for symbol_rows in by_symbol.values():
                rows.extend(symbol_rows)
            rows.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))

        warnings = [
            "AKShare daily endpoints do not provide historical PIT fundamentals; value/quality factors are neutral for this dataset.",
            "The index constituent endpoint returns current constituents, so a historical backtest on this file still has survivorship bias.",
            "Sector is set to Unknown unless an external mapping is supplied; sector caps are disabled for Unknown-only data.",
        ]
        metadata = {
            "source": "akshare",
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "start": start,
            "end": end,
            "adjust": adjust,
            "symbols_requested": len(symbols),
            "symbols_loaded": len(by_symbol),
            "rows": len(rows),
            "errors": errors,
            "warnings": warnings,
            "calendar_aligned": align_calendar,
        }
        return FetchResult(rows=rows, symbols=symbols, errors=errors, warnings=warnings, metadata=metadata)


def save_fetch_result(result: FetchResult, data_path: str, metadata_path: Optional[str] = None) -> None:
    write_csv(result.rows, data_path)
    if metadata_path:
        Path(metadata_path).parent.mkdir(parents=True, exist_ok=True)
        Path(metadata_path).write_text(
            json.dumps(result.metadata, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
