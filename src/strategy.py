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
    MARKING_LEVELS = auto()   # pre-9:30: marking previous day / Asia / London levels (used for stop placement)
    BUILDING_BOX = auto()     # 9:30-9:45: accumulating the opening range candle
    WAIT_BREAKOUT = auto()    # waiting for a close beyond the box high/low
    WAIT_15M_FVG = auto()     # breakout direction set; waiting for a large, unmitigated 15m FVG in that direction
    WAIT_1M_FVG = auto()      # 15m FVG anchored; waiting for a 1m FVG nested inside it
    WAIT_FILL = auto()        # a valid nested 1m FVG was found; limit order resting at its midpoint
    IN_TRADE = auto()         # limit order filled, waiting on the runner/broker to close it
    DONE_FOR_DAY = auto()


@dataclass
class EntrySignal:
    direction: Direction
    entry_price: float
    fvg: FairValueGap
    anchor_fvg: FairValueGap
    structural_levels: list[float]
    timestamp: datetime


def _nearest_then_largest(fvgs: list[FairValueGap], current_price: float) -> FairValueGap:
    """Picks whichever gap's midpoint is nearest to current price, breaking
    ties by the larger gap."""
    return max(fvgs, key=lambda f: (-abs((f.gap_low + f.gap_high) / 2 - current_price), f.size))


class OpeningRangeStrategy:
    """State machine implementing the NY-open opening-range breakout + large
    15m FVG + nested 1m FVG entry strategy described in STRATEGY.md.

    Sequence: mark previous-day/Asia/London levels (kept for stop-loss
    placement, see risk.py) -> form the 9:30-9:45 box -> a close beyond the
    box sets the breakout direction -> wait for a large 15m FVG in that
    direction to anchor the move (whenever it formed, and kept live/current
    rather than frozen on the first pick, see WAIT_1M_FVG) -> once anchored,
    wait for a fresh 1-minute FVG whose midpoint falls inside that 15m FVG's
    range -> a limit order rests at that midpoint, kept tight/precise
    (1m-scale) rather than sized off 15m-candle noise, so the resulting
    stop isn't blown out by ordinary 15m volatility. Mitigation (a gap
    broken by price trading through its far side) only matters when
    *selecting* a candidate 15m anchor or 1m entry -- a resting limit order
    at the midpoint always fills before price can reach far enough to
    break the gap it's sitting inside, so a pending entry is never
    abandoned for having been mitigated.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.session_levels = SessionLevels(cfg.session)
        self.box = OpeningRangeBox(cfg.session)
        self.fvg_detector_15m = FvgDetector(cfg.strategy.fvg, self.tz)
        self.fvg_detector_1m = FvgDetector(cfg.strategy.entry_fvg, self.tz)

        self.state = State.MARKING_LEVELS
        self._trading_date: date | None = None
        self._breakout_direction: Direction | None = None
        self._levels: SessionLevelSet | None = None
        self._anchor_fvg: FairValueGap | None = None
        self._anchor_locked_in_at: datetime | None = None
        self._pending_fvg: FairValueGap | None = None
        self._pending_limit_price: float | None = None

        # Funnel counters -- how many setups made it past each gate. Lets
        # you tell "nothing happened" apart from "something almost
        # happened" when a backtest window produces zero trades.
        self.stats = {
            "breakouts": 0,
            "large_15m_fvgs": 0,
            "nested_1m_fvgs": 0,
            "fills": 0,
        }

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
        self.fvg_detector_15m.add_bar(bar)
        self.fvg_detector_1m.add_bar(bar)

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
                self.state = State.WAIT_15M_FVG
                self.stats["breakouts"] += 1
            elif self.box.low is not None and bar.close < self.box.low:
                self._breakout_direction = Direction.SHORT
                self.state = State.WAIT_15M_FVG
                self.stats["breakouts"] += 1
            return None

        if self.state is State.WAIT_15M_FVG:
            # Any large, unmitigated 15m FVG in the breakout direction
            # anchors the move -- it doesn't need to have just formed on
            # this bar, it may already have been sitting there, untouched,
            # since earlier in the session.
            candidates = self.fvg_detector_15m.unmitigated_in_direction(self._breakout_direction)
            if candidates:
                self._anchor_fvg = _nearest_then_largest(candidates, bar.close)
                self._anchor_locked_in_at = bar.timestamp
                self.stats["large_15m_fvgs"] += 1
                self.state = State.WAIT_1M_FVG
            return None

        if self.state is State.WAIT_1M_FVG:
            # Keep the anchor current: if a nearer-to-price unmitigated 15m
            # FVG exists now (including ones that formed after the current
            # anchor), switch to it. This is *not* "wait for mitigation" --
            # the anchor is never abandoned because it broke, it's just
            # kept up to date so the bot isn't stuck all session on the
            # very first (possibly stale or far-away) anchor it found.
            candidates = self.fvg_detector_15m.unmitigated_in_direction(self._breakout_direction)
            if candidates:
                best_anchor = _nearest_then_largest(candidates, bar.close)
                if best_anchor is not self._anchor_fvg:
                    self._anchor_fvg = best_anchor
                    self._anchor_locked_in_at = bar.timestamp
                    self.stats["large_15m_fvgs"] += 1

            # The nested 1m FVG must be a genuinely new structure that
            # appeared *after* the anchor locked in -- not a gap that was
            # already sitting there (or that formed as part of the same
            # displacement that built the anchor itself). Without this,
            # the bot could claim a coincidentally-overlapping 1m gap the
            # instant the anchor confirms, which looks like "entering as
            # the FVG forms" instead of waiting for an actual retest.
            #
            # "Nested" only requires the 1m gap's midpoint (the actual
            # entry price) to fall inside the anchor's range -- requiring
            # the whole 1m gap to fit inside left very little room in a
            # tight anchor and was starving the bot of entries entirely.
            nested = [
                fvg
                for fvg in self.fvg_detector_1m.unmitigated_in_direction(self._breakout_direction)
                if self._anchor_fvg.gap_low <= (fvg.gap_low + fvg.gap_high) / 2 <= self._anchor_fvg.gap_high
                and fvg.formed_at > self._anchor_locked_in_at
            ]
            if nested:
                fvg = _nearest_then_largest(nested, bar.close)
                self.stats["nested_1m_fvgs"] += 1
                self._pending_fvg = fvg
                self._pending_limit_price = (fvg.gap_low + fvg.gap_high) / 2
                self.state = State.WAIT_FILL
            return None

        if self.state is State.WAIT_FILL:
            # No separate mitigation check here: the limit order rests
            # exactly at the midpoint, strictly between gap_low and
            # gap_high, so any bar that reaches far enough to break the
            # gap's far edge has necessarily *also* reached the midpoint
            # first (the midpoint is always closer to where price is
            # coming from than the far edge is). A resting limit order
            # fills the instant price touches it -- it doesn't wait to see
            # where price ends up by the close of the bar. So "mitigated
            # before it could fill" can't happen for the pending FVG; it
            # always fills. (Mitigation still matters earlier, in
            # WAIT_1M_FVG's candidate search -- a gap that's already
            # broken is never selected as the pending FVG in the first
            # place.)
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
                # The 15m anchor FVG's far boundary is itself a structural
                # level -- a break of it invalidates the whole setup, so
                # it's a sensible stop candidate alongside the marked
                # previous-day/Asia/London/box levels ("the next break of
                # structure" beyond it). Both edges are added; risk.py's
                # nearest-beyond-entry filter picks whichever side (if
                # either) actually applies for this trade's direction.
                structural_levels.append(self._anchor_fvg.gap_low)
                structural_levels.append(self._anchor_fvg.gap_high)

                signal = EntrySignal(
                    direction=self._breakout_direction,
                    entry_price=self._pending_limit_price,
                    fvg=self._pending_fvg,
                    anchor_fvg=self._anchor_fvg,
                    structural_levels=structural_levels,
                    timestamp=bar.timestamp,
                )
                self.state = State.IN_TRADE
                self._anchor_fvg = None
                self._pending_fvg = None
                self._pending_limit_price = None
                self.stats["fills"] += 1
                return signal
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
        self._anchor_fvg = None
        self._anchor_locked_in_at = None
        self._pending_fvg = None
        self._pending_limit_price = None
        self.state = State.WAIT_BREAKOUT

    def _start_new_day(self, trading_date: date) -> None:
        self._trading_date = trading_date
        self.box.reset_for_day(trading_date)
        self.state = State.MARKING_LEVELS
        self._breakout_direction = None
        self._levels = None
        self._anchor_fvg = None
        self._anchor_locked_in_at = None
        self._pending_fvg = None
        self._pending_limit_price = None
