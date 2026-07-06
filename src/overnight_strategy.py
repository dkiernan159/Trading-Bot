from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum, auto
from zoneinfo import ZoneInfo

from src.config import BotConfig, SessionConfig
from src.fvg import FairValueGap, FvgDetector
from src.models import Bar, Direction
from src.risk import compute_stop_target
from src.session_levels import SessionLevels, SessionLevelSet
from src.strategy import AnchorRecord


class State(Enum):
    IDLE = auto()            # outside the Asia/London window entirely
    WAIT_ANCHOR = auto()     # in-window; hunting a large, unmitigated pooled 15m/30m FVG (either direction)
    WAIT_ENTRY = auto()      # anchor locked (direction set); hunting a pooled 5m/1m FVG nested inside it
    WAIT_FILL = auto()       # nested entry chosen; limit order resting at its own retracement point
    IN_TRADE = auto()        # limit order filled, waiting on the runner/broker to close it
    DONE_FOR_NIGHT = auto()


@dataclass
class OvernightEntrySignal:
    direction: Direction
    entry_price: float
    anchor_fvg: FairValueGap
    entry_fvg: FairValueGap
    structural_levels: list[float]
    timestamp: datetime


def _nearest_then_largest(fvgs: list[FairValueGap], current_price: float) -> FairValueGap:
    """Picks whichever gap's midpoint is nearest to current price, breaking
    ties by the larger gap. Same rule the day strategy uses."""
    return max(fvgs, key=lambda f: (-abs((f.gap_low + f.gap_high) / 2 - current_price), f.size))


def _in_overnight_window(t: dtime, cfg: SessionConfig) -> bool:
    """Asia (19:00-23:59) -> gap (00:00-02:00) -> London (02:00-05:00) is
    treated as one continuous window rather than resetting in the gap, so
    an anchor/entry hunt in progress isn't abandoned just because there's
    no session label active at that exact moment."""
    return t >= cfg.asia_start or t <= cfg.london_end


def _night_date(local_dt: datetime, cfg: SessionConfig) -> date:
    """The trading_date this overnight window's marked levels belong to --
    matches SessionLevels.levels_for's own mapping (evening-before for Asia,
    same-morning for London), so both halves of one continuous overnight
    window always resolve to the same date even though the wall-clock date
    itself rolls over at midnight in between."""
    return local_dt.date() + timedelta(days=1) if local_dt.time() >= cfg.asia_start else local_dt.date()


class OvernightMomentumStrategy:
    """State machine for the Asia/London overnight momentum layer described
    in STRATEGY.md: a large (15m or 30m, pooled) FVG sets direction directly
    -- no box or breakout, since there isn't one outside NY hours -- then a
    smaller (5m or 1m, pooled) FVG that forms *after* the anchor locks in,
    with its own midpoint inside the anchor's gap, is the precise entry.
    Runs in parallel with, and entirely independently of, OpeningRangeStrategy
    (src/strategy.py) -- a separate instance of each is fed the same bars.

    Mirrors the day strategy's "keep live" pattern: while hunting for (or
    resting a limit inside) an entry, a nearer/fresher anchor or nested FVG
    supersedes the current one rather than freezing on the first found. If
    the current anchor itself becomes mitigated with nothing in the same
    direction to replace it, the whole thesis is invalidated and the hunt
    restarts from scratch (direction included) -- earlier versions of this
    design (see git history) left a mitigated anchor sitting there stale
    instead of explicitly invalidating it.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.session_levels = SessionLevels(cfg.session)
        ov = cfg.strategy.overnight
        self.anchor_detector_15m = FvgDetector(ov.anchor_15m, self.tz)
        self.anchor_detector_30m = FvgDetector(ov.anchor_30m, self.tz)
        self.entry_detector_5m = FvgDetector(ov.entry_5m, self.tz)
        self.entry_detector_1m = FvgDetector(ov.entry_1m, self.tz)

        self.state = State.IDLE
        self._night_date: date | None = None
        self._direction: Direction | None = None
        self._anchor_fvg: FairValueGap | None = None
        self._anchor_locked_in_at: datetime | None = None
        self._nested_fvg: FairValueGap | None = None
        self._pending_limit_price: float | None = None
        # Nested candidates rejected for having no real structural stop
        # within budget -- tracked by identity, same rationale as the day
        # strategy's _rejected_anchor_ids (see strategy.py).
        self._rejected_nested_ids: set[int] = set()

        self.stats = {
            "anchors": 0,
            "anchors_invalidated": 0,
            "nested_entries": 0,
            "fills": 0,
        }
        self.anchor_history: list[AnchorRecord] = []

    @property
    def current_session_levels(self) -> SessionLevelSet | None:
        """Previous-day/Asia/London levels for the night in progress,
        recomputed live (unlike the day strategy, Asia/London bars for this
        very window are often still accumulating while a trade is being
        considered, so there's no single fixed marking point)."""
        if self._night_date is None:
            return None
        return self.session_levels.levels_for(self._night_date)

    def _entry_price(self, gap: FairValueGap) -> float:
        pct = self.cfg.strategy.entry_retracement_pct
        width = gap.gap_high - gap.gap_low
        if gap.direction is Direction.LONG:
            return gap.gap_high - pct * width
        return gap.gap_low + pct * width

    def _candidate_anchors(self, direction: Direction) -> list[FairValueGap]:
        return self.anchor_detector_15m.unmitigated_in_direction(
            direction
        ) + self.anchor_detector_30m.unmitigated_in_direction(direction)

    def _all_anchor_candidates(self) -> list[FairValueGap]:
        return self._candidate_anchors(Direction.LONG) + self._candidate_anchors(Direction.SHORT)

    def _candidate_nested(self) -> list[FairValueGap]:
        """Pooled 5m/1m FVGs in the anchor's direction whose own midpoint
        falls inside the anchor's gap and that formed strictly after the
        anchor locked in -- a coincidentally-overlapping gap that was
        already sitting there doesn't count as a genuine retest."""
        pooled = self.entry_detector_5m.unmitigated_in_direction(
            self._direction
        ) + self.entry_detector_1m.unmitigated_in_direction(self._direction)
        return [
            g
            for g in pooled
            if self._anchor_fvg.gap_low <= (g.gap_low + g.gap_high) / 2 <= self._anchor_fvg.gap_high
            and g.formed_at > self._anchor_locked_in_at
            and id(g) not in self._rejected_nested_ids
        ]

    def _close_anchor(self, outcome: str, ended_at: datetime) -> None:
        if self._anchor_fvg is None:
            return
        self.anchor_history.append(
            AnchorRecord(
                direction=self._direction,
                gap_low=self._anchor_fvg.gap_low,
                gap_high=self._anchor_fvg.gap_high,
                started_at=self._anchor_locked_in_at,
                ended_at=ended_at,
                outcome=outcome,
            )
        )

    def _reset_hunt_state(self) -> None:
        self._direction = None
        self._anchor_fvg = None
        self._anchor_locked_in_at = None
        self._nested_fvg = None
        self._pending_limit_price = None

    def _start_new_night(self, night_date: date, bar_timestamp: datetime) -> None:
        self._night_date = night_date
        self._close_anchor("session_ended", bar_timestamp)
        self.anchor_detector_15m.clear_active_gaps()
        self.anchor_detector_30m.clear_active_gaps()
        self.entry_detector_5m.clear_active_gaps()
        self.entry_detector_1m.clear_active_gaps()
        self._rejected_nested_ids = set()
        self._reset_hunt_state()
        self.state = State.WAIT_ANCHOR

    def on_bar(self, bar: Bar) -> OvernightEntrySignal | None:
        local = bar.timestamp.astimezone(self.tz)
        t = local.time()
        in_window = _in_overnight_window(t, self.cfg.session)

        self.session_levels.add_bar(bar)
        self.anchor_detector_15m.add_bar(bar)
        self.anchor_detector_30m.add_bar(bar)
        self.entry_detector_5m.add_bar(bar)
        self.entry_detector_1m.add_bar(bar)

        if self.state is State.IN_TRADE:
            # Nothing to do until the runner/backtest harness calls
            # notify_trade_closed -- deliberately not re-evaluated against
            # in_window/night_date so an overnight trade still open when the
            # window ends (e.g. still running into the 9:30 day session) is
            # left alone rather than having its bookkeeping reset mid-trade.
            return None

        if not in_window:
            if self.state is not State.IDLE:
                self._close_anchor("session_ended", bar.timestamp)
                self._reset_hunt_state()
                self.state = State.IDLE
            return None

        night_date = _night_date(local, self.cfg.session)
        if self._night_date != night_date:
            self._start_new_night(night_date, bar.timestamp)

        if self.state is State.DONE_FOR_NIGHT:
            return None

        if self.state in (State.WAIT_ENTRY, State.WAIT_FILL):
            # The anchor itself can be invalidated (mitigated with nothing
            # in the same direction to replace it), same idea as the day
            # strategy's breakout-invalidation check -- except here there's
            # no box to fall back to, so invalidation means restarting the
            # whole hunt (direction included) rather than reverting to a
            # single well-known "wait for breakout" state.
            candidates = self._candidate_anchors(self._direction)
            if not candidates:
                self.stats["anchors_invalidated"] += 1
                self._close_anchor("invalidated", bar.timestamp)
                self._reset_hunt_state()
                self.state = State.WAIT_ANCHOR
                return None

            best_anchor = _nearest_then_largest(candidates, bar.close)
            if best_anchor is not self._anchor_fvg:
                self._close_anchor("superseded", bar.timestamp)
                self._anchor_fvg = best_anchor
                self._anchor_locked_in_at = bar.timestamp
                self._nested_fvg = None
                self._pending_limit_price = None
                self.stats["anchors"] += 1
                self.state = State.WAIT_ENTRY

        if self.state is State.WAIT_ANCHOR:
            candidates = self._all_anchor_candidates()
            if candidates:
                self._anchor_fvg = _nearest_then_largest(candidates, bar.close)
                self._direction = self._anchor_fvg.direction
                self._anchor_locked_in_at = bar.timestamp
                self.stats["anchors"] += 1
                self.state = State.WAIT_ENTRY
            return None

        if self.state is State.WAIT_ENTRY:
            nested = self._candidate_nested()
            if nested:
                self._nested_fvg = _nearest_then_largest(nested, bar.close)
                self._pending_limit_price = self._entry_price(self._nested_fvg)
                self.stats["nested_entries"] += 1
                self.state = State.WAIT_FILL
            return None

        if self.state is State.WAIT_FILL:
            nested = self._candidate_nested()
            if nested:
                best_nested = _nearest_then_largest(nested, bar.close)
                if best_nested is not self._nested_fvg:
                    self._nested_fvg = best_nested
                    self._pending_limit_price = self._entry_price(best_nested)
                    self.stats["nested_entries"] += 1

            # No separate mitigation check on the resting nested FVG itself:
            # the same proof the day strategy relies on (see strategy.py's
            # WAIT_FILL) holds here too -- a retracement price strictly
            # between a gap's two edges always fills before price can reach
            # far enough to break the far edge, so it can't be mitigated out
            # from under a resting limit order.
            filled = (
                bar.low <= self._pending_limit_price
                if self._direction is Direction.LONG
                else bar.high >= self._pending_limit_price
            )
            if filled:
                levels = self.current_session_levels
                structural_levels = list(levels.all_levels()) if levels else []
                # The anchor's own edges are real structure here (unlike the
                # day strategy): entry sits at the *nested* FVG's own
                # retracement point, not at a fixed fraction of the anchor's
                # own width, so the anchor's edges aren't just a pre-known
                # arithmetic distance from entry -- a break of either is a
                # genuine invalidation of the anchor thesis.
                structural_levels.append(self._anchor_fvg.gap_low)
                structural_levels.append(self._anchor_fvg.gap_high)

                bracket = compute_stop_target(
                    direction=self._direction,
                    entry_price=self._pending_limit_price,
                    structural_levels=structural_levels,
                    max_stop_dollars=self.cfg.strategy.max_stop_dollars,
                    min_stop_dollars=self.cfg.strategy.min_stop_dollars,
                    point_value=self.cfg.instrument.point_value,
                    contracts=self.cfg.position_sizing.contract_size,
                    reward_risk_ratio=self.cfg.strategy.reward_risk_ratio,
                )
                if bracket is None:
                    self._rejected_nested_ids.add(id(self._nested_fvg))
                    self._nested_fvg = None
                    self._pending_limit_price = None
                    self.state = State.WAIT_ENTRY
                    return None

                signal = OvernightEntrySignal(
                    direction=self._direction,
                    entry_price=self._pending_limit_price,
                    anchor_fvg=self._anchor_fvg,
                    entry_fvg=self._nested_fvg,
                    structural_levels=structural_levels,
                    timestamp=bar.timestamp,
                )
                self._close_anchor("filled", bar.timestamp)
                self.state = State.IN_TRADE
                self._reset_hunt_state()
                self.stats["fills"] += 1
                return signal
            return None

        return None

    def notify_trade_closed(self, won: bool) -> None:
        """Runner/backtest harness calls this once the open trade hits its
        stop or target. Reentry flags are shared with the day strategy
        (cfg.strategy.reentry) -- "keep TP/SL ratio the same as the usual
        strategy" extends naturally to reusing its reentry behavior too."""
        if won and not self.cfg.strategy.reentry.allow_new_setup_after_win:
            self.state = State.DONE_FOR_NIGHT
            return
        if not won and not self.cfg.strategy.reentry.allow_reentry_after_stop:
            self.state = State.DONE_FOR_NIGHT
            return

        self._reset_hunt_state()
        self.state = State.WAIT_ANCHOR
