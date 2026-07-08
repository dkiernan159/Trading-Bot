from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, auto
from zoneinfo import ZoneInfo

from src.config import BotConfig
from src.fvg import FairValueGap, FvgDetector
from src.models import Bar, Direction
from src.opening_range import OpeningRangeBox
from src.risk import compute_stop_target, find_structural_stop_price
from src.session_levels import SessionLevels, SessionLevelSet
from src.swing_points import SwingPointTracker


class State(Enum):
    MARKING_LEVELS = auto()   # pre-9:30: marking previous day / Asia / London levels (used for stop placement)
    BUILDING_BOX = auto()     # 9:30-9:45: accumulating the opening range candle
    WAIT_BREAKOUT = auto()    # waiting for a close beyond the box high/low
    WAIT_5M_FVG = auto()      # breakout direction set; waiting for a large, unmitigated 5m FVG in that direction
    WAIT_FILL = auto()        # anchor 5m FVG found; limit order resting at a retracement point inside it
    IN_TRADE = auto()         # limit order filled, waiting on the runner/broker to close it
    DONE_FOR_DAY = auto()


@dataclass
class EntrySignal:
    direction: Direction
    entry_price: float
    anchor_fvg: FairValueGap
    stop_price: float
    timestamp: datetime
    stop_source: str = "swing"  # "fvg" | "swing" -- see risk.StopCandidate
    stop_fvg_size: float | None = None


@dataclass
class AnchorRecord:
    """One anchor's full lifecycle -- for near-miss diagnostics only (see
    backtest.py's print_near_miss_anchors), not used by the trading logic
    itself. `outcome` is one of "filled", "superseded" (a nearer/fresher
    anchor replaced it before it ever filled), "invalidated" (the
    breakout thesis failed while it was still live), "no_valid_stop"
    (price retraced far enough into the gap to fill, but no real
    invalidation point -- neither a strong 5m FVG nor a 1m break of
    structure -- sat within the $40-$200 budget beyond that entry, so the
    trade was skipped rather than using an arbitrary or noise-sized stop
    -- see risk.py's find_structural_stop_price / compute_stop_target), or
    "session_ended" (time ran out with it still live, unfilled)."""

    direction: Direction
    gap_low: float
    gap_high: float
    started_at: datetime
    ended_at: datetime
    outcome: str


def _nearest_then_largest(fvgs: list[FairValueGap], current_price: float) -> FairValueGap:
    """Picks whichever gap's midpoint is nearest to current price, breaking
    ties by the larger gap."""
    return max(fvgs, key=lambda f: (-abs((f.gap_low + f.gap_high) / 2 - current_price), f.size))


class OpeningRangeStrategy:
    """State machine implementing the NY-open opening-range breakout + 5m
    FVG anchor entry strategy described in STRATEGY.md.

    Sequence: mark previous-day/Asia/London levels (used for chart/
    dashboard display only, not stop placement -- see risk.py's
    find_structural_stop_price for that) -> form the 9:30-9:45 box -> a
    close beyond the box sets the breakout direction -> wait for a large
    5m FVG in that direction to anchor the move -> a limit order rests at
    a retracement point inside that anchor (see _entry_price -- the exact
    midpoint by default, configurably shallower), kept live/current while
    waiting to fill (see WAIT_FILL: switching to a nearer/fresher
    unmitigated 5m FVG, and updating the resting price, rather than
    staying frozen on the first anchor found). The anchor FVG can form --
    and later get retested -- at any point before the session cutoff, no
    matter how far price has since moved away from it; there's no separate
    time or distance limit on the retest beyond the cutoff itself.
    Mitigation (a gap broken by price trading through its far side) only
    matters when *selecting* a candidate anchor -- a resting limit order
    at any point strictly between the gap's two edges always fills before
    price can reach far enough to break the far edge, so a pending entry
    is never abandoned for having been mitigated; if it were going to be
    mitigated, it already filled first. The breakout thesis itself can
    fail too: a close back through the box's opposite edge while waiting
    on an anchor/entry invalidates it, resetting to WAIT_BREAKOUT rather
    than continuing to chase a same-direction anchor somewhere price has
    already fully reversed away from.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.session_levels = SessionLevels(cfg.session)
        self.box = OpeningRangeBox(cfg.session)
        self.fvg_detector_5m = FvgDetector(cfg.strategy.fvg, self.tz)
        # Alternative, 1-minute-timeframe anchor source -- either detector
        # can supply the anchor/entry once the breakout direction is set,
        # whichever qualifies (see _candidate_anchors). Not the old nested
        # "small FVG inside the big one" design removed in the 5m-only
        # redesign; this pool is searched independently and pooled
        # alongside fvg_detector_5m's, not required to sit inside it.
        self.fvg_detector_1m = FvgDetector(cfg.strategy.entry_fvg, self.tz)
        # Break-of-structure stop fallback (see risk.py's
        # find_structural_stop_price) -- only consulted when no strong 5m
        # FVG sits on the stop side of an entry.
        self.swing_tracker = SwingPointTracker()

        self.state = State.MARKING_LEVELS
        self._trading_date: date | None = None
        self._breakout_direction: Direction | None = None
        self._levels: SessionLevelSet | None = None
        self._anchor_fvg: FairValueGap | None = None
        self._anchor_started_at: datetime | None = None
        self._pending_limit_price: float | None = None
        # Anchors rejected for having no real structural level within the
        # $200 stop budget (see the WAIT_FILL fill check below) -- tracked
        # by identity so a rejected anchor isn't immediately re-picked
        # every subsequent bar just because it's still the nearest
        # unmitigated gap; nothing about it changed by being re-examined,
        # so it stays excluded until mitigated, the day rolls over, or a
        # different anchor supersedes it.
        self._rejected_anchor_ids: set[int] = set()

        # Funnel counters -- how many setups made it past each gate. Lets
        # you tell "nothing happened" apart from "something almost
        # happened" when a backtest window produces zero trades.
        self.stats = {
            "breakouts": 0,
            "breakouts_invalidated": 0,
            "large_fvgs": 0,
            "fills": 0,
        }

        # Every anchor's full lifecycle, filled or not -- near-miss
        # diagnostics only (see AnchorRecord / backtest.py's
        # print_near_miss_anchors), no effect on trading decisions.
        self.anchor_history: list[AnchorRecord] = []

    @property
    def current_session_levels(self) -> SessionLevelSet | None:
        """Previous-day/Asia/London levels marked for the trading day in
        progress (None before 9:30 ET marks them for the day)."""
        return self._levels

    def status_snapshot(self) -> dict:
        """Plain-dict view of what the strategy is currently doing -- for
        the dashboard's live "bot activity" view only (src/runner.py writes
        this out after every bar); has no effect on trading decisions."""
        return {
            "state": self.state.name,
            "direction": self._breakout_direction.value if self._breakout_direction else None,
            "box_high": self.box.high,
            "box_low": self.box.low,
            "anchor_gap_low": self._anchor_fvg.gap_low if self._anchor_fvg else None,
            "anchor_gap_high": self._anchor_fvg.gap_high if self._anchor_fvg else None,
            "pending_limit_price": self._pending_limit_price,
        }

    def _entry_price(self, gap: FairValueGap) -> float:
        """The resting limit price for an anchor: how far price must
        retrace into the gap before counting as filled, as a fraction
        (`cfg.strategy.entry_retracement_pct`) of the gap's own width --
        0.5 is the exact midpoint (the original design); anything less is
        a shallower, easier-to-reach retracement, scaling automatically
        with each anchor's own size rather than a fixed point distance.
        Still provably safe from "mitigated before it could fill" for any
        fraction strictly between 0 and 1: a bar can't break the gap's
        far edge (gap_low for LONG, gap_high for SHORT) without its
        low/high having already reached any point *closer* to where
        price is coming from, which this always is -- so no separate
        mitigation check is needed here, only that the fraction stays
        inside (0, 1)."""
        pct = self.cfg.strategy.entry_retracement_pct
        width = gap.gap_high - gap.gap_low
        if gap.direction is Direction.LONG:
            return gap.gap_high - pct * width
        return gap.gap_low + pct * width

    def _candidate_anchors(self, direction: Direction) -> list[FairValueGap]:
        """Unmitigated FVGs in `direction` from *either* detector -- the 5m
        one or the 1m one (see __init__), pooled together so whichever
        qualifies can anchor the move, not just the 5m one -- excluding any
        already rejected for having no real structural stop within budget
        (see WAIT_FILL). Re-examining a rejected one wouldn't change that
        outcome, since it depends only on entry price vs. the day's fixed
        marked levels."""
        candidates = self.fvg_detector_5m.unmitigated_in_direction(
            direction
        ) + self.fvg_detector_1m.unmitigated_in_direction(direction)
        return [g for g in candidates if id(g) not in self._rejected_anchor_ids]

    def _close_anchor(self, outcome: str, ended_at: datetime) -> None:
        """Records the currently-active anchor's outcome, if there is one
        (a no-op otherwise -- e.g. a breakout invalidated before any
        anchor ever formed). Must be called before self._anchor_fvg is
        replaced or cleared, since it reads the anchor that's about to
        stop being current."""
        if self._anchor_fvg is None:
            return
        self.anchor_history.append(
            AnchorRecord(
                direction=self._breakout_direction,
                gap_low=self._anchor_fvg.gap_low,
                gap_high=self._anchor_fvg.gap_high,
                started_at=self._anchor_started_at,
                ended_at=ended_at,
                outcome=outcome,
            )
        )

    def on_bar(self, bar: Bar) -> EntrySignal | None:
        local = bar.timestamp.astimezone(self.tz)
        trading_date = local.date()
        t = local.time()

        if self._trading_date != trading_date:
            self._start_new_day(trading_date, bar.timestamp)

        self.session_levels.add_bar(bar)
        self.box.add_bar(bar)
        self.fvg_detector_5m.add_bar(bar)
        self.fvg_detector_1m.add_bar(bar)
        self.swing_tracker.add_bar(bar)

        if self.state is State.DONE_FOR_DAY:
            return None

        if self.state is not State.IN_TRADE and t >= self.cfg.session.no_new_entries_after:
            self._close_anchor("session_ended", bar.timestamp)
            # Must actually clear the anchor here, same as every other
            # _close_anchor call site -- otherwise it sits around stale
            # (DONE_FOR_DAY skips straight past this branch for the rest
            # of the day) and gets silently re-recorded by
            # _start_new_day's defensive close on whatever day the next
            # bar happens to arrive, doubling up this same anchor in the
            # history with an inflated, sometimes multi-day, duration.
            # Found via a real 30-day --near-miss backtest where every
            # single "session_ended" anchor appeared twice.
            self._anchor_fvg = None
            self._anchor_started_at = None
            self._pending_limit_price = None
            self.state = State.DONE_FOR_DAY
            return None

        if self.state in (State.WAIT_5M_FVG, State.WAIT_FILL):
            # The breakout thesis itself can fail: if price closes back
            # through the *opposite* side of the box, the original
            # direction call is no longer valid, no matter how "large" or
            # "unmitigated" some same-direction 5m FVG elsewhere still
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
                self._close_anchor("invalidated", bar.timestamp)
                self._breakout_direction = None
                self._anchor_fvg = None
                self._anchor_started_at = None
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
                self.state = State.WAIT_5M_FVG
                self.stats["breakouts"] += 1
            elif self.box.low is not None and bar.close < self.box.low:
                self._breakout_direction = Direction.SHORT
                self.state = State.WAIT_5M_FVG
                self.stats["breakouts"] += 1
            return None

        if self.state is State.WAIT_5M_FVG:
            # Any large, unmitigated 5m FVG in the breakout direction
            # anchors the move -- it doesn't need to have just formed on
            # this bar, it may already have been sitting there, untouched,
            # since earlier in the session. A retracement into its own gap
            # (see _entry_price) is the entry itself, so as soon as one's
            # found, a limit order rests there.
            candidates = self._candidate_anchors(self._breakout_direction)
            if candidates:
                self._anchor_fvg = _nearest_then_largest(candidates, bar.close)
                self._anchor_started_at = bar.timestamp
                self._pending_limit_price = self._entry_price(self._anchor_fvg)
                self.stats["large_fvgs"] += 1
                self.state = State.WAIT_FILL
            return None

        if self.state is State.WAIT_FILL:
            # Keep the anchor current while waiting for price to retrace
            # into it far enough: if a nearer-to-price unmitigated 5m FVG
            # exists now, switch to it and move the resting limit order to
            # its own entry point -- the anchor is never abandoned because
            # it broke, it's just kept up to date so the bot isn't stuck
            # all session on the very first (possibly stale or far-away)
            # anchor it found. No time or distance limit on the retest
            # itself -- price can come back to it whenever it does, right
            # up to the session cutoff.
            candidates = self._candidate_anchors(self._breakout_direction)
            if candidates:
                best_anchor = _nearest_then_largest(candidates, bar.close)
                if best_anchor is not self._anchor_fvg:
                    self._close_anchor("superseded", bar.timestamp)
                    self._anchor_fvg = best_anchor
                    self._anchor_started_at = bar.timestamp
                    self._pending_limit_price = self._entry_price(best_anchor)
                    self.stats["large_fvgs"] += 1

            # No separate mitigation check here: the limit order rests at
            # a point strictly between gap_low and gap_high (see
            # _entry_price), so any bar that reaches far enough to break
            # the gap's far edge has necessarily *also* reached the entry
            # point first (the entry point is always closer to where
            # price is coming from than the far edge is, for any
            # retracement fraction strictly between 0 and 1). A resting
            # limit order fills the instant price touches it -- it
            # doesn't wait to see where price ends up by the close of the
            # bar. So "mitigated before it could fill" can't happen for
            # the pending entry; it always fills. (Mitigation still
            # matters earlier, in the candidate search above -- an
            # already-broken gap is never selected as the anchor in the
            # first place.)
            filled = (
                bar.low <= self._pending_limit_price
                if self._breakout_direction is Direction.LONG
                else bar.high >= self._pending_limit_price
            )
            if filled:
                # Stop is the nearest strong 5m FVG's outer edge on the
                # stop side of entry, or (if none qualifies) the most
                # recent 1m break-of-structure swing point on that side --
                # see risk.py's find_structural_stop_price for the full
                # rule (replaced the previous-day/Asia/London/box-edge
                # approach entirely, 2026-07-08, at the user's explicit
                # correction).
                stop_candidate = find_structural_stop_price(
                    direction=self._breakout_direction,
                    entry_price=self._pending_limit_price,
                    fvg_candidates=self.fvg_detector_5m.unmitigated_in_direction(self._breakout_direction),
                    swing_high=self.swing_tracker.most_recent_swing_high,
                    swing_low=self.swing_tracker.most_recent_swing_low,
                )
                bracket = compute_stop_target(
                    direction=self._breakout_direction,
                    entry_price=self._pending_limit_price,
                    stop_price=stop_candidate.price if stop_candidate is not None else None,
                    max_stop_dollars=self.cfg.strategy.max_stop_dollars,
                    min_stop_dollars=self.cfg.strategy.min_stop_dollars,
                    point_value=self.cfg.instrument.point_value,
                    contracts=self.cfg.position_sizing.contract_size,
                    reward_risk_ratio=self.cfg.strategy.reward_risk_ratio,
                )
                if bracket is None:
                    self._close_anchor("no_valid_stop", bar.timestamp)
                    self._rejected_anchor_ids.add(id(self._anchor_fvg))
                    self._anchor_fvg = None
                    self._anchor_started_at = None
                    self._pending_limit_price = None
                    self.state = State.WAIT_5M_FVG
                    return None

                signal = EntrySignal(
                    direction=self._breakout_direction,
                    entry_price=self._pending_limit_price,
                    anchor_fvg=self._anchor_fvg,
                    stop_price=bracket.stop_price,
                    timestamp=bar.timestamp,
                    stop_source=stop_candidate.source,
                    stop_fvg_size=stop_candidate.fvg_size,
                )
                self._close_anchor("filled", bar.timestamp)
                self.state = State.IN_TRADE
                self._anchor_fvg = None
                self._anchor_started_at = None
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
        self._anchor_started_at = None
        self._pending_limit_price = None
        self.state = State.WAIT_BREAKOUT

    def notify_entry_not_filled(self) -> None:
        """Live trading only: on_bar transitions to IN_TRADE the instant it
        returns a signal, since in backtest a signal always means a real
        fill (the bar-level check IS the fill). Live, a resting limit order
        placed off that same signal can still fail to actually fill at the
        broker -- price may have moved on in the ~60s it takes to detect a
        closed bar and place the order. Runner calls this when that
        happens, so the state machine doesn't get stuck believing it's in a
        trade that was never actually taken.

        Deliberately not the same as notify_trade_closed(won=False): no
        win/loss occurred, so the stand-down-after-loss / stand-down-after-
        win flags don't apply, and the breakout thesis itself is still
        intact -- this goes back to WAIT_5M_FVG to keep hunting within the
        same breakout, not all the way back to WAIT_BREAKOUT."""
        self._anchor_fvg = None
        self._anchor_started_at = None
        self._pending_limit_price = None
        self.state = State.WAIT_5M_FVG

    def _start_new_day(self, trading_date: date, bar_timestamp: datetime) -> None:
        self._trading_date = trading_date
        # Defensive: normally an anchor is already closed out for
        # diagnostics by the no_new_entries_after cutoff before a new
        # day's bar ever arrives, but a gap in the fed history (e.g. a
        # day that ends before the cutoff bar was ever reached) could
        # otherwise leave one dangling unrecorded.
        self._close_anchor("session_ended", bar_timestamp)
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
        self.fvg_detector_5m.clear_active_gaps()
        self.fvg_detector_1m.clear_active_gaps()
        self.swing_tracker.reset()
        self._rejected_anchor_ids = set()
        self.state = State.MARKING_LEVELS
        self._breakout_direction = None
        self._levels = None
        self._anchor_fvg = None
        self._anchor_started_at = None
        self._pending_limit_price = None
