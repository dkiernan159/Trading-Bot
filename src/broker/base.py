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
    ) -> str | None:
        """Submit an entry + protective stop + take-profit as one bracket.
        Returns a broker-assigned order/trade id, or None if the entry
        itself never actually filled (e.g. a resting limit order that
        never got touched before timing out) -- no stop/target legs are
        placed in that case, since there's no position to protect. Callers
        must treat None as "no trade was taken," not an error."""

    @abstractmethod
    def poll_order_status(self, order_id: str) -> str:
        """Returns one of: 'open', 'filled_target', 'filled_stop', 'cancelled'."""

    @abstractmethod
    def flatten_all(self, symbol: str) -> None:
        """Close any open position immediately (hard safety stop / EOD flatten)."""

    @abstractmethod
    def fetch_net_position(self, symbol: str) -> int:
        """Confirmed live 2026-08-05: a real order can fill and then vanish
        from the bot's own tracking entirely (a connection-instability
        episode left one orphaned, unlogged and unprotected, with real
        money on the line) -- the bot only ever knew about an open trade
        through its own in-memory state, with no way to check that against
        reality. Returns the account's actual net position in this symbol
        (positive = long, negative = short, 0 = flat), so Runner can
        periodically reconcile the broker's own truth against what every
        strategy slot believes, and flatten anything unaccounted for."""

    @abstractmethod
    def cancel_orphaned_orders(self, symbol: str) -> int:
        """Confirmed live 2026-08-07: a resting order can outlive the
        process life that placed it -- a timeout-cancel that itself
        silently fails (see _cancel_order's history) leaves it working on
        the exchange indefinitely, invisible to whatever process is
        running by the time it eventually fills on its own, hours or days
        later, with zero trace in that process's own log. Cancels any
        working order for this symbol that doesn't belong to a bracket
        this broker instance currently considers open, and returns how
        many it cancelled. Implementations should apply a minimum-age
        safety margin before touching an order, so a real order this same
        process just placed a moment ago (still legitimately mid-flight)
        is never mistaken for an orphan."""
