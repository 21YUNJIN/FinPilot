"""Broker gateway abstraction for the read-only first production step.

The existing :class:`Broker` is the small paper/crypto interface.  A real
broker needs connection lifecycle, order lookup, fill lookup and explicit
read-only safety.  This module adds those concepts without forcing every
existing broker implementation to change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .broker import Broker
from .types import AccountSnapshot, Fill, OrderRequest, Position, Quote


class BrokerGatewayError(RuntimeError):
    pass


class BrokerNotConfiguredError(BrokerGatewayError):
    pass


class ReadOnlyBrokerError(BrokerGatewayError):
    pass


@dataclass
class BrokerStatus:
    provider: str
    connected: bool
    read_only: bool
    account_id: str = ""
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class BrokerGateway(Broker, ABC):
    """Broker contract for a production-like execution layer."""

    provider = "unknown"
    read_only = True

    @abstractmethod
    def connect(self) -> BrokerStatus:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> BrokerStatus:
        raise NotImplementedError

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return []

    def get_fills(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def cancel_order(self, broker_order_id: str) -> None:
        raise ReadOnlyBrokerError("cancel is disabled in read-only mode")


class PaperGateway(BrokerGateway):
    """A gateway wrapper around the existing PaperBroker for integration tests."""

    provider = "paper"

    def __init__(self, paper_broker, read_only: bool = False):
        self.paper = paper_broker
        self.read_only = bool(read_only)
        self._status = BrokerStatus(provider=self.provider, connected=False,
                                    read_only=self.read_only, account_id="paper")

    def connect(self) -> BrokerStatus:
        self._status.connected = True
        self._status.message = "paper gateway connected"
        return self._status

    def disconnect(self) -> None:
        self._status.connected = False

    def status(self) -> BrokerStatus:
        return self._status

    def get_account(self) -> AccountSnapshot:
        return self.paper.get_account()

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return self.paper.get_quotes(symbols)

    def submit_order(self, order: OrderRequest, quote: Quote) -> Fill:
        if self.read_only:
            raise ReadOnlyBrokerError("paper gateway is read-only")
        return self.paper.submit_order(order, quote)

    def cancel_all(self) -> None:
        if self.read_only:
            raise ReadOnlyBrokerError("paper gateway is read-only")
        self.paper.cancel_all()


class QmtReadOnlyGateway(BrokerGateway):
    """Safe read-only adapter for miniQMT/xtquant.

    QMT/xtquant versions expose slightly different methods/fields.  This class
    therefore performs connection and account queries dynamically and refuses
    to send orders until a broker-specific order adapter has been validated.
    """

    provider = "qmt"
    read_only = True

    def __init__(self, qmt_path: str, account_id: str, session_id: int = 0):
        if not qmt_path or not account_id:
            raise BrokerNotConfiguredError("qmt_path and account_id are required")
        self.qmt_path = str(qmt_path)
        self.account_id = str(account_id)
        self.session_id = int(session_id)
        self._trader = None
        self._account = None
        self._status = BrokerStatus(provider=self.provider, connected=False,
                                    read_only=True, account_id=self.account_id)

    def connect(self) -> BrokerStatus:
        try:
            from xtquant.xttrader import XtQuantTrader
            from xtquant.xttype import StockAccount
        except ImportError as exc:
            raise BrokerNotConfiguredError(
                "xtquant is not installed; install the miniQMT/xtquant client package "
                "provided by your broker"
            ) from exc
        try:
            self._trader = XtQuantTrader(self.qmt_path, self.session_id)
            self._trader.start()
            result = self._trader.connect()
            if result not in (0, True, None):
                raise BrokerGatewayError(f"QMT connect failed: {result!r}")
            self._account = StockAccount(self.account_id)
            if hasattr(self._trader, "subscribe"):
                sub = self._trader.subscribe(self._account)
                if sub not in (0, True, None):
                    raise BrokerGatewayError(f"QMT subscribe failed: {sub!r}")
        except BrokerGatewayError:
            raise
        except Exception as exc:
            raise BrokerGatewayError(f"QMT connection error: {type(exc).__name__}: {exc}") from exc
        self._status.connected = True
        self._status.message = "QMT read-only gateway connected"
        return self._status

    def disconnect(self) -> None:
        if self._trader is not None and hasattr(self._trader, "stop"):
            try:
                self._trader.stop()
            except Exception:
                pass
        self._status.connected = False

    def status(self) -> BrokerStatus:
        return self._status

    def _query(self, method: str):
        if not self._status.connected or self._trader is None:
            raise BrokerGatewayError("QMT is not connected")
        fn = getattr(self._trader, method, None)
        if fn is None:
            raise BrokerGatewayError(f"QMT API does not expose {method}")
        return fn(self._account)

    def get_account(self) -> AccountSnapshot:
        asset = self._query("query_stock_asset")
        positions = self._query("query_stock_positions")
        cash = float(getattr(asset, "cash", getattr(asset, "可用资金", 0.0)) or 0.0)
        equity = float(getattr(asset, "total_asset", getattr(asset, "总资产", cash)) or cash)
        pos_map: Dict[str, Position] = {}
        for item in positions or []:
            symbol = str(getattr(item, "stock_code", getattr(item, "证券代码", "")))
            if not symbol:
                continue
            qty = float(getattr(item, "volume", getattr(item, "持仓数量", 0.0)) or 0.0)
            cost = float(getattr(item, "open_price", getattr(item, "成本价", 0.0)) or 0.0)
            price = float(getattr(item, "last_price", getattr(item, "最新价", cost)) or cost)
            pos_map[symbol] = Position(symbol=symbol, quantity=qty, average_cost=cost, market_price=price)
        return AccountSnapshot(cash=cash, equity=equity, positions=pos_map)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        raise BrokerNotConfiguredError(
            "QMT quote adapter is not implemented; use the project's real-time quote provider "
            "until a broker-specific quote adapter is validated"
        )

    def submit_order(self, order: OrderRequest, quote: Quote) -> Fill:
        raise ReadOnlyBrokerError("QMT gateway is read-only; order submission is not enabled")

    def cancel_all(self) -> None:
        raise ReadOnlyBrokerError("QMT gateway is read-only; cancellation is not enabled")

    def get_open_orders(self) -> List[Dict[str, Any]]:
        rows = self._query("query_stock_orders")
        out = []
        for item in rows or []:
            out.append({k: getattr(item, k, None) for k in
                        ("order_id", "stock_code", "order_volume", "traded_volume", "order_status")})
        return out


def create_gateway(provider: str, **kwargs) -> BrokerGateway:
    p = str(provider or "").strip().lower()
    if p == "paper":
        from .paper import PaperBroker
        paper_kwargs = dict(kwargs)
        read_only = bool(paper_kwargs.pop("read_only", False))
        return PaperGateway(PaperBroker(**paper_kwargs), read_only=read_only)
    if p == "qmt":
        return QmtReadOnlyGateway(
            qmt_path=str(kwargs.get("qmt_path") or ""),
            account_id=str(kwargs.get("account_id") or ""),
            session_id=int(kwargs.get("session_id") or 0),
        )
    raise BrokerNotConfiguredError(f"unsupported broker provider: {provider}")


__all__ = ["BrokerStatus", "BrokerGateway", "PaperGateway", "QmtReadOnlyGateway",
           "BrokerGatewayError", "BrokerNotConfiguredError", "ReadOnlyBrokerError",
           "create_gateway"]
