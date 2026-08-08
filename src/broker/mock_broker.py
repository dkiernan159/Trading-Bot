from __future__ import annotations

from typing import Callable

from src.broker.base import Broker
from src.models import Bar, Direction


class MockBroker(Broker):
    """Replays a fixed list of bars and simulates bracket-order fills against
    them. No network calls -- used for testing the strategy/risk/runner
    pipeline end-to-end before real ProjectX Gateway credentials exist.

    Fill simulation is intentionally simple: an order placed while handling
    bar[i] can only fill starting bar[i+1] (a bracket can't fill on the same
    bar that created it), and stop is checked before target on each bar.
    """

    def __init__(self, bars: list[Bar]):
        self._bars = bars
        self._callback: Callable[[Bar], None] | None = None
        self._open_orders: dict[str, dict] = {}
        self._next_id = 1

    def connect(self) -> None:
        return None

    def subscribe_bars(self, symbol: str, timeframe_minutes: int, on_bar: Callable[[Bar], None]) -> None:
        self._callback = on_bar

    def run(self) -> None:
        """Drives the whole bar list through the subscribed callback."""
        for bar in self._bars:
            self._check_fills(bar)
            if self._callback is not None:
                self._callback(bar)

    def place_bracket_order(
        self,
        symbol: str,
        direction: Direction,
        contracts: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> str:
        order_id = str(self._next_id)
        self._next_id += 1
        self._open_orders[order_id] = {
            "direction": direction,
            "contracts": contracts,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "status": "open",
        }
        return order_id

    def poll_order_status(self, order_id: str) -> str:
        return self._open_orders[order_id]["status"]

    def flatten_all(self, symbol: str) -> None:
        for order in self._open_orders.values():
            if order["status"] == "open":
                order["status"] = "cancelled"

    def fetch_net_position(self, symbol: str) -> int:
        # Backtests replay a fixed, known bar list -- there's no real
        # broker-side state that could surprise the bot with an orphaned
        # position, so always flat from this method's point of view.
        return 0

    def _check_fills(self, bar: Bar) -> None:
        for order in self._open_orders.values():
            if order["status"] != "open":
                continue
            if order["direction"] is Direction.LONG:
                if bar.low <= order["stop_price"]:
                    order["status"] = "filled_stop"
                elif bar.high >= order["target_price"]:
                    order["status"] = "filled_target"
            else:
                if bar.high >= order["stop_price"]:
                    order["status"] = "filled_stop"
                elif bar.low <= order["target_price"]:
                    order["status"] = "filled_target"
