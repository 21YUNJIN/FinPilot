"""A 股交易日历（离线缓存 + 退化近似）。

用途：把"数据几天没更新"从**自然日**改成**交易日**——周五刷的数据在周一只有 1 个交易日差，
但自然日照样是 3 天；长假后同理。界面上的"数据陈旧"提示必须按交易日算才不会天天误报。

日历来源：``akshare.tool_trade_date_hist_sina()``，由 ``scripts/fetch_trade_calendar.py``
落盘到 ``data/real/trade_calendar.csv``（含未来日期，所以可以判断"最近一个交易日"）。

没有日历文件时退化用"周一到周五"近似，并把 ``source`` 标成 ``weekday_approx``
——调用方必须知道这个数字是近似的。
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_CALENDAR_PATH = "data/real/trade_calendar.csv"

_CACHE: Dict[str, Tuple[float, List[str]]] = {}


def _parse(d: object) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    s = str(d).strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    return None


def load_calendar(path: str = DEFAULT_CALENDAR_PATH) -> List[str]:
    """交易日升序列表；文件缺失/损坏时返回 []。"""
    p = Path(path)
    try:
        stat = p.stat()
    except Exception:
        return []
    key = str(p)
    cached = _CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1]:
        return cached[1]
    out: List[str] = []
    try:
        with p.open(encoding="utf-8-sig") as f:
            for i, line in enumerate(f):
                v = _parse(line.split(",")[0].strip().strip('"'))
                if v is None:
                    if i == 0 and "date" in line.lower():
                        continue  # 表头
                    continue
                out.append(v)
    except Exception:
        return []
    out = sorted(set(out))
    _CACHE[key] = (stat.st_mtime, out)
    return out


def _weekdays_between(start: str, end: str) -> int:
    a = date.fromisoformat(start)
    b = date.fromisoformat(end)
    if b <= a:
        return 0
    n = 0
    cur = a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def trading_days_between(start: str, end: str, path: str = DEFAULT_CALENDAR_PATH) -> Tuple[int, str]:
    """``start``（不含）到 ``end``（含）之间的交易日数量。

    返回 ``(days, source)``；``source`` 为 ``calendar`` 或 ``weekday_approx``。
    """
    cal = load_calendar(path)
    s, e = _parse(start), _parse(end)
    if not s or not e:
        return 0, "unknown"
    if cal:
        return sum(1 for d in cal if s < d <= e), "calendar"
    return _weekdays_between(s, e), "weekday_approx"


def last_trading_day(on_or_before: str, path: str = DEFAULT_CALENDAR_PATH) -> Optional[str]:
    d = _parse(on_or_before)
    if not d:
        return None
    cal = load_calendar(path)
    if cal:
        prev = [x for x in cal if x <= d]
        return prev[-1] if prev else None
    cur = date.fromisoformat(d)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def is_trading_day(day: str, path: str = DEFAULT_CALENDAR_PATH) -> bool:
    """Return whether ``day`` is a trading day.

    The calendar file is authoritative when present.  Without it we fall back
    to a weekday approximation and callers can inspect the source themselves.
    """
    d = _parse(day)
    if not d:
        return False
    cal = load_calendar(path)
    if cal:
        return d in set(cal)
    return date.fromisoformat(d).weekday() < 5


def previous_trading_day(before: str, path: str = DEFAULT_CALENDAR_PATH) -> Optional[str]:
    """Last trading day strictly before ``before``."""
    d = _parse(before)
    if not d:
        return None
    cal = load_calendar(path)
    if cal:
        prev = [x for x in cal if x < d]
        return prev[-1] if prev else None
    cur = date.fromisoformat(d) - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def next_trading_day(after: str, path: str = DEFAULT_CALENDAR_PATH) -> Optional[str]:
    """First trading day strictly after ``after``."""
    d = _parse(after)
    if not d:
        return None
    cal = load_calendar(path)
    if cal:
        nxt = [x for x in cal if x > d]
        return nxt[0] if nxt else None
    cur = date.fromisoformat(d) + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


def latest_completed_trading_day(
    now: Optional[datetime] = None,
    close_time: str = "15:00",
    path: str = DEFAULT_CALENDAR_PATH,
) -> Optional[str]:
    """Latest session whose close has completed by ``now``.

    This is the anchor for ETF plan freshness.  On a Sunday it returns Friday;
    on a trading day before 15:00 it returns the prior session; after 15:00 it
    returns the current session.  A date-only ``now`` is interpreted as end of
    day so callers that only have a date get deterministic behaviour.
    """
    now = now or datetime.now()
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime.combine(now, dt_time(23, 59))
    today = now.strftime("%Y-%m-%d")
    close = dt_time.fromisoformat(close_time)
    if is_trading_day(today, path) and now.time() >= close:
        return today
    return previous_trading_day(today, path)


def is_stale(data_date: str, today: Optional[str] = None, tolerance: int = 2,
             path: str = DEFAULT_CALENDAR_PATH) -> Tuple[bool, int, str]:
    """数据是否落后超过 ``tolerance`` 个交易日（默认 2）。"""
    today = today or date.today().strftime("%Y-%m-%d")
    lag, source = trading_days_between(data_date, today, path)
    return lag > tolerance, lag, source
