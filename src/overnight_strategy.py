from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum, auto
from zoneinfo import ZoneInfo

from src.config import BotConfig, SessionConfig
from src.fvg import FairValueGap, FvgDetector
from src.models import Bar, Direction
from src.risk import compute_stop_target, find_structural_stop_price
from src.session_levels import SessionLevels, SessionLevelSet
from src.strategy import AnchorRecord
from src.swing_points import SwingPointTracker


class State(Enum):
    IDLE = auto()          # outside the Asia/London window entirely
    WAIT_FVG = auto()      # in-window; hunting a large, unmitigated pooled 5m/1m FVG in either direction
    WAIT_FILL = auto()     # a qualifying FVG was found; limit order resting at its own retracement point
    IN_TRADE = auto()      # limit order filled, waiting on the runner/broker to close it
    DONE_FOR_NIGHT = auto()


@dataclass
class EntrySignal:
    direction: Direction
    entry_price: float
    anchor_fvg: FairValueGap
    stop_price: float
    timestamp: datetime
    stop_source: str = "swing"  # "fvg" | "swing" -- see risk.StopCandidate
    stop_fvg_size: float | None = None


def _nearest_then_largest(fvgs: list[FairValueGap], current_price: float) -> FairValueGap:
    """Picks whichever gap's midpoint is nearest to current price, breaking
    ties by the larger gap. Same rule the day strategy uses."""
    return max(fvgs, key=lambda f: (-abs((f.gap_low + f.gap_high) / 2 - current_price), f.size))


def _in_overnight_window(t: dtime, cfg: SessionConfig) -> bool:
    """Asia (19:00-23:59) -> gap (00:00-02:00) -> London (02:00-05:00) is
    treated as one continuous window rather than resetting in the gap, so
    a hunt in progress isn't abandoned just because there's no session
    label active at that exact moment."""
    return t >= cfg.asia_start or t <= cfg.london_end


def _night_date(local_dt: datetime, cfg: SessionConfig) -> date:
    """The trading_date this overnight window's marked levels belong to --
    matches SessionLevels.levels_for's own mapping (evening-before for Asia,
    same-morning for London), so both halves of one continuous overnight
    window always resolve to the same date even though the wall-clock date
    itself rolls over at midnight in between."""
    return local_dt.date() + timedelta(days=1) if local_dt.time() >= cfg.asia_start else local_dt.date()


class OvernightMomentumStrategy:
    """Asia/London overnight momentum layer described in STRATEGY.md: the
    exact same FVG-detection-and-entry logic the 9:30 ORB strategy uses
    (src/strategy.py's pooled 5m/1m fvg/entry_fvg detectors, entry at a
    configurable retracement into the FVG's own gap), just without that
    strategy's box/breakout gate -- there's no box to form outside NY
    hours, so whichever pooled FVG qualifies first (in *either* direction)
    sets the trade direction directly. Runs in parallel with, and entirely
    independently of, OpeningRangeStrategy -- a separate instance of each is
    fed the same bars.

    Originally built as a two-stage large-anchor (15m/30m) + nested-entry
    (5m/1m) design (see git history) -- resurrecting an even older design
    this project had already tried and removed once for being a funnel
    bottleneck. A real 30-day backtest of that two-stage version reproduced
    the exact same failure shape (110 anchors, only 8 nested entries, all 4
    fills lost), so it was replaced with this single-stage design at the
    user's direct instruction: reuse the day strategy's own proven
    FVG-finding logic, just without its ORB confluence layer.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.session_levels = SessionLevels(cfg.session)
        self.fvg_detector_5m = FvgDetector(cfg.strategy.fvg, self.tz)
        self.fvg_detector_1m = FvgDetector(cfg.strategy.entry_fvg, self.tz)
        # Break-of-structure stop -- the primary stop rule since 2026-07-08
        # (see risk.py's find_structural_stop_price); a strong 5m FVG on
        # the stop side is only used as a fallback when no swing point
        # qualifies.
        self.swing_tracker = SwingPointTracker()

        self.state = State.IDLE
        self._night_date: date | None = None
        self._direction: Direction | None = None
        self._anchor_fvg: FairValueGap | None = None
        self._anchor_started_at: datetime | None = None
        self._pending_limit_price: float | None = None
        # Anchors rejected for having no real structural stop within
        # budget -- tracked by identity, same rationale as the day
        # strategy's _rejected_anchor_ids (see strategy.py).
        self._rejected_anchor_ids: set[int] = set()
        # Real 30-day evidence (see config.yaml's overnight.max_trades_per_night
        # comment) showed nights with 2+ trades performing far worse than
        # single-trade nights -- capped independently of the day strategy's
        # own reentry.allow_reentry_after_stop.
        self._trades_tonight = 0
        # User's explicit instruction, 2026-07-09: a real overnight session
        # sat in WAIT_FILL for hours while price fell ~52 points past a
        # SHORT anchor's own entry and kept going -- no fresher/nearer FVG
        # ever qualified to supersede it (the decline was a steady grind,
        # not a sharp displacement move), so the strategy just kept
        # "hedging the whole night" on the first anchor it found. Half the
        # $200 stop budget (config.yaml's strategy.max_stop_dollars) was
        # chosen as the abandon-and-rehunt threshold -- half, not the full
        # amount, so a continuation move gets dropped well before it would
        # ever reach the full stop distance a fill at this level would even
        # be given. See the WAIT_FILL handling below.
        self._stale_anchor_distance_points = (cfg.strategy.max_stop_dollars / 2) / (
            cfg.instrument.point_value * cfg.position_sizing.contract_size
        )

        self.stats = {
            "large_fvgs": 0,
            "fills": 0,
            "stale_abandoned": 0,
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

    def status_snapshot(self) -> dict:
        """Plain-dict view of what the strategy is currently doing -- for
        the dashboard's live "bot activity" view only (src/runner.py writes
        this out after every bar); has no effect on trading decisions."""
        return {
            "state": self.state.name,
            "direction": self._direction.value if self._direction else None,
            "anchor_gap_low": self._anchor_fvg.gap_low if self._anchor_fvg else None,
            "anchor_gap_high": self._anchor_fvg.gap_high if self._anchor_fvg else None,
            "pending_limit_price": self._pending_limit_price,
        }

    def _entry_price(self, gap: FairValueGap) -> float:
        """Same retracement rule as the day strategy (strategy.py's
        _entry_price) -- see there for why any fraction strictly between 0
        and 1 stays provably safe from "mitigated before it could fill"."""
        pct = self.cfg.strategy.entry_retracement_pct
        width = gap.gap_high - gap.gap_low
        if gap.direction is Direction.LONG:
            return gap.gap_high - pct * width
        return gap.gap_low + pct * width

    def _candidate_fvgs(self, direction: Direction) -> list[FairValueGap]:
        candidates = self.fvg_detector_5m.unmitigated_in_direction(
            direction
        ) + self.fvg_detector_1m.unmitigated_in_direction(direction)
        return [g for g in candidates if id(g) not in self._rejected_anchor_ids]

    def _all_candidate_fvgs(self) -> list[FairValueGap]:
        """Both directions pooled -- there's no breakout to fix a direction
        ahead of time here, so whichever qualifying FVG appears first (in
        either direction) is the one that sets it."""
        return self._candidate_fvgs(Direction.LONG) + self._candidate_fvgs(Direction.SHORT)

    def _close_anchor(self, outcome: str, ended_at: datetime) -> None:
        if self._anchor_fvg is None:
            return
        self.anchor_history.append(
            AnchorRecord(
                direction=self._direction,
                gap_low=self._anchor_fvg.gap_low,
                gap_high=self._anchor_fvg.gap_high,
                started_at=self._anchor_started_at,
                ended_at=ended_at,
                outcome=outcome,
            )
        )

    def _reset_hunt_state(self) -> None:
        self._direction = None
        self._anchor_fvg = None
        self._anchor_started_at = None
        self._pending_limit_price = None

    def _start_new_night(self, night_date: date, bar_timestamp: datetime) -> None:
        self._night_date = night_date
        self._close_anchor("session_ended", bar_timestamp)
        self.fvg_detector_5m.clear_active_gaps()
        self.fvg_detector_1m.clear_active_gaps()
        self.swing_tracker.reset()
        self._rejected_anchor_ids = set()
        self._trades_tonight = 0
        self._reset_hunt_state()
        self.state = State.WAIT_FVG

    def on_bar(self, bar: Bar) -> EntrySignal | None:
        local = bar.timestamp.astimezone(self.tz)
        t = local.time()
        in_window = _in_overnight_window(t, self.cfg.session)

        self.session_levels.add_bar(bar)
        self.fvg_detector_5m.add_bar(bar)
        self.fvg_detector_1m.add_bar(bar)
        self.swing_tracker.add_bar(bar)

        if self.state is State.IN_TRADE:
            # Nothing to do until the runner/backtest harness calls
            # notify_trade_closed -- deliberately not re-evaluated against
            # in_window/night_date so an overnight trade still open when
            # the window ends isn't reset mid-trade.
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

        if self.state is State.WAIT_FVG:
            # Any large, unmitigated 5m or 1m FVG -- in *either* direction,
            # since there's no breakout to fix one ahead of time -- both
            # sets direction and anchors the move, same as the day
            # strategy's WAIT_5M_FVG once its own breakout has set
            # direction.
            candidates = self._all_candidate_fvgs()
            if candidates:
                self._anchor_fvg = _nearest_then_largest(candidates, bar.close)
                self._direction = self._anchor_fvg.direction
                self._anchor_started_at = bar.timestamp
                self._pending_limit_price = self._entry_price(self._anchor_fvg)
                self.stats["large_fvgs"] += 1
                self.state = State.WAIT_FILL
            return None

        if self.state is State.WAIT_FILL:
            # Keep the anchor current: a nearer/fresher unmitigated FVG in
            # the same direction supersedes it, exactly like the day
            # strategy's WAIT_FILL. No separate "mitigated before fill"
            # check is needed -- the resting limit sits strictly between
            # the gap's own edges (see _entry_price), so it always fills
            # before price could reach far enough to mitigate it.
            candidates = self._candidate_fvgs(self._direction)
            if candidates:
                best = _nearest_then_largest(candidates, bar.close)
                if best is not self._anchor_fvg:
                    self._close_anchor("superseded", bar.timestamp)
                    self._anchor_fvg = best
                    self._anchor_started_at = bar.timestamp
                    self._pending_limit_price = self._entry_price(best)
                    self.stats["large_fvgs"] += 1

            # User's explicit instruction, 2026-07-09: don't "hedge the
            # whole night" on the first anchor found -- if price keeps
            # moving away from the entry (the continuation move the
            # anchor bet on, playing out too far without ever retracing)
            # by more than half the stop budget, drop it and go back to
            # plain hunting rather than sitting on a now-stale level
            # indefinitely. Checked after the supersede block above, so a
            # fresh (near-zero-distance) anchor just picked this same bar
            # never spuriously trips this.
            distance_away = (
                bar.close - self._pending_limit_price
                if self._direction is Direction.LONG
                else self._pending_limit_price - bar.close
            )
            if distance_away > self._stale_anchor_distance_points:
                # Confirmed live 2026-07-09: without excluding it here, the
                # same gap is still the nearest unmitigated candidate in
                # the pool, so WAIT_FVG immediately re-picked this exact
                # anchor next bar, which immediately re-tripped this same
                # check -- an infinite pick/abandon loop on one gap that
                # defeated the entire point of this feature (real bot.log
                # showed the identical gap marked "stale" ~15 times in a
                # row instead of ever moving on to a fresh one). Same
                # exclusion mechanism as the no_valid_stop rejection path
                # below: tracked by identity so it stays excluded until
                # mitigated or the night rolls over, not just until the
                # next bar.
                self._close_anchor("stale", bar.timestamp)
                self._rejected_anchor_ids.add(id(self._anchor_fvg))
                self._reset_hunt_state()
                self.stats["stale_abandoned"] += 1
                self.state = State.WAIT_FVG
                return None

            filled = (
                bar.low <= self._pending_limit_price
                if self._direction is Direction.LONG
                else bar.high >= self._pending_limit_price
            )
            if filled:
                # Stop is the most recent 1m break-of-structure swing
                # point on the stop side of entry, or (if none qualifies)
                # the nearest strong 5m FVG's outer edge on that same side
                # -- see risk.py's find_structural_stop_price for the full
                # rule. swing-first (prefer_swing=True) specifically for
                # this (overnight) strategy: a real 32-trade backtest
                # showed swing-based stops winning 50% (+$55/trade) versus
                # FVG-based stops winning only 35% (+$12/trade) here --
                # the opposite holds for the day strategy, see strategy.py.
                stop_candidate = find_structural_stop_price(
                    direction=self._direction,
                    entry_price=self._pending_limit_price,
                    fvg_candidates=self.fvg_detector_5m.unmitigated_in_direction(self._direction),
                    swing_high=self.swing_tracker.most_recent_swing_high,
                    swing_low=self.swing_tracker.most_recent_swing_low,
                    prefer_swing=True,
                )
                bracket = compute_stop_target(
                    direction=self._direction,
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
                    self._reset_hunt_state()
                    self.state = State.WAIT_FVG
                    return None

                signal = EntrySignal(
                    direction=self._direction,
                    entry_price=self._pending_limit_price,
                    anchor_fvg=self._anchor_fvg,
                    stop_price=bracket.stop_price,
                    timestamp=bar.timestamp,
                    stop_source=stop_candidate.source,
                    stop_fvg_size=stop_candidate.fvg_size,
                )
                self._close_anchor("filled", bar.timestamp)
                self.state = State.IN_TRADE
                self._reset_hunt_state()
                self._trades_tonight += 1
                self.stats["fills"] += 1
                return signal
            return None

        return None

    def notify_trade_closed(self, won: bool) -> None:
        """Runner/backtest harness calls this once the open trade hits its
        stop or target. Reentry flags are shared with the day strategy
        (cfg.strategy.reentry), but max_trades_per_night is this strategy's
        own, separate cap (see config.yaml's comment) -- checked regardless
        of allow_reentry_after_stop, since real data showed nights with 2+
        trades performing far worse than single-trade nights."""
        if won and not self.cfg.strategy.reentry.allow_new_setup_after_win:
            self.state = State.DONE_FOR_NIGHT
            return
        if not won and not self.cfg.strategy.reentry.allow_reentry_after_stop:
            self.state = State.DONE_FOR_NIGHT
            return
        if self._trades_tonight >= self.cfg.strategy.overnight.max_trades_per_night:
            self.state = State.DONE_FOR_NIGHT
            return

        self._reset_hunt_state()
        self.state = State.WAIT_FVG

    def notify_entry_not_filled(self) -> None:
        """Live trading only -- mirrors OpeningRangeStrategy's method of the
        same name (see its docstring for the full reasoning). on_bar
        already moves to IN_TRADE and clears the hunt state
        (_reset_hunt_state) the instant it returns a signal, since backtest
        treats a signal as a guaranteed fill; live, the broker's resting
        limit order can still fail to actually fill or the order placement
        itself can be rejected. Runner calls this in that case.

        Confirmed live 2026-07-08: this method didn't exist at all before
        -- a real /Order/place rejection left Runner._enter_trade's
        not-filled path calling it, raising AttributeError and leaving
        self.state stuck at IN_TRADE forever, with no trade ever recorded
        and no further hunting for the rest of the process's life. Hunt-
        state fields are already None by this point (_reset_hunt_state
        already ran when the signal was generated), so there's nothing
        left to reset except the state itself."""
        self.state = State.WAIT_FVG
