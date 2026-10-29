"""Lightweight real-time A-share quotes (Sina).

Used by the holdings monitor so a 24/7 watch can react to an intraday stop-loss
breach instead of only the daily close.  Best-effort: callers must be able to
fall back to the last daily bar when the quote source is unreachable.
"""

from __future__ import annotations

import urllib.request
from typing import Dict, Iterable, List

SINA_URL = "https://hq.sinajs.cn/list={codes}"
_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (StockQuant research)",
}


def sina_code(symbol: str) -> str:
    """``600309.SH`` -> ``sh600309``; bare codes are resolved by prefix.

    Convertible bonds need the same treatment as stocks:  SH bonds are
    110xxx/111xxx/113xxx/118xxx (all start with ``11``), SZ bonds are
    123xxx/127xxx/128xxx/12xxxx (start with ``12``).  Getting this wrong made
    every SH convertible bond silently disappear from the realtime monitor.
    """
    code, _, exchange = str(symbol).strip().upper().partition(".")
    if exchange in ("SH", "SZ", "BJ"):
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exchange]
    elif code.startswith(("6", "9", "5", "11")):
        prefix = "sh"
    elif code.startswith(("0", "3", "12", "15", "16", "18")):
        prefix = "sz"
    else:
        prefix = "bj" if code.startswith(("4", "8")) else "sh"
    return f"{prefix}{code}"


def parse_sina_quotes(text: str) -> Dict[str, Dict[str, object]]:
    """Parse the ``var hq_str_xxx="..."`` payload into a symbol-keyed dict."""
    out: Dict[str, Dict[str, object]] = {}
    for line in text.splitlines():
        if "hq_str_" not in line or '"' not in line:
            continue
        head, _, rest = line.partition("=")
        sina = head.replace("var hq_str_", "").strip().strip(";")
        body = rest.split('"')[1] if '"' in rest else ""
        fields = body.split(",")
        if len(fields) < 32 or not fields[0]:
            continue
        def _f(idx: int) -> float:
            try:
                return float(fields[idx])
            except Exception:
                return 0.0

        symbol = sina[2:].upper() + "." + sina[:2].upper()
        price = _f(3)
        prev_close = _f(2)
        # 新浪返回里本来就带五档：10..19 是买一~买五(量,价)，20..29 是卖一~卖五(量,价)。
        # 股票和可转债格式一致（已用 113042 实测：卖一 117.138 == 券商 App 显示的卖一）。
        # 有它才能回答"你到底能不能按这个价买到"——paper 对账的关键输入。
        out[symbol] = {
            "name": fields[0],
            "open": _f(1),
            "prev_close": prev_close,
            "price": price,
            "high": _f(4),
            "low": _f(5),
            "bid1": _f(11),
            "bid1_volume": _f(10),
            "ask1": _f(21),
            "ask1_volume": _f(20),
            "date": fields[30],
            "time": fields[31],
            "change_pct": (price / prev_close - 1.0) if prev_close > 0 else 0.0,
        }
    return out


def fetch_realtime_quotes(symbols: Iterable[str], timeout: float = 8.0) -> Dict[str, Dict[str, object]]:
    """Fetch latest quotes for the given symbols. Returns {} on any failure."""
    codes: List[str] = [sina_code(s) for s in symbols if str(s).strip()]
    if not codes:
        return {}
    url = SINA_URL.format(codes=",".join(codes))
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        raw = urllib.request.urlopen(request, timeout=timeout).read()
    except Exception:
        return {}
    try:
        text = raw.decode("gbk", "ignore")
    except Exception:
        text = raw.decode("utf-8", "ignore")
    return parse_sina_quotes(text)
