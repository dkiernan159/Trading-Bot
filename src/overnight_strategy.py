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
    structural_levels: list[float]
    timestamp: datetime


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

        self.stats = {
            "large_fvgs": 0,
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

            filled = (
                bar.low <= self._pending_limit_price
                if self._direction is Direction.LONG
                else bar.high >= self._pending_limit_price
            )
            if filled:
                levels = self.current_session_levels
                structural_levels = list(levels.all_levels()) if levels else []

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
                    self._close_anchor("no_valid_stop", bar.timestamp)
                    self._rejected_anchor_ids.add(id(self._anchor_fvg))
                    self._reset_hunt_state()
                    self.state = State.WAIT_FVG
                    return None

                signal = EntrySignal(
                    direction=self._direction,
                    entry_price=self._pending_limit_price,
                    anchor_fvg=self._anchor_fvg,
                    structural_levels=structural_levels,
                    timestamp=bar.timestamp,
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
