from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, auto
from zoneinfo import ZoneInfo

from src.config import BotConfig
from src.fvg import FairValueGap, FvgDetector
from src.models import Bar, Direction
from src.opening_range import OpeningRangeBox
from src.session_levels import SessionLevels, SessionLevelSet


class State(Enum):
    MARKING_LEVELS = auto()   # pre-9:30: marking previous day / Asia / London levels
    BUILDING_BOX = auto()     # 9:30-9:45: accumulating the opening range candle
    WAIT_BREAKOUT = auto()    # waiting for a close beyond the box high/low
    WAIT_RETEST = auto()      # waiting for price to come back and test the broken level
    WAIT_FVG = auto()         # waiting for a strong 1m FVG in the breakout direction
    IN_TRADE = auto()         # entry signal fired, waiting on the runner/broker to close it
    DONE_FOR_DAY = auto()


@dataclass
class EntrySignal:
    direction: Direction
    entry_price: float
    fvg: FairValueGap
    structural_levels: list[float]
    timestamp: datetime


class OpeningRangeStrategy:
    """State machine implementing the NY-open opening-range breakout + retest
    + 1m FVG strategy described in STRATEGY.md."""

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.session_levels = SessionLevels(cfg.session)
        self.box = OpeningRangeBox(cfg.session)
        self.fvg_detector = FvgDetector(cfg.strategy.fvg)

        self.state = State.MARKING_LEVELS
        self._trading_date: date | None = None
        self._breakout_direction: Direction | None = None
        self._levels: SessionLevelSet | None = None

    @property
    def current_session_levels(self) -> SessionLevelSet | None:
        """Previous-day/Asia/London levels marked for the trading day in
        progress (None before 9:30 ET marks them for the day)."""
        return self._levels

    def on_bar(self, bar: Bar) -> EntrySignal | None:
        local = bar.timestamp.astimezone(self.tz)
        trading_date = local.date()
        t = local.time()

        if self._trading_date != trading_date:
            self._start_new_day(trading_date)

        self.session_levels.add_bar(bar)
        self.box.add_bar(bar)
        fvg = self.fvg_detector.add_bar(bar)

        if self.state is State.DONE_FOR_DAY:
            return None

        if self.state is not State.IN_TRADE and t >= self.cfg.session.no_new_entries_after:
            self.state = State.DONE_FOR_DAY
            return None

        if self.state is State.MARKING_LEVELS:
            if t >= self.cfg.session.ny_open:
                self._levels = self.session_levels.levels_for(trading_date)
                self.state = State.BUILDING_BOX
            return None

        if self.state is State.BUILDING_BOX:
            if self.box.is_formed:
                self.state = State.WAIT_BREAKOUT
            return None

        if self.state is State.WAIT_BREAKOUT:
            if self.box.high is not None and bar.close > self.box.high:
                self._breakout_direction = Direction.LONG
                self.state = State.WAIT_RETEST
            elif self.box.low is not None and bar.close < self.box.low:
                self._breakout_direction = Direction.SHORT
                self.state = State.WAIT_RETEST
            return None

        if self.state is State.WAIT_RETEST:
            broken_level = self.box.high if self._breakout_direction is Direction.LONG else self.box.low
            if self._breakout_direction is Direction.LONG and bar.low <= broken_level:
                self.state = State.WAIT_FVG
            elif self._breakout_direction is Direction.SHORT and bar.high >= broken_level:
                self.state = State.WAIT_FVG
            return None

        if self.state is State.WAIT_FVG:
            if fvg is not None and fvg.direction is self._breakout_direction:
                structural_levels = self._levels.all_levels() if self._levels else []
                structural_levels = list(structural_levels)
                if self.box.high is not None:
                    structural_levels.append(self.box.high)
                if self.box.low is not None:
                    structural_levels.append(self.box.low)

                self.state = State.IN_TRADE
                return EntrySignal(
                    direction=self._breakout_direction,
                    entry_price=bar.close,
                    fvg=fvg,
                    structural_levels=structural_levels,
                    timestamp=bar.timestamp,
                )
            return None

        return None

    def notify_trade_closed(self, won: bool) -> None:
        """Runner calls this once the broker confirms the open trade hit its
        stop or target, so the state machine can decide whether to re-arm."""
        if won and not self.cfg.strategy.reentry.allow_new_setup_after_win:
            self.state = State.DONE_FOR_DAY
            return
        if not won and not self.cfg.strategy.reentry.allow_reentry_after_stop:
            self.state = State.DONE_FOR_DAY
            return

        self._breakout_direction = None
        self.state = State.WAIT_BREAKOUT

    def _start_new_day(self, trading_date: date) -> None:
        self._trading_date = trading_date
        self.box.reset_for_day(trading_date)
        self.state = State.MARKING_LEVELS
        self._breakout_direction = None
        self._levels = None
