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
    WAIT_FILL = auto()        # anchor 15m FVG found; limit order resting at its own midpoint
    IN_TRADE = auto()         # limit order filled, waiting on the runner/broker to close it
    DONE_FOR_DAY = auto()


@dataclass
class EntrySignal:
    direction: Direction
    entry_price: float
    anchor_fvg: FairValueGap
    structural_levels: list[float]
    timestamp: datetime


def _nearest_then_largest(fvgs: list[FairValueGap], current_price: float) -> FairValueGap:
    """Picks whichever gap's midpoint is nearest to current price, breaking
    ties by the larger gap."""
    return max(fvgs, key=lambda f: (-abs((f.gap_low + f.gap_high) / 2 - current_price), f.size))


class OpeningRangeStrategy:
    """State machine implementing the NY-open opening-range breakout + 15m
    FVG anchor entry strategy described in STRATEGY.md.

    Sequence: mark previous-day/Asia/London levels (kept for stop-loss
    placement, see risk.py) -> form the 9:30-9:45 box -> a close beyond the
    box sets the breakout direction -> wait for a large 15m FVG in that
    direction to anchor the move -> a limit order rests at that anchor's
    own midpoint, kept live/current while waiting to fill (see WAIT_FILL:
    switching to a nearer/fresher unmitigated 15m FVG, and updating the
    resting price, rather than staying frozen on the first anchor found).
    Mitigation (a gap broken by price trading through its far side) only
    matters when *selecting* a candidate anchor -- a resting limit order
    at the midpoint always fills before price can reach far enough to
    break the gap it's sitting inside (the midpoint is strictly between
    the gap's two edges), so a pending entry is never abandoned for
    having been mitigated; if it were going to be mitigated, it already
    filled first. The breakout thesis itself can fail too: a close back
    through the box's opposite edge while waiting on an anchor/entry
    invalidates it, resetting to WAIT_BREAKOUT rather than continuing to
    chase a same-direction anchor somewhere price has already fully
    reversed away from.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.session_levels = SessionLevels(cfg.session)
        self.box = OpeningRangeBox(cfg.session)
        self.fvg_detector_15m = FvgDetector(cfg.strategy.fvg, self.tz)

        self.state = State.MARKING_LEVELS
        self._trading_date: date | None = None
        self._breakout_direction: Direction | None = None
        self._levels: SessionLevelSet | None = None
        self._anchor_fvg: FairValueGap | None = None
        self._pending_limit_price: float | None = None

        # Funnel counters -- how many setups made it past each gate. Lets
        # you tell "nothing happened" apart from "something almost
        # happened" when a backtest window produces zero trades.
        self.stats = {
            "breakouts": 0,
            "breakouts_invalidated": 0,
            "large_15m_fvgs": 0,
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

        if self.state is State.DONE_FOR_DAY:
            return None

        if self.state is not State.IN_TRADE and t >= self.cfg.session.no_new_entries_after:
            self.state = State.DONE_FOR_DAY
            return None

        if self.state in (State.WAIT_15M_FVG, State.WAIT_FILL):
            # The breakout thesis itself can fail: if price closes back
            # through the *opposite* side of the box, the original
            # direction call is no longer valid, no matter how "large" or
            # "unmitigated" some same-direction 15m FVG elsewhere still
            # looks. Without this, the bot could keep hunting for a
            # same-direction anchor/entry arbitrarily far from where the
            # breakout actually happened -- e.g. a bounce well below the
            # entire opening range, long after a LONG breakout has
            # completely round-tripped and reversed.
            breakout_failed = (
                bar.close < self.box.low
                if self._breakout_direction is Direction.LONG
                else bar.close > self.box.high
            )
            if breakout_failed:
                self.stats["breakouts_invalidated"] += 1
                self._breakout_direction = None
                self._anchor_fvg = None
                self._pending_limit_price = None
                self.state = State.WAIT_BREAKOUT
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
            # since earlier in the session. Its own midpoint is the entry
            # itself, so as soon as one's found, a limit order rests there.
            candidates = self.fvg_detector_15m.unmitigated_in_direction(self._breakout_direction)
            if candidates:
                self._anchor_fvg = _nearest_then_largest(candidates, bar.close)
                self._pending_limit_price = (self._anchor_fvg.gap_low + self._anchor_fvg.gap_high) / 2
                self.stats["large_15m_fvgs"] += 1
                self.state = State.WAIT_FILL
            return None

        if self.state is State.WAIT_FILL:
            # Keep the anchor current while waiting for price to retrace
            # to its midpoint: if a nearer-to-price unmitigated 15m FVG
            # exists now, switch to it and move the resting limit order to
            # its midpoint -- the anchor is never abandoned because it
            # broke, it's just kept up to date so the bot isn't stuck all
            # session on the very first (possibly stale or far-away)
            # anchor it found.
            candidates = self.fvg_detector_15m.unmitigated_in_direction(self._breakout_direction)
            if candidates:
                best_anchor = _nearest_then_largest(candidates, bar.close)
                if best_anchor is not self._anchor_fvg:
                    self._anchor_fvg = best_anchor
                    self._pending_limit_price = (best_anchor.gap_low + best_anchor.gap_high) / 2
                    self.stats["large_15m_fvgs"] += 1

            # No separate mitigation check here: the limit order rests
            # exactly at the anchor's midpoint, strictly between gap_low
            # and gap_high, so any bar that reaches far enough to break
            # the gap's far edge has necessarily *also* reached the
            # midpoint first (the midpoint is always closer to where
            # price is coming from than the far edge is). A resting limit
            # order fills the instant price touches it -- it doesn't wait
            # to see where price ends up by the close of the bar. So
            # "mitigated before it could fill" can't happen for the
            # pending entry; it always fills. (Mitigation still matters
            # earlier, in the candidate search above -- an already-broken
            # gap is never selected as the anchor in the first place.)
            filled = (
                bar.low <= self._pending_limit_price
                if self._breakout_direction is Direction.LONG
                else bar.high >= self._pending_limit_price
            )
            if filled:
                # Deliberately does NOT include the anchor FVG's own
                # boundaries: now that entry sits exactly at the anchor's
                # midpoint, its near/far edges are always exactly half the
                # anchor's own gap width from entry -- a pure arithmetic
                # consequence of where entry was defined, not a real break
                # of structure. Left in, that half-gap distance was
                # provably always the nearest candidate (checked against 3
                # real losing trades: stop distances of $55.75/$56.25/$27
                # matched exactly half the anchor's width in every case),
                # so it silently overrode the marked previous-day/Asia/
                # London/box levels even when those were legitimately
                # closer to representing an actual invalidation and would
                # have used much more of the $200 budget. Only the marked
                # session levels and the box edges are real structure here.
                structural_levels = list(self._levels.all_levels()) if self._levels else []
                if self.box.high is not None:
                    structural_levels.append(self.box.high)
                if self.box.low is not None:
                    structural_levels.append(self.box.low)

                signal = EntrySignal(
                    direction=self._breakout_direction,
                    entry_price=self._pending_limit_price,
                    anchor_fvg=self._anchor_fvg,
                    structural_levels=structural_levels,
                    timestamp=bar.timestamp,
                )
                self.state = State.IN_TRADE
                self._anchor_fvg = None
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
        self._pending_limit_price = None
        self.state = State.WAIT_BREAKOUT

    def _start_new_day(self, trading_date: date) -> None:
        self._trading_date = trading_date
        self.box.reset_for_day(trading_date)
        # Drop any still-unmitigated FVGs from previous days -- otherwise
        # a gap that simply never got revisited could sit in the pool
        # indefinitely and get picked as an anchor/entry days or weeks
        # later, making results depend on how far back the bar history
        # happens to start (a real bug: a 30-day backtest and a 7-day
        # backtest were producing different results on the exact same
        # calendar day). The candle history itself (used for the
        # average-range baseline) is left alone, so it's already
        # populated with real pre-market/overnight data by 9:30.
        self.fvg_detector_15m.clear_active_gaps()
        self.state = State.MARKING_LEVELS
        self._breakout_direction = None
        self._levels = None
        self._anchor_fvg = None
        self._pending_limit_price = None
