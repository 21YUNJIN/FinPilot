"""Free PIT-safe A-share fundamentals via AKShare's Sina endpoints.

Replaces the (paid) Tushare financial interfaces with free equivalents:

* ``stock_financial_analysis_indicator`` - EPS, BPS, ROE, revenue/profit growth,
  gross margin and more, keyed by *report period*.

Sina exposes the report period but **not** the announcement date.  To stay
point-in-time safe we map each report period to the statutory disclosure
deadline for A-share periodic reports (证券法 / 交易所信息披露规则):

    Q1  (03-31) -> 04-30 same year
    H1  (06-30) -> 08-31 same year
    Q3  (09-30) -> 10-31 same year
    FY  (12-31) -> 04-30 next year

Using the deadline is *conservative*: it never assumes data is available
earlier than it could have been, so it cannot introduce look-ahead bias.  If a
source with the real ``ann_date`` is available (e.g. Tushare, Baostock), prefer
that and use this as the fallback.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

DEADLINE_BY_REPORT_MONTH_DAY = {
    "-03-31": (0, "04-30"),
    "-06-30": (0, "08-31"),
    "-09-30": (0, "10-31"),
    "-12-31": (1, "04-30"),   # next year
}


def availability_date(report_period: str) -> Optional[str]:
    """Conservative PIT availability date for a report period (YYYY-MM-DD)."""
    text = str(report_period).strip()
    m = re.match(r"^(\d{4})-(03-31|06-30|09-30|12-31)$", text)
    if not m:
        return None
    year = int(m.group(1))
    offset, month_day = DEADLINE_BY_REPORT_MONTH_DAY["-" + m.group(2)]
    return f"{year + offset}-{month_day}"


def _num(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if text in ("", "--", "nan", "None", "NaN"):
            return None
        out = float(text)
        return out if out == out else None
    except Exception:
        return None


def _column(df, *candidates: str) -> Optional[str]:
    """Pick the candidate column with the most non-null values.

    Sina sometimes ships a column that exists but is empty for every row (for
    example 销售毛利率); blindly taking the first match would leave the field
    blank even though a usable alternative (主营业务利润率) exists.
    """
    best, best_count = None, 0
    for name in candidates:
        if name not in df.columns:
            continue
        count = int(df[name].notna().sum())
        if count > best_count:
            best, best_count = name, count
    return best


def fetch_symbol_fundamentals(symbol: str, start_year: str = "2019") -> List[Dict[str, object]]:
    """Return PIT-safe fundamental snapshots sorted by availability date.

    Each snapshot has ``available_at`` = the disclosure deadline, so joining by
    that field never uses a report before it could legally have been known.
    """
    import akshare as ak

    code = str(symbol).split(".")[0]
    try:
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(start_year))
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []

    col_date = _column(df, "日期")
    col_eps = _column(df, "摊薄每股收益(元)", "加权每股收益(元)", "每股收益_调整后(元)")
    col_bps = _column(df, "每股净资产_调整前(元)", "每股净资产_调整后(元)", "调整后的每股净资产(元)")
    col_roe = _column(df, "净资产收益率(%)", "加权净资产收益率(%)")
    col_rev = _column(df, "主营业务收入增长率(%)")
    col_pro = _column(df, "净利润增长率(%)")
    col_gm = _column(df, "销售毛利率(%)", "主营业务利润率(%)")
    if col_date is None:
        return []

    out: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        period = str(row.get(col_date) or "").strip()[:10]
        available = availability_date(period)
        if not available:
            continue
        out.append({
            "symbol": str(symbol),
            "report_period": period,
            "available_at": available,
            "eps": _num(row.get(col_eps)) if col_eps else None,
            "bps": _num(row.get(col_bps)) if col_bps else None,
            "roe": _num(row.get(col_roe)) if col_roe else None,
            "revenue_growth": _num(row.get(col_rev)) if col_rev else None,
            "profit_growth": _num(row.get(col_pro)) if col_pro else None,
            "gross_margin": _num(row.get(col_gm)) if col_gm else None,
            "source": "akshare:sina_financial_indicator",
        })
    # Annual (12-31) and Q1 (03-31) share the same statutory deadline (04-30),
    # so ``available_at`` collides on every April 30. Sort by (available_at,
    # report_period) so the *later* report period deterministically wins the tie:
    # by 04-30 both are legally available, and Q1 is the fresher information.
    # (Before this fix the tie-break depended on the API's row order.)
    out.sort(key=lambda r: (str(r["available_at"]), str(r["report_period"])))
    return out


def latest_snapshot_asof(snapshots: List[Dict[str, object]], date: str) -> Dict[str, object]:
    """Most recent snapshot whose availability date is <= ``date`` (PIT join)."""
    current: Dict[str, object] = {}
    for snap in snapshots:
        if str(snap.get("available_at", "")) <= date:
            current = snap
        else:
            break
    return current
