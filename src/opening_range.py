from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.config import SessionConfig
from src.models import Bar


class OpeningRangeBox:
    """Tracks the high/low of the 9:30-9:45 ET opening range ("the box").

    Feed it every bar (1m bars are fine); it accumulates high/low for bars
    whose timestamp falls within [ny_open, opening_range_end) on the current
    trading date. The box is considered formed once a bar at/after
    opening_range_end for that date has been seen.
    """

    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._trading_date: date | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._formed: bool = False

    def reset_for_day(self, trading_date: date) -> None:
        self._trading_date = trading_date
        self._high = None
        self._low = None
        self._formed = False

    def add_bar(self, bar: Bar) -> None:
        local = bar.timestamp.astimezone(self.tz)
        if self._trading_date is None or local.date() != self._trading_date:
            self.reset_for_day(local.date())

        t = local.time()
        if self.cfg.ny_open <= t < self.cfg.opening_range_end:
            self._high = bar.high if self._high is None else max(self._high, bar.high)
            self._low = bar.low if self._low is None else min(self._low, bar.low)
        elif t >= self.cfg.opening_range_end and self._high is not None:
            self._formed = True

    @property
    def is_formed(self) -> bool:
        return self._formed

    @property
    def high(self) -> float | None:
        return self._high

    @property
    def low(self) -> float | None:
        return self._low
