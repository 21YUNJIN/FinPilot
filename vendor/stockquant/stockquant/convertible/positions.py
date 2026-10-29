"""可转债持仓盯盘：把"我实际持有的转债"接进风控与预警。

设计动机
--------
原来的「自动盯盘」只认股票：它拿股票因子模型去分析持仓，而真实持仓是可转债
（代码形如 11xxxx/12xxxx）。转债在股票面板里查不到行情，于是每只都被判成
"数据不足 / 盈亏 0"，盯盘对用户的主仓位实际上完全失效。

这个模块给转债持仓一套自己的判断链，全部只用**当前可得**信息：

1. **强赎**：已公告强赎（含最后交易日）→ 必须在最后交易日前卖出/转股；
2. **信用风险**：正股 ST / 正股股价 < 3 元（面退风险）/ 转债价格跌破 95 债底区；
3. **急跌**：近 20 个交易日跌幅超过阈值（>15%）→ 便宜的"信用事件探测器"；
4. **出榜**：双低值已经跌出策略名单（排名 > top_n）→ 下期轮动就该换掉；
5. **止损纪律**：单只浮亏超过 -15%；
6. 以上都不触发 → 持有。

排名用与回测**同一套过滤器**（价格/溢价率/成交额/纯债溢价率 PIT）算出，
所以"跌出名单"这句话是可执行、可复现的，不是拍脑袋。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from stockquant.convertible.double_low import _last, _load_panel

# 与回测/清单保持一致的默认参数（写在报告 double_low.json 的 params 里，可被覆盖）
DEFAULTS = {
    "top_n": 15,
    "min_price": 95.0,
    "max_price": 200.0,
    "max_premium": 200.0,
    "min_premium": -5.0,
    "min_amount": 1e7,
    "min_pure_bond_premium": 0.0,
    "max_drop_20d": 0.15,
    "min_listed_days": 180,
}

ACTION_REDEEM = "卖出/转股（强赎）"
ACTION_CREDIT = "卖出（信用风险）"
ACTION_CRASH = "减仓（急跌）"
ACTION_STOP = "减仓（浮亏止损）"
ACTION_DROP = "卖出（已跌出名单）"
ACTION_WATCH = "观察（跌出名单）"
ACTION_HOLD = "持有"
ACTION_NODATA = "无数据"


def is_cb_code(code: object) -> bool:
    """6 位纯数字且以 11（沪）/12（深）开头 = 可转债（含 118 科创板、132 可交换债）。"""
    c = str(code or "").strip()
    return len(c) == 6 and c.isdigit() and c[:2] in ("11", "12")


def _days(a: str, b: str) -> int:
    try:
        from datetime import date
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:
        return 0


def _recent_drop(ser: Dict[str, float], asof: str, window: int = 20) -> Optional[float]:
    """近 ``window`` 个交易日涨跌幅（不足则 None）。"""
    hist = sorted((d, v) for d, v in ser.items() if d <= asof)
    if len(hist) < window + 1:
        return None
    first, last = hist[-(window + 1)][1], hist[-1][1]
    if first <= 0:
        return None
    return last / first - 1.0


def market_ranking(panel, asof: str, params: Dict[str, object]) -> List[Dict[str, object]]:
    """全市场双低排名（与回测同一套过滤，只是不过滤 top_n）。"""
    price, prem, name, _dates, rating, _stock, amount, pbond, volmap, listed = panel
    min_price = float(params.get("min_price", DEFAULTS["min_price"]))
    max_price = float(params.get("max_price", DEFAULTS["max_price"]))
    max_premium = float(params.get("max_premium", DEFAULTS["max_premium"]))
    min_premium = float(params.get("min_premium", DEFAULTS["min_premium"]))
    min_amount = float(params.get("min_amount", DEFAULTS["min_amount"]) or 0)
    min_pbond = params.get("min_pure_bond_premium", DEFAULTS["min_pure_bond_premium"])
    max_drop = params.get("max_drop_20d", DEFAULTS["max_drop_20d"])
    min_listed_days = int(params.get("min_listed_days") or DEFAULTS["min_listed_days"] or 0)

    rows: List[Dict[str, object]] = []
    for code in prem:
        ser = price.get(code, {})
        if not ser:
            continue
        # 与回测同口径：上市前占位行情直接排除；未满 min_listed_days 视为未进转股期。
        ld = listed.get(code)
        if ld and (asof < ld or _days(ld, asof) < min_listed_days):
            continue
        px = _last(ser, asof)
        pv = _last(prem.get(code, {}), asof)
        if px is None or pv is None:
            continue
        if not (min_price <= px <= max_price) or pv > max_premium or pv < min_premium:
            continue
        if volmap and volmap.get(code, {}).get(asof, 0.0) <= 0:
            continue
        if min_amount:
            amap = amount.get(code, {})
            recent = sorted((d, v) for d, v in amap.items() if d <= asof)[-20:]
            if len(recent) < 10 or sum(v for _d, v in recent) / len(recent) < min_amount:
                continue
        if min_pbond is not None:
            pb = _last(pbond.get(code, {}), asof)
            if pb is None or pb < float(min_pbond):
                continue
        drop = _recent_drop(ser, asof)
        if max_drop is not None and drop is not None and drop < -abs(float(max_drop)):
            continue
        rows.append({"code": code, "name": name.get(code, code), "price": px, "premium": pv,
                     "double_low": px + pv, "rating": rating.get(code, ""), "drop_20d": drop})
    rows.sort(key=lambda r: r["double_low"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def analyze_positions(
    holdings: Dict[str, Dict[str, object]],
    *,
    data_csv: str,
    volume_csv: Optional[str] = None,
    live_json: Optional[str] = None,
    params: Optional[Dict[str, object]] = None,
    top_n: Optional[int] = None,
    asof: Optional[str] = None,
    panel=None,
) -> List[Dict[str, object]]:
    """给每只转债持仓一条可执行建议（只读，不发通知）。"""
    prm = dict(DEFAULTS)
    prm.update({k: v for k, v in (params or {}).items() if v is not None})
    if top_n:
        prm["top_n"] = int(top_n)
    if panel is None:
        panel = _load_panel(data_csv, volume_csv)
    price, prem, name, dates, rating, _stock, amount, pbond, _volmap, listed = panel
    asof = asof or (dates[-1] if dates else None)
    ranked = market_ranking(panel, asof, prm) if asof else []
    rank_by_code = {r["code"]: r for r in ranked}
    n_all = len(ranked)

    live: Dict[str, object] = {}
    if live_json and Path(live_json).exists():
        try:
            live = json.loads(Path(live_json).read_text(encoding="utf-8"))
        except Exception:
            live = {}
    redeem = (live.get("redeem") or {})
    st_bonds = (live.get("st_bonds") or {})
    cheap = (live.get("cheap_stock") or {})

    out: List[Dict[str, object]] = []
    for code, pos in (holdings or {}).items():
        code = str(code)
        if not is_cb_code(code):
            continue  # 股票持仓走股票盯盘，不在这里
        shares = int(pos.get("shares", 0) or 0)
        cost = float(pos.get("average_cost", 0.0) or 0.0)
        px = _last(price.get(code, {}), asof) if asof else None
        pv = _last(prem.get(code, {}), asof) if asof else None
        pb = _last(pbond.get(code, {}), asof) if asof else None
        info = rank_by_code.get(code)
        # 行情缺失的持仓：用成本价兜底，绝不假装有最新价
        stale = px is None
        px = px if px is not None else cost
        market_value = px * shares
        pnl_pct = (px / cost - 1.0) if cost > 0 else 0.0
        drop = _recent_drop(price.get(code, {}), asof) if (asof and code in price) else None
        rt = redeem.get(code) or {}
        status = str(rt.get("status") or "")
        redeeming = bool(status) and "强赎" in status and "不强赎" not in status
        st_name = st_bonds.get(code)
        cheap_px = cheap.get(code)
        rating_now = rating.get(code, "")

        action, severity, reasons = ACTION_HOLD, "ok", []
        if stale:
            action, severity = ACTION_NODATA, "warning"
            reasons.append("该债已无最新行情（可能已退市/到期/停牌），请到券商确认")
        if redeeming:
            action, severity = ACTION_REDEEM, "critical"
            reasons.append(f"已公告强赎，最后交易日 {rt.get('last_trade_date') or '见公告'}")
        if st_name:
            action, severity = ACTION_CREDIT, "critical"
            reasons.append(f"正股 {st_name}，转债信用风险上升")
        if cheap_px:
            if severity != "critical":
                action, severity = ACTION_CREDIT, "critical"
            reasons.append(f"正股股价 {cheap_px} 元（<3 元，面值退市风险）")
        if px and px < float(prm["min_price"]):
            if severity != "critical":
                action, severity = ACTION_CREDIT, "critical"
            reasons.append(f"转债价 {px:.1f} 已跌破 {prm['min_price']:.0f} 债底区（信用风险信号）")
        if drop is not None and drop < -abs(float(prm["max_drop_20d"])):
            if severity not in ("critical",):
                action, severity = ACTION_CRASH, "warning"
            reasons.append(f"近 20 日跌 {drop*100:.1f}%（急跌，疑似信用事件）")
        if pnl_pct <= -0.15:
            if severity == "ok":
                action, severity = ACTION_STOP, "warning"
            reasons.append(f"单只浮亏 {pnl_pct*100:.1f}%（触发 -15% 止损纪律）")
        if not stale and info is None and severity == "ok":
            # 已不具备入选条件（价格/溢价/流动性/信用任一不过关）
            action, severity = ACTION_DROP, "warning"
            reasons.append("已不满足入选条件（价格/溢价/流动性/纯债溢价率任一不达标）")
        elif not stale and info is not None and info["rank"] > int(prm["top_n"]) and severity == "ok":
            if info["rank"] <= int(prm["top_n"]) * 2:
                action, severity = ACTION_WATCH, "warning"
                reasons.append(f"双低排名第 {info['rank']}（已跌出前 {prm['top_n']}）")
            else:
                action, severity = ACTION_DROP, "warning"
                reasons.append(f"双低排名第 {info['rank']}（已跌出前 {prm['top_n']}）")

        out.append({
            "code": code,
            "name": name.get(code, code),
            "shares": shares,
            "cost": round(cost, 3),
            "price": round(px, 3),
            "stale": stale,
            "pnl_pct": round(pnl_pct, 4),
            "market_value": round(market_value, 1),
            "premium": None if pv is None else round(pv, 2),
            "double_low": None if (pv is None or stale) else round(px + pv, 2),
            "pure_bond_premium": None if pb is None else round(pb, 2),
            "rating": rating_now,
            "rank": (info or {}).get("rank"),
            "rank_universe": n_all,
            "in_top_n": bool(info and info["rank"] <= int(prm["top_n"])),
            "drop_20d": None if drop is None else round(drop, 4),
            "redeem_status": status or None,
            "redeem_last_day": rt.get("last_trade_date"),
            "st_stock": st_name,
            "cheap_stock_price": cheap_px,
            "action": action,
            "severity": severity,
            "reasons": reasons or ["持有：仍在名单内且无风险信号"],
        })
    out.sort(key=lambda r: ({"critical": 0, "warning": 1, "ok": 2}.get(r["severity"], 3), r.get("rank") or 10**9))
    return out


def alerts_from_positions(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """把持仓建议变成去重友好的 alert（供 AlertStore.append_new 使用）。"""
    alerts = []
    for r in rows:
        if r.get("severity") == "ok":
            continue
        kind = "cb_" + str(r.get("action") or "").split("（")[0]
        alerts.append({
            "id": f"cb-hold-{r['code']}-{kind}",
            "severity": r["severity"],
            "kind": kind,
            "symbol": r["code"],
            "title": f"{r['code']} {r['name']} · {r['action']}",
            "message": f"现价 {r['price']}，成本 {r['cost']}，盈亏 {r['pnl_pct']*100:.1f}%。" + "；".join(r.get("reasons") or []),
        })
    return alerts
