"""腾讯行情：日线历史 + 实时快照（本项目自己的实现，只用标准库）。

为什么要它：可转债的行情以前只有 AKShare（底层东方财富）**一个**来源，
一旦供应商字段口径变化或某天数据异常，我们没有独立手段发现（UNKNOWNS A1）。
腾讯的接口同样支持可转债（已实测 sh113042/sz128142 的日线与实时），
于是它成为**第二个独立数据源**，用于对拍。

设计取舍：
- 只用标准库（urllib/json），与 convertible 模块的纪律一致，不引 pandas；
- 不复制第三方库代码：参考的 mpquant/Ashare 仓库**没有 LICENSE**（默认版权保留），
  不能把它的代码放进我们 MIT 许可的仓库；这里是我们自己的实现，只借用"双源"这个思路。
- 交易所前缀规则与 ``realtime.sina_code`` 保持一致：11xxxx→sh、12xxxx→sz、6→sh、0/3→sz。

接口：
    日线   http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<code>,day,,<end>,<count>[,qfq]
    实时   https://qt.gtimg.cn/q=<code1>,<code2>,...
"""

from __future__ import annotations

import json
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_HEADERS = {"User-Agent": "Mozilla/5.0 (StockQuant research)", "Referer": "https://gu.qq.com/"}
# 注意：腾讯这个接口要求 param 有 **6 段**（code,day,start,end,count,adj），少一段返回 "bad params"；最后一段留空即"不复权"，加 qfq 才是前复权。
# 实测：可转债返回的键名是 day（不是 qfqday），且各 flag 下数值相同 → 转债本来就不做复权。
_KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,{end},{count},"
_KLINE_URL_ADJ = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,{end},{count},qfq"
_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"


def tx_code(symbol: str) -> str:
    """``113042`` → ``sh113042``；``128142`` → ``sz128142``；带后缀的也认。"""
    code, _, exchange = str(symbol).strip().upper().partition(".")
    if exchange in ("SH", "SZ"):
        return ("sh" if exchange == "SH" else "sz") + code
    if code.startswith(("6", "9", "5", "11")):
        return "sh" + code
    return "sz" + code


def _get(url: str, timeout: float = 12.0) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read()


def daily_bars(symbol: str, count: int = 800, end: str = "", adjusted: bool = False,
               timeout: float = 12.0) -> List[Tuple[str, float, float, float, float, float]]:
    """日线：(date, open, close, high, low, volume)。默认**不复权**（与东财面板口径一致）。

    失败返回 []（调用方自己决定是降级还是报错，绝不编造数据）。
    """
    code = tx_code(symbol)
    template = _KLINE_URL_ADJ if adjusted else _KLINE_URL
    url = template.format(code=code, end=end, count=int(count))
    try:
        payload = json.loads(_get(url, timeout=timeout))
        node = payload["data"][code]
    except Exception:
        return []
    rows = None
    for key in ("day", "qfqday"):
        if key in node and node[key]:
            rows = node[key]
            break
    if not rows:
        return []
    out = []
    for r in rows:
        try:
            out.append((str(r[0])[:10], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
        except Exception:
            continue
    return out


def quotes(symbols: Iterable[str], timeout: float = 10.0) -> Dict[str, Dict[str, object]]:
    """实时快照（含五档买一/卖一）。键为 ``113042.SH`` 形式；失败返回 {}。"""
    codes = [tx_code(s) for s in symbols if str(s).strip()]
    if not codes:
        return {}
    try:
        raw = _get(_QUOTE_URL.format(codes=",".join(codes)), timeout=timeout)
    except Exception:
        return {}
    text = raw.decode("gbk", "ignore")
    out: Dict[str, Dict[str, object]] = {}
    for line in text.splitlines():
        if "=" not in line or '"' not in line:
            continue
        head, _, rest = line.partition("=")
        code = head.replace("v_", "").strip()
        body = rest.split('"')[1] if '"' in rest else ""
        f = body.split("~")
        if len(f) < 40 or not f[1]:
            continue

        def num(idx: int) -> float:
            try:
                return float(f[idx])
            except Exception:
                return 0.0

        sym = code[2:].upper() + "." + code[:2].upper()
        out[sym] = {
            "name": f[1], "price": num(3), "prev_close": num(4), "open": num(5),
            "volume": num(6), "bid1": num(9), "bid1_volume": num(10),
            "ask1": num(19), "ask1_volume": num(20),
            "date": (f[30][:4] + "-" + f[30][4:6] + "-" + f[30][6:8]) if len(f) > 30 and len(f[30]) == 8 else "",
            "time": (f[30][8:10] + ":" + f[30][10:12] + ":" + f[30][12:14]) if len(f) > 30 and len(f[30]) >= 14 else "",
            "change_pct": (num(3) / num(4) - 1.0) if num(4) > 0 else 0.0,
            "source": "tencent",
        }
    return out
