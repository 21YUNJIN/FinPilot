from __future__ import annotations

import os
from typing import Dict, List, Optional

from .broker import Broker
from .types import AccountSnapshot, Fill, OrderRequest, Position, Quote


class CcxtBroker(Broker):
    """CCXT broker adapter for spot crypto testnet/demo/live trading.

    OKX Demo Trading uses the normal REST host with the
    ``x-simulated-trading: 1`` header.  Live orders are blocked unless the
    explicit risk confirmation environment variable is present.
    """

    def __init__(
        self,
        exchange_id: str,
        testnet: bool = True,
        api_key_env: str = "STOCKQUANT_API_KEY",
        secret_env: str = "STOCKQUANT_API_SECRET",
        passphrase_env: str = "STOCKQUANT_API_PASSPHRASE",
        proxy_url: str = "",
        simulated_trading: Optional[bool] = None,
        public: bool = False,
    ):
        try:
            import ccxt
        except Exception as exc:
            raise RuntimeError("CcxtBroker requires ccxt: pip install ccxt") from exc

        exchange_class = getattr(ccxt, exchange_id)
        proxy_url = proxy_url or os.environ.get("STOCKQUANT_HTTP_PROXY", "http://127.0.0.1:7897")
        options = {
            "apiKey": "" if public else os.environ.get(api_key_env, ""),
            "secret": "" if public else os.environ.get(secret_env, ""),
            "password": "" if public else os.environ.get(passphrase_env, ""),
            "enableRateLimit": True,
            "timeout": 20000,
        }
        if proxy_url:
            options["httpsProxy"] = proxy_url
        self.exchange = exchange_class(options)
        self.testnet = testnet
        env_simulated = os.environ.get("STOCKQUANT_OKX_SIMULATED", "").lower() in {"1", "true", "yes"}
        enabled_simulation = env_simulated and testnet if simulated_trading is None else simulated_trading
        self.simulated = bool(enabled_simulation) and not public

        if exchange_id.lower() == "okx" and self.simulated:
            # This is the supported OKX Demo Trading mode.  CCXT 4.5.64's
            # OKX sign() rebuilds the request headers and drops the header set
            # by set_sandbox_mode(), so inject it after signing as well.
            if hasattr(self.exchange, "set_sandbox_mode"):
                self.exchange.set_sandbox_mode(True)
            original_sign = self.exchange.sign

            def sign_with_demo_header(path, api="public", method="GET", params=None, headers=None, body=None):
                signed = original_sign(path, api, method, params or {}, headers, body)
                request_headers = signed.get("headers") or {}
                request_headers["x-simulated-trading"] = "1"
                signed["headers"] = request_headers
                return signed

            self.exchange.sign = sign_with_demo_header
            self.exchange.headers = {**getattr(self.exchange, "headers", {}), "x-simulated-trading": "1"}
        elif testnet and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)

        # OKX's load_markets() otherwise calls the private currency endpoint.
        # Market metadata is public, so force the public market-loading path.
        self.exchange.options["fetchCurrencies"] = False
        self.exchange.load_markets()

    def _market_step(self, symbol: str) -> float:
        try:
            market = self.exchange.market(symbol)
            precision = market.get("precision", {}) or {}
            step = precision.get("amount")
            if step:
                return float(step)
            minimum = ((market.get("limits", {}) or {}).get("amount", {}) or {}).get("min")
            return float(minimum or 0.01)
        except Exception:
            return 0.01

    def get_account(self) -> AccountSnapshot:
        balance = self.exchange.fetch_balance()
        total = balance.get("total", {})
        quote_currency = "USDT"
        cash = float(total.get(quote_currency, 0.0) or 0.0)
        positions = {}
        for asset, qty in total.items():
            qty = float(qty or 0.0)
            if asset == quote_currency or qty <= 0:
                continue
            symbol = f"{asset}/{quote_currency}"
            price = 0.0
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price = float(ticker.get("last") or 0.0)
            except Exception:
                price = 0.0
            positions[symbol] = Position(symbol=symbol, quantity=qty, average_cost=price, market_price=price)
        equity = cash + sum(p.quantity * p.market_price for p in positions.values())
        return AccountSnapshot(cash=cash, equity=equity, positions=positions, peak_equity=equity)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out = {}
        for symbol in symbols:
            ticker = self.exchange.fetch_ticker(symbol)
            out[symbol] = Quote(
                symbol=symbol,
                bid=float(ticker.get("bid") or ticker.get("last") or 0.0),
                ask=float(ticker.get("ask") or ticker.get("last") or 0.0),
                last=float(ticker.get("last") or 0.0),
                volume=float(ticker.get("baseVolume") or 0.0),
                source=f"ccxt:{self.exchange.id}",
                amount_step=self._market_step(symbol),
            )
        return out

    def submit_order(self, order: OrderRequest, quote: Quote) -> Fill:
        if not self.testnet and os.environ.get("STOCKQUANT_LIVE_CONFIRM") != "YES_I_ACCEPT_RISK":
            raise RuntimeError("Live trading blocked: set STOCKQUANT_LIVE_CONFIRM=YES_I_ACCEPT_RISK")

        market = self.exchange.market(order.symbol)
        amount = float(self.exchange.amount_to_precision(order.symbol, order.quantity))
        limits = market.get("limits", {}) or {}
        min_amount = ((limits.get("amount", {}) or {}).get("min") or 0.0)
        min_cost = ((limits.get("cost", {}) or {}).get("min") or 0.0)
        if amount <= 0 or (min_amount and amount < float(min_amount)):
            raise ValueError(f"order amount below exchange minimum for {order.symbol}")

        price = None
        if order.order_type == "limit":
            if not order.limit_price or order.limit_price <= 0:
                raise ValueError("limit order requires a positive limit_price")
            price = float(self.exchange.price_to_precision(order.symbol, order.limit_price))
        reference_price = price if price is not None else (quote.ask if order.side.lower() == "buy" else quote.bid)
        if min_cost and amount * reference_price < float(min_cost):
            raise ValueError(f"order notional below exchange minimum for {order.symbol}")

        response = self.exchange.create_order(
            order.symbol,
            order.order_type,
            order.side.lower(),
            amount,
            price,
            dict(order.params),
        )
        fill = Fill(
            order_id=str(response.get("id", "")),
            symbol=order.symbol,
            side=order.side.lower(),
            quantity=float(response.get("amount") or amount),
            price=float(response.get("average") or response.get("price") or reference_price),
            reason=order.reason,
            strategy_id=order.strategy_id,
        )
        return fill

    def fetch_open_orders(self, symbol: Optional[str] = None):
        return self.exchange.fetch_open_orders(symbol)

    def fetch_order(self, order_id: str, symbol: Optional[str] = None):
        return self.exchange.fetch_order(order_id, symbol)

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> None:
        self.exchange.cancel_order(order_id, symbol)

    def cancel_all(self) -> None:
        for order in self.exchange.fetch_open_orders():
            self.exchange.cancel_order(order["id"], order.get("symbol"))
