"""可转债"双低"轮动策略（double-low rotation）.

逻辑：可转债下有债底、上有股性；按
    双低 = 收盘价 + 转股溢价率(%)
从小到大排序，买入最低的 N 只，定期轮动。价格低 = 债底保护，溢价低 = 跟涨能力强。

只依赖标准库；输入是 ``fetch_convertible_bonds.py`` 产出的长表 CSV。
"""
from __future__ import annotations

import bisect
import csv
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple


@dataclass
class DoubleLowResult:
    dates: List[str]
    equity: List[float]
    rebalances: List[Dict[str, object]]
    metrics: Dict[str, float] = field(default_factory=dict)


_RATING_ORDER = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
                 "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"]


def rating_ok(rating: str, min_rating: str) -> bool:
    """True if ``rating`` is at least ``min_rating`` (e.g. AA-)."""
    r = str(rating or "").upper().replace("STI", "").strip()
    base = r.rstrip("+-")
    norm = r
    try:
        return _RATING_ORDER.index(norm) <= _RATING_ORDER.index(min_rating)
    except ValueError:
        return False


def _load_panel(path: str, volume_path: Optional[str] = None):
    price: Dict[str, Dict[str, float]] = {}
    prem: Dict[str, Dict[str, float]] = {}
    name: Dict[str, str] = {}
    rating: Dict[str, str] = {}
    stock: Dict[str, str] = {}
    amount: Dict[str, Dict[str, float]] = {}
    volmap: Dict[str, Dict[str, float]] = {}
    pbond: Dict[str, Dict[str, float]] = {}
    listed: Dict[str, str] = {}
    dates_set = set()
    if volume_path:
        try:
            with open(volume_path, newline="", encoding="utf-8-sig") as vf:
                for row in csv.DictReader(vf):
                    c = str(row.get("bond_code") or "").strip()
                    d = str(row.get("date") or "").strip()
                    try:
                        a = float(row.get("amount") or 0)
                    except Exception:
                        a = 0.0
                    if c and d and a > 0:
                        amount.setdefault(c, {})[d] = a
                    try:
                        vv = float(row.get("volume") or 0)
                    except Exception:
                        vv = 0.0
                    if c and d:
                        volmap.setdefault(c, {})[d] = vv
        except Exception:
            pass
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = str(row.get("bond_code") or "").strip()
            d = str(row.get("date") or "").strip()
            if not code or not d:
                continue
            name[code] = str(row.get("bond_name") or code)
            rating.setdefault(code, str(row.get("rating") or ""))
            stock.setdefault(code, str(row.get("stock_code") or ""))
            ld = str(row.get("list_date") or "").strip()
            if len(ld) == 10 and ld[4] == "-" and ld not in ("NaT", "nan"):
                listed.setdefault(code, ld)
            try:
                c = float(row.get("close") or "")
            except Exception:
                c = None
            try:
                p = float(row.get("conversion_premium") or "")
            except Exception:
                p = None
            # data-quality guard: CB prices live roughly in [20, 5000]; anything
            # outside is a scrape error (e.g. price 0.001, code 404002 +14532%).
            if c is not None and 20.0 <= c <= 5000.0:
                price.setdefault(code, {})[d] = c
                dates_set.add(d)
            if c is not None and p is not None:
                prem.setdefault(code, {})[d] = p
            try:
                pb = float(row.get("pure_bond_premium") or "")
            except Exception:
                pb = None
            if c is not None and pb is not None:
                pbond.setdefault(code, {})[d] = pb
    return price, prem, name, sorted(dates_set), rating, stock, amount, pbond, volmap, listed


def _days_between(a: str, b: str) -> int:
    """``a`` 到 ``b`` 的自然日差（解析失败返回 0）。"""
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:
        return 0


class _DateIndex:
    """``last value / last date at or before asof`` in O(log n).

    ``_rank`` used to scan each bond's whole series (up to ~1,500 sessions) four
    to six times per bond per rebalance -- 376 rebalances x ~1,000 bonds made the
    threshold scan take hours.  The panel is read-only inside a run, so the sorted
    date list per code is built once and then bisected.
    """

    __slots__ = ("_series", "_dates")

    def __init__(self, series: Dict[str, Dict[str, float]]):
        self._series = series
        self._dates = {code: sorted(sub) for code, sub in series.items()}

    def dates(self, code: str) -> List[str]:
        return self._dates.get(code) or []

    def last_date(self, code: str, asof: str) -> Optional[str]:
        ds = self._dates.get(code)
        if not ds:
            return None
        i = bisect.bisect_right(ds, asof) - 1
        return ds[i] if i >= 0 else None

    def last(self, code: str, asof: str) -> Optional[float]:
        d = self.last_date(code, asof)
        return None if d is None else self._series[code][d]

    def count_upto(self, code: str, asof: str) -> int:
        return bisect.bisect_right(self._dates.get(code) or [], asof)


def _last(series: Dict[str, float], asof: str) -> Optional[float]:
    best_d, best_v = None, None
    for d, v in series.items():
        if d <= asof and (best_d is None or d > best_d):
            best_d, best_v = d, v
    return best_v


def _last_date(series: Dict[str, float], asof: str) -> Optional[str]:
    """`asof` 之前（含）最后一个有观测的日期。"""
    best_d = None
    for d in series:
        if d <= asof and (best_d is None or d > best_d):
            best_d = d
    return best_d


def valuation_price(price, code, d):
    """估值价：`d` 之前（含）最后一个已知收盘价。**只用于盯市，不可当成成交价。**"""
    return _last(price.get(code, {}), d)


def trade_state(price, volmap, code, d) -> str:
    """当日可交易状态：``traded`` / ``halted`` / ``unknown`` / ``no_quote``。

    区分这两件事至关重要：

    * ``halted``：成交量文件**有**这一行且成交量为 0 → 真停牌/无成交，卖不掉；
    * ``unknown``：成交量文件**没有**这一行 → 供应商缺口，不代表停牌。把缺口当停牌
      会让"卖不掉"的持仓无限累积（实测会让 15 只的组合膨胀到几十只，净值口径完全失真）。
    """
    if d not in price.get(code, {}):
        return "no_quote"
    if not volmap:
        return "unknown"
    vser = volmap.get(code, {})
    if d not in vser:
        return "unknown"
    return "traded" if vser.get(d, 0.0) > 0 else "halted"


def execution_price(price, volmap, code, d):
    """成交价：只有当**当日有成交量**时才返回价格，否则返回 ``None``（不可成交）。

    停牌/无量日既不能买也不能卖；用未来某个交易日的价格充当"当日成交价"
    会把停牌期间和恢复后的收益重复计进净值。
    """
    ser = price.get(code, {})
    if not ser:
        return None
    if volmap:
        if volmap.get(code, {}).get(d, 0.0) <= 0:
            return None
    return ser.get(d)


def first_executable(price, volmap, code, d, max_fwd=10):
    """``(成交日, 成交价)``：`d` 当日或之后第一个有成交量的交易日（用于"延迟成交"）。

    返回的日期必须被真正用于收益归属：持有期收益只能算到该日，下一期的成本基础
    也要更新到该日的价格 —— 否则就是拿未来价格当当日价，再把同一段收益算第二遍。
    """
    ser = price.get(code, {})
    if not ser:
        return None
    vser = volmap.get(code, {}) if volmap else {}
    for dd in sorted(x for x in ser if x >= d)[: max(1, int(max_fwd) + 1)]:
        if not volmap or vser.get(dd, 0.0) > 0:
            return dd, ser[dd]
    return None


def tradeable_price(price, volmap, code, d, max_fwd=0):
    """可成交价（默认只认 `d` 当日）。

    ``max_fwd=0``（新默认）= 当日无成交则退回最后已知价（**仅作盯市**，调用方必须
    自己知道这是估值价）；``max_fwd>0`` 只在明确做"延迟成交"研究时使用，且调用方
    必须同时使用 :func:`first_executable` 返回的成交日来切分收益 —— 不能只取价格。
    """
    if max_fwd:
        hit = first_executable(price, volmap, code, d, max_fwd=max_fwd)
        if hit is not None:
            return hit[1]
    px = execution_price(price, volmap, code, d)
    if px is not None:
        return px
    return valuation_price(price, code, d)


def _rank(price, prem, d, top_n, min_price, max_price, max_premium, min_premium=-5.0, max_stale_days=20, min_history_days=20, exclude=None, rating=None, min_rating=None, amount=None, min_amount=None, pbond=None, min_pure_bond_premium=None, volmap=None, max_drop_20d=None, min_amount_days=10, listed=None, min_listed_days=180, score_mode="double_low", max_stale_premium_days=None, index=None):
    rows = []
    exclude = exclude or set()
    cutoff = (date.fromisoformat(d) - timedelta(days=max_stale_days)).isoformat()
    price_idx = (index or {}).get("price") or _DateIndex(price)
    prem_idx = (index or {}).get("prem") or _DateIndex(prem)
    pbond_idx = (index or {}).get("pbond") or (_DateIndex(pbond) if pbond is not None else None)
    for code, pser in prem.items():
        if code in exclude:
            continue
        if listed:
            # 上市前供应商会给"占位行情"（收盘价 100.0、零成交），
            # 它在价格与溢价率两道过滤里都能蒙混过关 —— 用上市日直接挡住。
            ld = listed.get(code)
            if ld:
                if d < ld:
                    continue
                # 可转债发行结束满 6 个月才进转股期；上市日≈发行后 2~4 周，
                # 所以"上市满 180 天"是"已过转股起始日"的保守代理。
                # 未进转股期的债，转股溢价率没有经济含义。
                if min_listed_days and _days_between(ld, d) < min_listed_days:
                    continue
        ser = price.get(code, {})
        ser_dates = price_idx.dates(code)
        if not ser or not ser_dates or ser_dates[-1] < cutoff:
            # Bond has no live quote near ``d`` -> already delisted/matured (强赎/到期).
            continue
        upto = price_idx.count_upto(code, d)
        # Skip freshly listed bonds: their first days are 炒作 (no daily price limit,
        # T+0) and the panel often shows the 100 par before real trading starts,
        # which would let the backtest 'buy' the listing-day pop.
        if upto < min_history_days:
            continue
        c = ser[ser_dates[upto - 1]] if upto else None
        if c is None:
            continue
        if volmap:
            # 可成交: 当日必须有成交量, 否则无法真正以此价买入
            if volmap.get(code, {}).get(d, 0.0) <= 0:
                continue
        if max_drop_20d is not None:
            # 信用事件探测器: 近 20 日急跌 > max_drop_20d (如 15%) 则剔除。
            # 代价: 会误伤正常回调; 但能在"跳空跌穿债底"之前先躲开。
            hd = ser_dates[max(0, upto - 21):upto]
            if len(hd) >= 21 and ser[hd[0]] > 0 and ser[hd[-1]] / ser[hd[0]] - 1.0 < -abs(max_drop_20d):
                continue
        pv_date = prem_idx.last_date(code, d)
        pv = pser[pv_date] if pv_date is not None else None
        if pv is None or pv_date is None:
            continue
        # 转股溢价率必须和价格一样"新鲜"：价格可能是当日的，但溢价率沿用很旧的
        # 观测（转股价调整/停牌/抓取缺口）时，双低排序会用一个过期的因子值。
        premium_cutoff = cutoff if max_stale_premium_days is None else (
            date.fromisoformat(d) - timedelta(days=max_stale_premium_days)).isoformat()
        if pv_date < premium_cutoff:
            continue
        if not (min_price <= c <= max_price) or pv > max_premium or pv < min_premium:
            continue
        if min_rating and rating is not None and not rating_ok(rating.get(code, ''), min_rating):
            continue
        pb = None
        pb_date = None
        if pbond_idx is not None and pbond.get(code):
            pb_date = pbond_idx.last_date(code, d)
            pb = pbond[code][pb_date] if pb_date is not None else None
        if min_pure_bond_premium is not None and pbond is not None:
            # 同一个陈旧度规则：纯债溢价率是信用代理，用过期的值等于没做这道过滤。
            if pb is None or pb_date is None or pb_date < premium_cutoff:
                continue
            if pb < min_pure_bond_premium:
                continue
        if min_amount and amount is not None:
            amap = amount.get(code, {})
            recent = sorted([(dd, v) for dd, v in amap.items() if dd <= d])[-20:]
            # 观测天数太少时（数据缺口/刚上市），20 日均额不可信 -> 直接剔除，
            # 避免用 1~2 天的成交额把一只流动性差的债选进来。
            if len(recent) < min_amount_days:
                continue
            if sum(v for _, v in recent) / len(recent) < min_amount:
                continue
        # 额外带上研究/界面需要的原始字段（归因、赛马、清单展示都用同一份快照）
        amap = (amount or {}).get(code, {})
        recent_amt = sorted((dd, v) for dd, v in amap.items() if dd <= d)[-20:]
        rows.append({
            "code": code, "price": c, "premium": pv, "double_low": c + pv,
            "pure_bond_premium": pb,
            "amount_20d": (sum(v for _dd, v in recent_amt) / len(recent_amt)) if recent_amt else None,
            "days_listed": (_days_between(listed[code], d) if (listed and code in listed) else None),
            # 每个字段的观测日期都要能追溯：价格新、溢价率旧是最隐蔽的脏数据形态。
            "price_date": ser_dates[upto - 1] if upto else None,
            "premium_date": pv_date,
            "pure_bond_premium_date": pb_date,
        })
    rows = _apply_score(rows, score_mode)
    rows.sort(key=lambda r: r["score"])
    out = []
    for r in rows[:top_n]:
        r = dict(r)
        r["double_low"] = r.pop("score")   # 兼容旧字段名（清单/界面都读 double_low）
        out.append(r)
    return out


def _apply_score(rows, score_mode: str = "double_low"):
    """给候选行打分。默认 ``price + premium``（市场惯例的双低），其它是研究对照。

    支持：``price``（只按低价）、``premium``（只按低转股溢价率）、
    ``weighted:λ``（price + λ·premium）、``zscore``（两个因子横截面标准化后相加）。
    打分只在**通过同一套过滤**的候选池内部做，所以对照实验只改排序、不改 universe。
    """
    mode = (score_mode or "double_low").lower()
    if mode in ("double_low", "price+premium", ""):
        for r in rows:
            r["score"] = r["price"] + r["premium"]
        return rows
    if mode == "price":
        for r in rows:
            r["score"] = r["price"]
        return rows
    if mode == "premium":
        for r in rows:
            r["score"] = r["premium"]
        return rows
    if mode.startswith("weighted:"):
        lam = float(mode.split(":", 1)[1])
        for r in rows:
            r["score"] = r["price"] + lam * r["premium"]
        return rows
    if mode == "zscore":
        _zscore_inplace(rows, "price")
        _zscore_inplace(rows, "premium")
        for r in rows:
            r["score"] = r["z_price"] + r["z_premium"]
        return rows
    raise ValueError(f"unknown score_mode: {score_mode}")


def _zscore_inplace(rows, key):
    vals = [float(r[key]) for r in rows]
    if not vals:
        return
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals) - 1)
    sd = math.sqrt(var) if var > 0 else 1.0
    for r in rows:
        r[f"z_{key}"] = (float(r[key]) - mean) / sd


def run_double_low(
    path: str,
    top_n: int = 15,
    rebalance_days: int = 5,
    start: Optional[str] = None,
    end: Optional[str] = None,
    cost_bps: float = 5.0,
    min_price: float = 80.0,
    max_price: float = 200.0,
    max_premium: float = 200.0,
    min_premium: float = -5.0,
    exclude: Optional[set] = None,
    min_rating: Optional[str] = None,
    volume_path: Optional[str] = None,
    min_amount: Optional[float] = None,
    min_pure_bond_premium: Optional[float] = None,
    max_drop_20d: Optional[float] = None,
    min_listed_days: int = 180,
    strict_volume: bool = True,
    score_mode: str = "double_low",
    panel=None,
    sell_rank_buffer: Optional[int] = None,
    capital: Optional[float] = None,
    min_commission: float = 5.0,
    commission_rate: float = 0.00015,
) -> DoubleLowResult:
    """``panel`` 可传入 ``_load_panel`` 的结果以复用（参数扫描时省掉重复解析
    79 万行 CSV 的开销；面板是只读的）。

    ``strict_volume``（默认 True）= fail-closed：没有成交量数据就直接报错，
    因为缺了它"可成交标记"这道防线会静默消失（历史占位价/停牌债会被当成能买）。

    ``sell_rank_buffer``（换手带宽 / no-trade band）：买入仍然只看前 ``top_n`` 名，
    但**持仓只有在跌出前 ``sell_rank_buffer`` 名时才卖**（``sell_rank_buffer`` 必须 ≥ ``top_n``；
    ``None`` = 关闭 = 原口径）。它不改变任何选股信号，只减少排名在边界附近抖动带来的无谓换手。
    规则是显式的：**持仓只要没出现在「通过同一套过滤后的前 N 名」里，就被卖掉**——
    所以价格涨出 ``max_price``、当日无量、急跌触发等情况下仍然会被清仓（fail-closed 不变）。

    成交口径：**当日无量的债不能成交**（既不能买也不能卖）。具体规则：

    * 选股（买入）沿用原来的 fail-closed：调仓日没有成交量的债不入选；
    * 持仓在调仓日**真停牌**（成交量文件有该行且为 0）时不会被卖掉，而是被动保留，
      记录在 ``deferred_exits`` 里，等复牌后再换仓；
    * 成交量文件**缺行**属于数据缺口而不是停牌（``trade_state="unknown"``），
      不会被当成"卖不掉"，否则持仓会无限累积、净值口径彻底失真；
    * 收益按"当日或之前最后一个已知价"逐期盯市（纯 mark-to-market），
      停牌期间不产生收益、复牌跳空落在复牌的那一期 —— 既不提前确认、也不重复计算。

    ``capital`` + ``min_commission``（本金感知的成本模型）：给定本金后，成本不再是固定
    ``cost_bps``，而是**逐笔** ``max(min_commission, commission_rate × 名义金额)``。
    小资金下最低佣金占绝对主导（1.8 万本金、每手约 1,200 元 → 单边 ≈42bps，而回测假设 5bps）。
    不传 ``capital`` 时保持原来的固定 ``cost_bps`` 口径（历史结论可复现）。
    """
    if panel is None:
        panel = _load_panel(path, volume_path)
    price, prem, name, all_dates, rating, stock, amount, pbond, volmap, listed = panel
    # 没有成交额数据时，流动性过滤会把所有债静默剔除（回测变成一条直线），
    # 这是最容易骗过自己的失败模式 —— 直接报错，逼调用方给成交量文件。
    if min_amount and not amount:
        raise ValueError("min_amount 需要成交额数据：请传 volume_path（如 data/real/cb_volume.csv）")
    if strict_volume and not volmap:
        raise ValueError("可成交过滤需要成交量数据：请传 volume_path（如 data/real/cb_volume.csv）"
                         "；如确属故意（例如合成数据单测），显式传 strict_volume=False")
    if sell_rank_buffer is not None and sell_rank_buffer < top_n:
        raise ValueError("sell_rank_buffer 必须 >= top_n（否则缓冲区比持仓还小，等于立刻卖出）")
    if capital is not None and capital <= 0:
        raise ValueError("capital 必须为正数")
    dates = [d for d in all_dates if (start is None or d >= start) and (end is None or d <= end)]
    if len(dates) < rebalance_days * 3:
        raise ValueError("not enough dates")
    rb_dates = dates[::rebalance_days]

    equity = [1.0]
    eq_dates = [rb_dates[0]]
    rebalances: List[Dict[str, object]] = []
    prev_picks: List[Dict[str, object]] = []
    periods_per_year = 252.0 / rebalance_days
    _valuation_px = lambda code, d: valuation_price(price, code, d)

    # One sorted-date index for the whole run: ``_rank`` is called on every
    # rebalance for every bond, and a linear scan per bond per field made the
    # threshold scan take hours.
    _index = {"price": _DateIndex(price), "prem": _DateIndex(prem),
              "pbond": _DateIndex(pbond) if pbond is not None else None}
    rank_limit = max(top_n, sell_rank_buffer or 0)
    for d in rb_dates:
        ranked = _rank(price, prem, d, rank_limit, min_price, max_price, max_premium, min_premium, exclude=exclude, rating=rating, min_rating=min_rating, amount=amount, min_amount=min_amount, pbond=pbond,
                             min_pure_bond_premium=min_pure_bond_premium, volmap=volmap, max_drop_20d=max_drop_20d,
                             listed=listed, min_listed_days=min_listed_days, score_mode=score_mode,
                             index=_index)
        prev_codes = {p["code"] for p in prev_picks}

        # 当日**真的没有成交**（成交量文件有该行且为 0）的持仓不能假装卖掉。
        # 旧实现会向后找"未来第一个有成交的价格"当作当日卖出价 —— 那既把停牌期间的
        # 漂移提前记进当期净值，又让同一段收益在下一期被重复计算。
        # 注意：成交量文件缺行 = 数据缺口，不是停牌，不能据此把持仓锁死。
        stuck = [
            p for p in prev_picks
            if trade_state(price, volmap, p["code"], d) == "halted"
        ]

        if sell_rank_buffer and prev_picks:
            # 带宽：仍在「前 sell_rank_buffer 名」里的持仓保留，空出的仓位用排名最靠前的新面孔补齐。
            kept = [r for r in ranked if r["code"] in prev_codes]
            kept_codes = {r["code"] for r in kept}
            fresh = [r for r in ranked if r["code"] not in kept_codes][: max(0, top_n - len(kept))]
            picks = (kept + fresh)[:top_n]
        else:
            picks = ranked[:top_n]

        pick_codes = {p["code"] for p in picks}
        stuck_codes = {p["code"] for p in stuck}
        deferred_exits = [p for p in stuck if p["code"] not in pick_codes]
        if deferred_exits:
            # 卖不掉的持仓被动留在组合里（仓位数可能暂时超过 top_n）
            picks = picks + deferred_exits
            pick_codes |= {p["code"] for p in deferred_exits}

        # 换手只统计"真的能成交"的部分：卖不掉的持仓不计入卖出腿。
        # turnover = 换手只数 / 持仓数（单边），成本按双边收（2 × turnover）。
        sold = prev_codes - pick_codes
        bought = pick_codes - prev_codes
        turnover = (len(sold) + len(bought)) / (2 * max(1, top_n)) if prev_picks else 1.0
        rebalances.append({"date": d, "picks": picks, "turnover": turnover})

        marks: Dict[str, Optional[float]] = {}
        if prev_picks:
            # 当期收益 = 纯盯市：用"当日或之前最后一个已知价"计价，不使用任何未来数据。
            # 停牌期间价格不动 → 该期收益为 0；复牌后的跳空落在真正复牌的那一期里。
            rets = []
            unpriced = 0
            for p in prev_picks:
                px = _valuation_px(p["code"], d)
                marks[p["code"]] = px
                if px is None or p["price"] <= 0:
                    unpriced += 1
                    continue
                rets.append(px / p["price"] - 1.0)
            port_ret = statistics.mean(rets) if rets else 0.0
            # 成本口径（2026-10-13 修正）：turnover 的定义是"换手只数 / 持仓数"（单边，k/n），
            # 但一次调仓要**卖 k 只 + 买 k 只**，两边都付佣金/价差 → 双边成本 = 2 × turnover。
            # 之前只收了一边，等于把换手成本低估了一半（实测 CAGR 虚高 0.83 个百分点/年）。
            cost = 2.0 * turnover * cost_bps / 10000.0
            if capital is not None:
                # 本金感知：一次调仓要卖 k 只 + 买 k 只，k = turnover × top_n，
                # 每笔按 max(最低佣金, 费率 × 名义金额) 收，名义金额 = 本金 / top_n。
                notional = capital / max(1, top_n)
                per_trade = max(min_commission, commission_rate * notional)
                cost = 2.0 * turnover * max(1, top_n) * per_trade / capital
            rebalances[-1]["cost"] = cost
            rebalances[-1]["period_return"] = port_ret
            rebalances[-1]["deferred_exits"] = len(deferred_exits)
            rebalances[-1]["unpriced_holds"] = unpriced
            equity.append(equity[-1] * (1.0 + port_ret - cost))
            eq_dates.append(d)

        # 新一期的成本基础：老持仓（保留 + 卖不掉）结转当日盯市价，
        # 新买入的用 _rank 给出的当日成交价（_rank 已要求当日有成交量）。
        for pick in picks:
            if pick["code"] in prev_codes:
                px = marks.get(pick["code"])
                if px is not None and px > 0:
                    pick["price"] = px
                    pick["price_date"] = _last_date(price.get(pick["code"], {}), d)
        prev_picks = picks

    rets = [equity[i + 1] / equity[i] - 1.0 for i in range(len(equity) - 1)]
    m = _metrics(rets, equity, periods_per_year)
    return DoubleLowResult(dates=eq_dates, equity=equity, rebalances=rebalances, metrics=m)


def _metrics(rets, equity, periods_per_year):
    if not rets:
        return {}
    n = len(rets)
    years = n / periods_per_year
    cagr = equity[-1] ** (1.0 / years) - 1.0 if years > 0 and equity[-1] > 0 else 0.0
    mean = statistics.mean(rets)
    std = statistics.pstdev(rets)
    sharpe = mean / std * math.sqrt(periods_per_year) if std > 1e-16 else 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return {"periods": float(n), "years": years, "total_return": equity[-1] - 1.0,
            "cagr": cagr, "sharpe": sharpe, "max_drawdown": mdd,
            "win_rate": sum(1 for r in rets if r > 0) / n, "mean_period": mean}


def current_picks(path: str, top_n: int = 15, asof: Optional[str] = None,
                  min_price: float = 80.0, max_price: float = 200.0, max_premium: float = 200.0, min_premium: float = -5.0,
                  exclude: Optional[set] = None, min_rating: Optional[str] = None,
                  volume_path: Optional[str] = None, min_amount: Optional[float] = None,
                  min_pure_bond_premium: Optional[float] = None, max_drop_20d: Optional[float] = None,
                  min_listed_days: int = 180, strict_volume: bool = True, panel=None,
                  score_mode: str = "double_low"):
    """Latest double-low ranking (买入清单)."""
    if panel is None:
        panel = _load_panel(path, volume_path)
    price, prem, name, dates, rating, stock, amount, pbond, volmap, listed = panel
    if min_amount and not amount:
        raise ValueError("min_amount 需要成交额数据：请传 volume_path（如 data/real/cb_volume.csv）")
    if strict_volume and not volmap:
        raise ValueError("可成交过滤需要成交量数据：请传 volume_path（如 data/real/cb_volume.csv）"
                         "；如确属故意（例如合成数据单测），显式传 strict_volume=False")
    d = asof or dates[-1]
    rows = _rank(price, prem, d, top_n, min_price, max_price, max_premium, min_premium, exclude=exclude, rating=rating, min_rating=min_rating, amount=amount, min_amount=min_amount, pbond=pbond,
                             min_pure_bond_premium=min_pure_bond_premium, volmap=volmap,
                             max_drop_20d=max_drop_20d, listed=listed, min_listed_days=min_listed_days,
                             score_mode=score_mode,
                             index={"price": _DateIndex(price), "prem": _DateIndex(prem),
                                    "pbond": _DateIndex(pbond) if pbond is not None else None})
    out = [{"bond_code": r["code"], "bond_name": name.get(r["code"], r["code"]),
            "price": round(r["price"], 2), "premium": round(r["premium"], 2),
            "double_low": round(r["double_low"], 2), "rating": rating.get(r["code"], ""),
            "stock_code": stock.get(r["code"], ""), "asof": d,
            "amount": round(statistics.mean([v for _, v in sorted(amount.get(r["code"], {}).items())[-20:]]), 1)
            if amount and amount.get(r["code"]) else None} for r in rows]

    # ---- 「值得买分」0~100（越高越好），用于从高到低排序 ----
    if out:
        dls = [x["double_low"] for x in out]
        lo, hi = min(dls), max(dls)
        span = (hi - lo) or 1.0
        amts = [x["amount"] for x in out if x["amount"]]
        amax = max(amts) if amts else 0.0
        _rating_pts = {"AAA": 25, "AA+": 20, "AA": 15, "AA-": 10}
        for x in out:
            # 双低 60 分: 越低越高分
            s_dl = 60.0 * (1.0 - (x["double_low"] - lo) / span)
            # 评级 25 分
            s_rt = _rating_pts.get(str(x.get("rating") or "").upper().strip(), 5)
            # 流动性 15 分: 20 日均成交额相对最大
            s_lq = 15.0 * (x["amount"] / amax) if (x["amount"] and amax > 0) else 0.0
            x["worth_score"] = round(s_dl + s_rt + s_lq, 1)
        out.sort(key=lambda x: -x["worth_score"])
    return out


def redeem_risk_codes(live_path: str) -> set:
    """Bond codes with an active 强赎 (forced redemption) action -> must not buy."""
    import json
    try:
        data = json.loads(open(live_path, encoding="utf-8").read())
    except Exception:
        return set()
    out = set()
    for code, info in (data.get("redeem") or {}).items():
        status = str(info.get("status") or "")
        if "强赎" in status and "不强赎" not in status:
            out.add(str(code))
    return out


def st_risk_codes(live_path: str) -> set:
    """Bond codes whose underlying stock is ST/*ST (current) -> exclude from live picks."""
    import json
    try:
        data = json.loads(open(live_path, encoding="utf-8").read())
    except Exception:
        return set()
    return set((data.get("st_bonds") or {}).keys()) | set((data.get("cheap_stock") or {}).keys())
