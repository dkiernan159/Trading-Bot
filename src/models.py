from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(Enum):
    LONG = "long"
    SHORT = "short"

    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


@dataclass(frozen=True)
class Bar:
    timestamp: datetime  # timezone-aware, session timezone
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Trade:
    direction: Direction
    entry_price: float
    stop_price: float
    target_price: float
    contracts: int
    entry_time: datetime
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str | None = None  # "target" | "stop" | "flatten"
    stop_source: str = "unknown"  # "fvg" | "swing" | "cap" | "unknown" -- see risk.StopCandidate
    stop_fvg_size: float | None = None

    @property
    def stop_points(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def target_points(self) -> float:
        return abs(self.target_price - self.entry_price)

    def pnl_points(self) -> float | None:
        if self.exit_price is None:
            return None
        sign = 1 if self.direction is Direction.LONG else -1
        return sign * (self.exit_price - self.entry_price)

    def pnl_dollars(self, point_value: float) -> float | None:
        pts = self.pnl_points()
        if pts is None:
            return None
        return pts * point_value * self.contracts

    def unrealized_pnl_dollars(self, current_price: float, point_value: float) -> float:
        """P&L if the trade were closed right now at current_price -- for
        dashboard display only, while the trade is still open (exit_price
        is still None)."""
        sign = 1 if self.direction is Direction.LONG else -1
        return sign * (current_price - self.entry_price) * point_value * self.contracts
