from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

from stockquant.domain import MarketData


class QuoteProvider:
    def get_quotes(self, symbols: List[str]) -> Dict[str, object]:
        raise NotImplementedError


@dataclass
class SnapshotQuoteProvider(QuoteProvider):
    market: MarketData

    def get_quotes(self, symbols: List[str]):
        from .types import Quote
        latest = self.market.dates[-1]
        out = {}
        for symbol in symbols:
            bar = self.market.bar(symbol, latest)
            if bar is None:
                continue
            px = bar.raw_close if bar.raw_close > 0 else bar.close
            out[symbol] = Quote(
                symbol=symbol,
                bid=px,
                ask=px,
                last=px,
                volume=bar.volume,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                source="snapshot",
            )
        return out


class AkshareSpotQuoteProvider(QuoteProvider):
    def __init__(self, ttl_seconds: int = 20):
        self.ttl_seconds = ttl_seconds
        self._last_fetch = 0.0
        self._cache = None

    def get_quotes(self, symbols: List[str]):
        import time
        import akshare as ak
        from .types import Quote
        now = time.time()
        if self._cache is None or now - self._last_fetch > self.ttl_seconds:
            df = ak.stock_zh_a_spot()
            self._cache = df
            self._last_fetch = now
        wanted = {s.upper() for s in symbols}
        out = {}
        for _, row in self._cache.iterrows():
            raw = str(row.get("代码", "")).lower()
            code = "".join(ch for ch in raw if ch.isdigit())
            exchange = "SH" if raw.startswith("sh") else "BJ" if raw.startswith("bj") else "SZ"
            symbol = f"{code}.{exchange}"
            if symbol not in wanted:
                continue
            price = float(row.get("最新价") or 0.0)
            if price <= 0:
                continue
            out[symbol] = Quote(symbol=symbol, bid=price, ask=price, last=price, volume=float(row.get("成交量") or 0.0), timestamp_utc=datetime.now(timezone.utc).isoformat(), source="akshare_spot")
        return out
