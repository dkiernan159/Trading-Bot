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
    MARKING_LEVELS = auto()      # pre-9:30: marking previous day / Asia / London levels
    BUILDING_BOX = auto()        # 9:30-9:45: accumulating the opening range candle
    WAIT_BREAKOUT = auto()       # waiting for a close beyond the box high/low
    WAIT_KEY_LEVEL_FVG = auto()  # waiting for a strong FVG, in the breakout direction, whose
                                  # gap contains a previous-day/Asia/London key level
    WAIT_FILL = auto()           # a valid FVG was found; limit order resting at its midpoint
    IN_TRADE = auto()            # limit order filled, waiting on the runner/broker to close it
    DONE_FOR_DAY = auto()


@dataclass
class EntrySignal:
    direction: Direction
    entry_price: float
    fvg: FairValueGap
    structural_levels: list[float]
    timestamp: datetime


class OpeningRangeStrategy:
    """State machine implementing the NY-open opening-range breakout + key-
    level retest + 1m FVG strategy described in STRATEGY.md.

    Sequence: mark previous-day/Asia/London levels -> form the 9:30-9:45 box
    -> a close beyond the box sets the breakout direction -> wait for a
    strong FVG (in that direction) whose gap actually contains one of the
    marked key levels -> rest a limit order at the FVG's midpoint -> enter
    only once price actually trades back to that midpoint.
    """

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
        self._pending_fvg: FairValueGap | None = None
        self._pending_limit_price: float | None = None

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
                self.state = State.WAIT_KEY_LEVEL_FVG
            elif self.box.low is not None and bar.close < self.box.low:
                self._breakout_direction = Direction.SHORT
                self.state = State.WAIT_KEY_LEVEL_FVG
            return None

        if self.state is State.WAIT_KEY_LEVEL_FVG:
            if fvg is not None and fvg.direction is self._breakout_direction and self._fvg_contains_key_level(fvg):
                self._pending_fvg = fvg
                self._pending_limit_price = (fvg.gap_low + fvg.gap_high) / 2
                self.state = State.WAIT_FILL
            return None

        if self.state is State.WAIT_FILL:
            filled = (
                bar.low <= self._pending_limit_price
                if self._breakout_direction is Direction.LONG
                else bar.high >= self._pending_limit_price
            )
            if filled:
                structural_levels = list(self._levels.all_levels()) if self._levels else []
                if self.box.high is not None:
                    structural_levels.append(self.box.high)
                if self.box.low is not None:
                    structural_levels.append(self.box.low)

                signal = EntrySignal(
                    direction=self._breakout_direction,
                    entry_price=self._pending_limit_price,
                    fvg=self._pending_fvg,
                    structural_levels=structural_levels,
                    timestamp=bar.timestamp,
                )
                self.state = State.IN_TRADE
                self._pending_fvg = None
                self._pending_limit_price = None
                return signal
            return None

        return None

    def _fvg_contains_key_level(self, fvg: FairValueGap) -> bool:
        """True if the FVG's gap overlaps the zone around any marked
        previous-day/Asia/London level -- the "at a key level" requirement.
        A key level's zone is the low/high range of the 15-minute candle
        that set that extreme, not a single exact tick, so the FVG only
        needs to be in that resistance/support area, not pinpoint it."""
        if self._levels is None:
            return False
        return any(
            fvg.gap_low <= zone_high and zone_low <= fvg.gap_high
            for zone_low, zone_high in self._levels.all_zones()
        )

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
        self._pending_fvg = None
        self._pending_limit_price = None
        self.state = State.WAIT_BREAKOUT

    def _start_new_day(self, trading_date: date) -> None:
        self._trading_date = trading_date
        self.box.reset_for_day(trading_date)
        self.state = State.MARKING_LEVELS
        self._breakout_direction = None
        self._levels = None
        self._pending_fvg = None
        self._pending_limit_price = None
