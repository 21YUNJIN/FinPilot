from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from stockquant.data.akshare_provider import FetchResult, normalize_symbol


def _iso_date(value: object) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


def _compact_date(value: str) -> str:
    return value.replace("-", "")


class TushareProvider:
    """Optional Tushare adapter for PIT-aware A-share datasets.

    A Tushare token is required. The adapter uses:
    - stock_basic: industry and listing status
    - daily + adj_factor: prices and forward-adjusted returns
    - daily_basic: daily PE/PB/turnover/market value
    - fina_indicator: announced ROE, joined by announcement date
    - index_weight: historical index membership
    """

    def __init__(self, token: str, pause: float = 0.0):
        if not token:
            raise ValueError("Tushare token is required")
        try:
            import tushare as ts
        except Exception as exc:
            raise RuntimeError("TushareProvider requires: pip install tushare pandas") from exc
        self.pro = ts.pro_api(token)
        self.pause = pause

    def stock_basic(self) -> Dict[str, Dict[str, object]]:
        df = self.pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,industry,list_date,delist_date",
        )
        out: Dict[str, Dict[str, object]] = {}
        for _, row in df.iterrows():
            out[str(row["ts_code"])] = row.to_dict()
        return out


    def stock_basic_all(self) -> Dict[str, Dict[str, object]]:
        """All listing statuses (listed, delisted, paused) to remove survivorship bias."""
        out: Dict[str, Dict[str, object]] = {}
        for status in ("L", "D", "P"):
            try:
                df = self.pro.stock_basic(
                    exchange="",
                    list_status=status,
                    fields="ts_code,symbol,name,industry,list_status,list_date,delist_date",
                )
            except Exception:
                continue
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                out[str(row["ts_code"])] = row.to_dict()
        return out

    def index_weights(self, index_code: str, start: str, end: str) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        start_year = int(start[:4])
        end_year = int(end[:4])
        for year in range(start_year, end_year + 1):
            s = max(start, f"{year}-01-01")
            e = min(end, f"{year}-12-31")
            try:
                df = self.pro.index_weight(
                    index_code=index_code,
                    start_date=_compact_date(s),
                    end_date=_compact_date(e),
                )
            except Exception:
                # No permission: fall back to whatever membership the caller
                # already has (e.g. a DoltHub membership CSV).
                return rows
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    rows.append(
                        {
                            "trade_date": _iso_date(row["trade_date"]),
                            "con_code": str(row["con_code"]),
                            "weight": float(row["weight"]) if row.get("weight") is not None else 0.0,
                        }
                    )
        return rows

    def _membership_by_date(
        self,
        index_code: str,
        trading_dates: Iterable[str],
        start: str,
        end: str,
    ) -> Dict[str, Set[str]]:
        weights = self.index_weights(index_code, start, end)
        if not weights:
            return {}
        by_date: Dict[str, Set[str]] = {}
        for row in weights:
            by_date.setdefault(str(row["trade_date"]), set()).add(str(row["con_code"]))
        current: Set[str] = set()
        out: Dict[str, Set[str]] = {}
        for date in sorted(set(trading_dates)):
            if date in by_date:
                current = set(by_date[date])
            if current:
                out[date] = set(current)
        return out

    def _fundamentals_by_announcement(self, symbol: str, start: str, end: str) -> List[Dict[str, object]]:
        """Point-in-time fundamental snapshots, sorted by announcement date.

        Each ``fina_indicator`` row is a *revision* keyed by (ann_date, end_date),
        so joining by ``ann_date`` (rather than ``end_date``) avoids look-ahead.
        ``update_flag`` is preserved for revision auditing.
        """
        try:
            df = self.pro.fina_indicator(
                ts_code=symbol,
                start_date=_compact_date(start),
                end_date=_compact_date(end),
                fields="ts_code,ann_date,end_date,update_flag,eps,bps,roe,grossprofit_margin,or_yoy,netprofit_yoy",
            )
        except Exception:
            # No permission / transient API error: degrade gracefully so the
            # price+valuation fetch for this symbol still succeeds.
            return []
        if df is None or df.empty:
            return []
        out: List[Dict[str, object]] = []
        for _, row in df.iterrows():
            ann = row.get("ann_date")
            if ann is None:
                continue

            def _f(key: str) -> Optional[float]:
                value = row.get(key)
                try:
                    return float(value) if value is not None else None
                except Exception:
                    return None

            out.append({
                "ann_date": _iso_date(ann),
                "end_date": _iso_date(row.get("end_date")) if row.get("end_date") is not None else "",
                "update_flag": str(row.get("update_flag") or ""),
                "eps": _f("eps"),
                "bps": _f("bps"),
                "roe": _f("roe"),
                "gross_margin": _f("grossprofit_margin"),
                "revenue_growth": _f("or_yoy"),
                "profit_growth": _f("netprofit_yoy"),
            })
        return sorted(out, key=lambda r: str(r["ann_date"]))

    def _forecast_by_announcement(self, symbol: str, start: str, end: str) -> List[Dict[str, object]]:
        try:
            df = self.pro.forecast(
                ts_code=symbol,
                start_date=_compact_date(start),
                end_date=_compact_date(end),
                fields="ts_code,ann_date,end_date,type,p_change_min,p_change_max",
            )
        except Exception:
            return []
        if df is None or df.empty:
            return []
        out: List[Dict[str, object]] = []
        for _, row in df.iterrows():
            ann = row.get("ann_date")
            if ann is None:
                continue
            lo = row.get("p_change_min")
            hi = row.get("p_change_max")
            vals = []
            for value in (lo, hi):
                try:
                    if value is not None:
                        vals.append(float(value))
                except Exception:
                    pass
            growth = sum(vals) / len(vals) if vals else None
            out.append({
                "ann_date": _iso_date(ann),
                "end_date": _iso_date(row.get("end_date")) if row.get("end_date") is not None else "",
                "type": str(row.get("type") or ""),
                "forecast_growth": growth,
                "forecast_positive": 1.0 if growth is not None and growth > 0 else (0.0 if growth is not None else None),
            })
        return sorted(out, key=lambda r: str(r["ann_date"]))

    def fetch_fundamental_versions(self, symbol: str, start: str, end: str) -> Dict[str, List[Dict[str, object]]]:
        """Raw PIT-versioned fundamentals for a symbol (for auditing/versioning)."""
        return {
            "fina_indicator": self._fundamentals_by_announcement(symbol, start, end),
            "forecast": self._forecast_by_announcement(symbol, start, end),
        }

    def _daily_rows(
        self,
        symbol: str,
        info: Dict[str, object],
        start: str,
        end: str,
    ) -> List[Dict[str, object]]:
        daily = self.pro.daily(ts_code=symbol, start_date=_compact_date(start), end_date=_compact_date(end))
        if daily is None or daily.empty:
            return []
        extra = self.pro.daily_basic(
            ts_code=symbol,
            start_date=_compact_date(start),
            end_date=_compact_date(end),
            fields="trade_date,pe,pb,total_mv,turnover_rate",
        )
        adj = self.pro.adj_factor(ts_code=symbol, start_date=_compact_date(start), end_date=_compact_date(end))
        daily = daily.merge(extra, on="trade_date", how="left") if extra is not None and not extra.empty else daily
        daily = daily.merge(adj[["trade_date", "adj_factor"]], on="trade_date", how="left") if adj is not None and not adj.empty else daily
        daily = daily.sort_values("trade_date")
        adj_values = [float(v) for v in daily.get("adj_factor", [1.0] * len(daily)).ffill().fillna(1.0).tolist()]
        base_adj = adj_values[0] if adj_values else 1.0
        fund_events = self._fundamentals_by_announcement(symbol, start, end)
        forecast_events = self._forecast_by_announcement(symbol, start, end)
        fund_idx = 0
        forecast_idx = 0
        current_fund: Dict[str, object] = {}
        current_forecast: Dict[str, object] = {}
        rows: List[Dict[str, object]] = []
        prev_close: Optional[float] = None
        industry = str(info.get("industry") or "Unknown")
        name = str(info.get("name") or "")

        def _cell(value: object) -> object:
            return "" if value is None else value

        for pos, (_, row) in enumerate(daily.iterrows()):
            date = _iso_date(row["trade_date"])
            while fund_idx < len(fund_events) and str(fund_events[fund_idx]["ann_date"]) <= date:
                current_fund = fund_events[fund_idx]
                fund_idx += 1
            while forecast_idx < len(forecast_events) and str(forecast_events[forecast_idx]["ann_date"]) <= date:
                current_forecast = forecast_events[forecast_idx]
                forecast_idx += 1
            # Availability of the announcement-derived fields carried on this row:
            # the *latest* announcement date already reflected in the row.  A
            # date-only stamp means "published that day, assume after the close",
            # so the factor engine will only use it from the next session on --
            # which is exactly the conservative reading for A-share disclosures.
            fund_avail = max(
                [str(current_fund.get("ann_date") or ""), str(current_forecast.get("ann_date") or "")]
            ) if (current_fund or current_forecast) else ""
            adj_factor = adj_values[pos] if pos < len(adj_values) and adj_values[pos] else 1.0
            factor = adj_factor / base_adj if base_adj else 1.0
            open_px = float(row["open"]) * factor
            high_px = float(row["high"]) * factor
            low_px = float(row["low"]) * factor
            close_px = float(row["close"]) * factor
            volume = float(row.get("vol", 0.0) or 0.0) * 100.0
            limit_up = bool(prev_close and open_px / prev_close - 1.0 >= 0.095)
            limit_down = bool(prev_close and open_px / prev_close - 1.0 <= -0.095)
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": open_px,
                    "high": high_px,
                    "low": low_px,
                    "close": close_px,
                    "volume": volume,
                    "sector": industry,
                    "market_cap": float(row.get("total_mv", 0.0) or 0.0) * 10000.0,
                    "pe": row.get("pe", ""),
                    "pb": row.get("pb", ""),
                    "roe": _cell(current_fund.get("roe")),
                    "eps": _cell(current_fund.get("eps")),
                    "bps": _cell(current_fund.get("bps")),
                    "revenue_growth": _cell(current_fund.get("revenue_growth")),
                    "profit_growth": _cell(current_fund.get("profit_growth")),
                    "gross_margin": _cell(current_fund.get("gross_margin")),
                    "forecast_growth": _cell(current_forecast.get("forecast_growth")),
                    "forecast_positive": _cell(current_forecast.get("forecast_positive")),
                    "suspended": False,
                    "limit_up": limit_up,
                    "limit_down": limit_down,
                    "is_st": "ST" in name.upper(),
                    "source": "tushare:hfq",
                    "available_at": f"{date} 15:00:00",
                    "fundamentals_available_at": fund_avail,
                }
            )
            prev_close = close_px
        return rows

    def fetch_dataset(
        self,
        index_code: Optional[str],
        symbols: Optional[Iterable[str]],
        start: str,
        end: str,
        limit: Optional[int] = None,
    ) -> FetchResult:
        basic = self.stock_basic()
        if symbols:
            chosen = {str(s): basic.get(str(s), {}).get("name", "") for s in symbols}
        elif index_code:
            weights = self.index_weights(index_code, start, end)
            codes = sorted({str(r["con_code"]) for r in weights})
            if limit:
                codes = codes[:limit]
            chosen = {s: str(basic.get(s, {}).get("name", "")) for s in codes}
        else:
            raise ValueError("Provide symbols or index_code")

        all_rows: List[Dict[str, object]] = []
        errors: Dict[str, str] = {}
        for symbol in chosen:
            try:
                all_rows.extend(self._daily_rows(symbol, basic.get(symbol, {}), start, end))
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"

        warnings = [
            "Tushare ROE is joined by announcement date; verify vendor revision semantics before production use.",
            "Forward-adjusted prices are used to reduce look-ahead risk, but corporate-action handling still requires validation.",
        ]
        if index_code:
            membership = self._membership_by_date(index_code, [str(r["date"]) for r in all_rows], start, end)
            if membership:
                # Fail closed: a session with no membership row means the index
                # composition is *unknown* there (usually because the index_weight
                # history starts later than the requested window).  The old code
                # kept every symbol in that case, which silently turned the early
                # part of the backtest into an unfiltered, survivorship-biased
                # universe.
                filtered = []
                dropped = 0
                for row in all_rows:
                    members = membership.get(str(row["date"]))
                    if members is not None and str(row["symbol"]) in members:
                        filtered.append(row)
                    else:
                        dropped += 1
                all_rows = filtered
                if dropped:
                    first_member_date = min(membership)
                    warnings.append(
                        f"{dropped} rows dropped because index membership is unknown on their date "
                        f"(first covered session: {first_member_date}); the universe before that is "
                        "treated as empty rather than unfiltered"
                    )
            else:
                warnings.append("No index_weight history was returned; survivorship bias remains possible.")
        all_rows.sort(key=lambda r: (str(r["date"]), str(r["symbol"])))
        metadata = {
            "source": "tushare",
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "start": start,
            "end": end,
            "index_code": index_code,
            "symbols_requested": len(chosen),
            "symbols_loaded": len({str(r["symbol"]) for r in all_rows}),
            "rows": len(all_rows),
            "errors": errors,
            "warnings": warnings,
        }
        return FetchResult(rows=all_rows, symbols=chosen, errors=errors, warnings=warnings, metadata=metadata)
