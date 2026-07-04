from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import FvgConfig
from src.models import Bar, Direction


@dataclass
class FairValueGap:
    """A 3-candle fair value gap on the strategy's FVG timeframe (15m by
    default, see FvgConfig.timeframe_minutes).

    direction is the trade direction this FVG supports: a bullish gap (price
    displaced upward, leaving untraded space below) supports LONG entries; a
    bearish gap supports SHORT entries.
    """

    direction: Direction
    gap_low: float
    gap_high: float
    formed_at: datetime
    timeframe_minutes: int

    @property
    def size(self) -> float:
        return self.gap_high - self.gap_low


class FvgDetector:
    """Detects 'strong' fair value gaps on `cfg.timeframe_minutes` candles
    (built internally from whatever bars it's fed -- normally 1m bars) and
    keeps a running pool of the ones still unmitigated.

    A gap counts as strong when both hold:
      - size >= cfg.min_gap_points
      - the middle (displacement) candle's body >= cfg.displacement_multiplier
        times the average candle range over the preceding cfg.lookback_bars
    See STRATEGY.md for why these thresholds are assumptions to tune.

    A gap is mitigated the moment any subsequent bar trades through its far
    edge (below gap_low for a LONG gap, above gap_high for a SHORT gap) --
    checked on every incoming bar for precision, not just at each candle
    close. Mitigated gaps are dropped from the pool immediately and are
    never offered as entry candidates again.
    """

    def __init__(self, cfg: FvgConfig, tz: ZoneInfo):
        self.cfg = cfg
        self.tz = tz
        self._candles: list[Bar] = []
        self._active: list[FairValueGap] = []
        self._bucket_start: datetime | None = None
        self._bucket_bars: list[Bar] = []

    def add_bar(self, bar: Bar) -> FairValueGap | None:
        """Feed one incoming bar. Returns the FVG detected this call, if
        any (also added to the active pool) -- kept mainly for tests; the
        pool (see `unmitigated_in_direction`) is what the strategy uses."""
        self._prune_mitigated(bar)
        new_gap = self._feed(bar)
        if new_gap is not None:
            self._active.append(new_gap)
        return new_gap

    def unmitigated_in_direction(self, direction: Direction) -> list[FairValueGap]:
        """Currently-active gaps in the given direction, oldest first --
        `[-1]` is the most recently formed one still untouched."""
        return [g for g in self._active if g.direction is direction]

    def clear_active_gaps(self) -> None:
        """Drops every gap from the active pool without touching the
        candle history used for the average-range baseline. Called at the
        start of each new trading day so a gap from a previous day (never
        mitigated because price simply never traded back through it)
        can't linger indefinitely and get selected as today's anchor or
        entry -- "the session" in STRATEGY.md means today's session, not
        an unbounded lookback across every day the detector has ever
        seen. The candle history is kept so `_recent_average_range` still
        has real (pre-market/overnight) data to work with right from
        9:30, instead of needing to rebuild it from scratch each day."""
        self._active = []

    def _prune_mitigated(self, bar: Bar) -> None:
        self._active = [g for g in self._active if not self._is_mitigated(g, bar)]

    @staticmethod
    def _is_mitigated(gap: FairValueGap, bar: Bar) -> bool:
        if gap.direction is Direction.LONG:
            return bar.low < gap.gap_low
        return bar.high > gap.gap_high

    def _feed(self, bar: Bar) -> FairValueGap | None:
        local = bar.timestamp.astimezone(self.tz)
        step = self.cfg.timeframe_minutes
        bucket_start = local.replace(minute=(local.minute // step) * step, second=0, microsecond=0)

        if self._bucket_start is None:
            self._bucket_start = bucket_start
            self._bucket_bars.append(bar)
            return None

        new_gap = None
        if bucket_start != self._bucket_start:
            self._candles.append(self._finalize_bucket())
            max_len = self.cfg.lookback_bars + 10
            if len(self._candles) > max_len:
                self._candles = self._candles[-max_len:]
            new_gap = self._check_pattern()
            self._bucket_bars = []
            self._bucket_start = bucket_start

        self._bucket_bars.append(bar)
        return new_gap

    def _finalize_bucket(self) -> Bar:
        group = self._bucket_bars
        return Bar(
            timestamp=self._bucket_start,
            open=group[0].open,
            high=max(g.high for g in group),
            low=min(g.low for g in group),
            close=group[-1].close,
        )

    def _check_pattern(self) -> FairValueGap | None:
        if len(self._candles) < 3:
            return None

        c0, c1, c2 = self._candles[-3], self._candles[-2], self._candles[-1]

        avg_range = self._recent_average_range()
        if avg_range is None or avg_range == 0:
            return None

        displacement_body = abs(c1.close - c1.open)
        is_strong = displacement_body >= self.cfg.displacement_multiplier * avg_range

        if c0.high < c2.low and c1.close > c1.open:
            gap_size = c2.low - c0.high
            if gap_size >= self.cfg.min_gap_points and is_strong:
                return FairValueGap(
                    direction=Direction.LONG,
                    gap_low=c0.high,
                    gap_high=c2.low,
                    formed_at=c2.timestamp,
                    timeframe_minutes=self.cfg.timeframe_minutes,
                )

        if c0.low > c2.high and c1.close < c1.open:
            gap_size = c0.low - c2.high
            if gap_size >= self.cfg.min_gap_points and is_strong:
                return FairValueGap(
                    direction=Direction.SHORT,
                    gap_low=c2.high,
                    gap_high=c0.low,
                    formed_at=c2.timestamp,
                    timeframe_minutes=self.cfg.timeframe_minutes,
                )

        return None

    def _recent_average_range(self) -> float | None:
        history = self._candles[:-3]
        history = history[-self.cfg.lookback_bars :]
        if not history:
            return None
        return sum(b.high - b.low for b in history) / len(history)
