from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from src.config import SessionConfig
from src.models import Bar


@dataclass
class SessionLevelSet:
    """High/low reference levels marked out ahead of the NY session."""

    previous_day_high: float | None
    previous_day_low: float | None
    asia_high: float | None
    asia_low: float | None
    london_high: float | None
    london_low: float | None

    def all_levels(self) -> list[float]:
        return [
            v
            for v in (
                self.previous_day_high,
                self.previous_day_low,
                self.asia_high,
                self.asia_low,
                self.london_high,
                self.london_low,
            )
            if v is not None
        ]


class SessionLevels:
    """Tracks previous-day, Asia-session, and London-session high/low.

    Asia session is assumed to fall on the evening *before* the trading date
    (ET); London session is assumed to fall in the early morning *of* the
    trading date, ahead of the 9:30 NY open. See STRATEGY.md for why these
    windows are configurable assumptions.
    """

    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._bars: list[Bar] = []

    def add_bar(self, bar: Bar) -> None:
        self._bars.append(bar)

    def levels_for(self, trading_date: date) -> SessionLevelSet:
        prev_date = trading_date - timedelta(days=1)

        prev_day_bars = [
            b for b in self._bars if b.timestamp.astimezone(self.tz).date() == prev_date
        ]
        asia_bars = [
            b
            for b in prev_day_bars
            if self.cfg.asia_start <= b.timestamp.astimezone(self.tz).time() <= self.cfg.asia_end
        ]
        london_bars = [
            b
            for b in self._bars
            if b.timestamp.astimezone(self.tz).date() == trading_date
            and self.cfg.london_start <= b.timestamp.astimezone(self.tz).time() <= self.cfg.london_end
        ]

        return SessionLevelSet(
            previous_day_high=max((b.high for b in prev_day_bars), default=None),
            previous_day_low=min((b.low for b in prev_day_bars), default=None),
            asia_high=max((b.high for b in asia_bars), default=None),
            asia_low=min((b.low for b in asia_bars), default=None),
            london_high=max((b.high for b in london_bars), default=None),
            london_low=min((b.low for b in london_bars), default=None),
        )
