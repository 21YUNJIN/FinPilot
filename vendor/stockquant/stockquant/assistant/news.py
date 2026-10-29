from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

POSITIVE = ["增长", "盈利", "回购", "增持", "中标", "突破", "超预期", "上调", "扩产", "合作", "涨价", "分红", "扭亏", "创新高", "订单"]
NEGATIVE = ["亏损", "下滑", "减持", "处罚", "立案", "退市", "违约", "裁员", "下调", "风险", "制裁", "冲突", "战争", "爆炸", "关税", "限制", "停牌"]
POLITICAL = ["关税", "制裁", "战争", "冲突", "选举", "导弹", "北约", "欧盟", "美国", "俄罗斯", "乌克兰", "中东", "朝鲜", "地缘"]
MACRO = ["央行", "利率", "降息", "加息", "汇率", "通胀", "财政", "GDP", "PMI", "原油", "黄金", "美联储", "证监会", "国务院"]
MARKET = ["A股", "港股", "美股", "指数", "涨停", "跌停", "资金", "北向", "成交量", "债券", "人民币"]


def _score_text(text: str) -> Dict[str, float]:
    score = 0.0
    for word in POSITIVE:
        if word in text:
            score += 1.0
    for word in NEGATIVE:
        if word in text:
            score -= 1.0
    if not math.isfinite(score):
        score = 0.0
    return {"sentiment": max(-5.0, min(5.0, score))}


def _category(text: str) -> str:
    if any(word in text for word in POLITICAL):
        return "政治/地缘"
    if any(word in text for word in MACRO):
        return "宏观/政策"
    if any(word in text for word in MARKET):
        return "市场"
    return "公司/行业"


def _clean(value: object) -> str:
    return str(value or "").replace("\n", " ").strip()


class NewsEngine:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _symbol_names(self) -> Dict[str, str]:
        cache = self.state_dir / "symbol_names.json"
        if cache.exists():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                pass
        names: Dict[str, str] = {}
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot()
            for _, row in df.iterrows():
                raw = str(row.get("代码", "")).lower()
                code = "".join(ch for ch in raw if ch.isdigit())
                if len(code) == 6:
                    names[code] = _clean(row.get("名称"))
        except Exception:
            names = {}
        cache.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8")
        return names

    def fetch_global(self, limit: int = 80) -> List[Dict[str, object]]:
        import akshare as ak

        items: List[Dict[str, object]] = []
        errors = []
        try:
            df = ak.stock_info_global_em()
            for _, row in df.head(limit).iterrows():
                title = _clean(row.get("标题"))
                content = _clean(row.get("摘要"))
                text = title + " " + content
                items.append(
                    {
                        "title": title,
                        "content": content,
                        "published_at": _clean(row.get("发布时间")),
                        "source": "东方财富全球快讯",
                        "url": _clean(row.get("链接")),
                        "category": _category(text),
                        "sentiment": sentiment,
                        "direct_match": direct,
                        "relevance": 1.0 if direct else 0.25,
                    }
                )
        except Exception as exc:
            errors.append(f"stock_info_global_em: {exc}")
        try:
            df = ak.stock_info_global_cls(symbol="全部")
            for _, row in df.head(max(10, limit // 3)).iterrows():
                title = _clean(row.get("标题"))
                content = _clean(row.get("内容"))
                text = title + " " + content
                published = f"{_clean(row.get('发布日期'))} {_clean(row.get('发布时间'))}"
                items.append(
                    {
                        "title": title,
                        "content": content,
                        "published_at": published,
                        "source": "财联社",
                        "url": "",
                        "category": _category(text),
                        **_score_text(text),
                    }
                )
        except Exception as exc:
            errors.append(f"stock_info_global_cls: {exc}")
        items.sort(key=lambda x: str(x.get("published_at") or ""), reverse=True)
        return items[:limit]

    def fetch_stock_news(self, symbols: Iterable[str], per_symbol: int = 8, workers: int = 4, names: Optional[Dict[str, str]] = None) -> List[Dict[str, object]]:
        import akshare as ak

        unique = sorted(set(symbols))
        names = names or {}

        def fetch(symbol: str):
            df = ak.stock_news_em(symbol=symbol.split(".")[0])
            out = []
            if df is None or df.empty:
                return out
            for _, row in df.head(per_symbol).iterrows():
                title = _clean(row.get("新闻标题"))
                content = _clean(row.get("新闻内容"))
                text = title + " " + content
                code = symbol.split(".")[0]
                company = names.get(code, "")
                direct = code in text or (bool(company) and company in text)
                sentiment = float(_score_text(text).get("sentiment", 0.0))
                if not direct:
                    sentiment *= 0.25
                out.append(
                    {
                        "symbol": symbol,
                        "title": title,
                        "content": content,
                        "published_at": _clean(row.get("发布时间")),
                        "source": _clean(row.get("文章来源")),
                        "url": _clean(row.get("新闻链接")),
                        "category": _category(text),
                        **_score_text(text),
                    }
                )
            return out

        items: List[Dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch, symbol): symbol for symbol in unique}
            for future in as_completed(futures):
                try:
                    items.extend(future.result())
                except Exception:
                    continue
        items.sort(key=lambda x: str(x.get("published_at") or ""), reverse=True)
        return items

    def fetch_gdelt(self, limit: int = 20) -> List[Dict[str, object]]:
        query = "(China OR US OR tariff OR sanctions OR war) (stocks OR market OR chips OR oil)"
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
            {"query": query, "mode": "ArtList", "maxrecords": limit, "format": "json"}
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stockquant-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
            items = []
            for article in payload.get("articles", [])[:limit]:
                title = _clean(article.get("title"))
                source_country = _clean(article.get("sourcecountry"))
                text = title + " " + source_country
                items.append(
                    {
                        "title": title,
                        "content": "",
                        "published_at": _clean(article.get("seendate")),
                        "source": "GDELT · " + _clean(article.get("domain")),
                        "url": _clean(article.get("url")),
                        "category": "政治/地缘",
                        **_score_text(text),
                    }
                )
            return items
        except Exception:
            return []

    def snapshot(self, symbols: Iterable[str], global_limit: int = 80, stock_news_per_symbol: int = 8) -> Dict[str, object]:
        global_items = self.fetch_global(global_limit)
        political = self.fetch_gdelt(20)
        stock_items = self.fetch_stock_news(symbols, stock_news_per_symbol, names=self._symbol_names())
        all_items = global_items + political + stock_items
        risk_words = ["制裁", "战争", "冲突", "关税", "处罚", "立案", "退市", "违约"]
        risks = [item for item in all_items if any(word in str(item.get("title")) + str(item.get("content")) for word in risk_words)]
        snapshot = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "global": global_items,
            "political": political,
            "stock": stock_items,
            "risk_events": risks[:30],
            "counts": {
                "global": len(global_items),
                "political": len(political),
                "stock": len(stock_items),
                "risk": len(risks),
            },
        }
        save_news_snapshot(snapshot, str(self.state_dir / "news_snapshot.json"))
        return snapshot


def save_news_snapshot(snapshot: Dict[str, object], path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_news_snapshot(path: str) -> Dict[str, object]:
    target = Path(path)
    if not target.exists():
        return {"global": [], "political": [], "stock": [], "risk_events": [], "counts": {}}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {"global": [], "political": [], "stock": [], "risk_events": [], "counts": {}}
