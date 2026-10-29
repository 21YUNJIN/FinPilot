from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List

from .types import AccountSnapshot, Fill, OrderRequest, Quote


class Broker(ABC):
    @abstractmethod
    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: OrderRequest, quote: Quote) -> Fill:
        raise NotImplementedError

    @abstractmethod
    def cancel_all(self) -> None:
        raise NotImplementedError
