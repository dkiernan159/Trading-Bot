from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from src.models import Bar, Direction


class Broker(ABC):
    """Abstract interface the strategy runner depends on. Implement this for
    any data/execution source (mock, backtest, ProjectX Gateway, ...)."""

    @abstractmethod
    def connect(self) -> None:
        """Authenticate / open connections. Raise on failure."""

    @abstractmethod
    def subscribe_bars(self, symbol: str, timeframe_minutes: int, on_bar: Callable[[Bar], None]) -> None:
        """Register a callback invoked with each new closed bar."""

    @abstractmethod
    def place_bracket_order(
        self,
        symbol: str,
        direction: Direction,
        contracts: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> str:
        """Submit an entry + protective stop + take-profit as one bracket.
        Returns a broker-assigned order/trade id."""

    @abstractmethod
    def poll_order_status(self, order_id: str) -> str:
        """Returns one of: 'open', 'filled_target', 'filled_stop', 'cancelled'."""

    @abstractmethod
    def flatten_all(self, symbol: str) -> None:
        """Close any open position immediately (hard safety stop / EOD flatten)."""
