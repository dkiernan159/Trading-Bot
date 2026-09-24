from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import ZoneConfig
from src.models import Bar, Direction

# Same fractal-pivot rule as swing_points.py's SwingPointTracker (a bar's
# high/low strictly beats both PIVOT_WIDTH neighbors on either side) --
# just run on ZoneTracker's own coarser (15m by default) candles instead
# of raw 1-minute bars, since the whole point of this detector is to see
# structure the fast timeframe can't.
PIVOT_WIDTH = 2


@dataclass
class Zone:
    """A higher-timeframe support or resistance level: 2+ (cfg.min_touches)
    independent swing-point touches clustering near the same price within
    cfg.lookback_days. direction is which side of price the zone defends
    -- LONG means a support zone (built from swing lows, defends against
    downside, opposes a SHORT trade); SHORT means a resistance zone (built
    from swing highs, opposes a LONG trade)."""

    direction: Direction
    touches: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def price(self) -> float:
        """Representative level -- the running average of every touch, so
        the zone can drift slightly as fresh touches accumulate instead of
        being pinned forever to wherever its first touch happened to be."""
        return sum(p for _, p in self.touches) / len(self.touches)

    @property
    def touch_count(self) -> int:
        return len(self.touches)


class ZoneTracker:
    """Detects higher-timeframe support/resistance zones and tells a
    strategy when a candidate entry would fight one.

    Added 2026-09-24 at the user's explicit request, after a real trade
    shorted directly into a support zone that had already bounced twice
    in the prior 3 days and lost (see STRATEGY.md's real 1-minute-bar
    evidence: 2026-09-21 through 09-23, five independent touches of the
    same ~30615-30690 floor, the fifth one bouncing right into the losing
    short's own entry) -- "we need to factor this in to future trades...
    sometimes a support zone is created days prior and only retested
    once." Deliberately built on a *coarser* timeframe
    (cfg.timeframe_minutes, 15m by default) than the FVG detectors (5m)
    or the swing tracker (1m) -- the whole point is to see structure
    those faster timeframes can't, matching the 15m chart the user was
    actually reading when they spotted it.

    Unlike SwingPointTracker/FvgDetector, this is deliberately NOT reset
    at session/day boundaries -- a multi-day zone is the entire point, so
    memory persists across days and only ages out via cfg.lookback_days
    (see _prune_old). reset() exists for tests/manual use only; nothing
    in the live strategies calls it on a day/night rollover.
    """

    def __init__(self, cfg: ZoneConfig, tz: ZoneInfo):
        self.cfg = cfg
        self.tz = tz
        self._bucket_start: datetime | None = None
        self._bucket_bars: list[Bar] = []
        self._candles: list[Bar] = []
        self.support_zones: list[Zone] = []
        self.resistance_zones: list[Zone] = []

    def add_bar(self, bar: Bar) -> None:
        local = bar.timestamp.astimezone(self.tz)
        step = self.cfg.timeframe_minutes
        bucket_start = local.replace(minute=(local.minute // step) * step, second=0, microsecond=0)

        if self._bucket_start is None:
            self._bucket_start = bucket_start
            self._bucket_bars.append(bar)
            return

        if bucket_start != self._bucket_start:
            self._candles.append(self._finalize_bucket())
            self._prune_old(bar.timestamp)
            self._check_pivot()
            self._bucket_bars = []
            self._bucket_start = bucket_start

        self._bucket_bars.append(bar)

    def opposing_zone(self, direction: Direction, price: float) -> Zone | None:
        """The nearest significant (>= cfg.min_touches) zone that opposes
        `direction` -- a resistance zone above a LONG, or a support zone
        below a SHORT -- within cfg.tolerance_points of `price`. None if
        no such zone qualifies. This is the query a strategy's WAIT_FILL
        calls on every bar to decide whether to pause into
        WAIT_ZONE_CONFIRMATION instead of taking a plain retracement fill."""
        zones = self.resistance_zones if direction is Direction.LONG else self.support_zones
        candidates = [
            z
            for z in zones
            if z.touch_count >= self.cfg.min_touches and abs(z.price - price) <= self.cfg.tolerance_points
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda z: abs(z.price - price))

    def _finalize_bucket(self) -> Bar:
        group = self._bucket_bars
        return Bar(
            timestamp=self._bucket_start,
            open=group[0].open,
            high=max(g.high for g in group),
            low=min(g.low for g in group),
            close=group[-1].close,
        )

    def _check_pivot(self) -> None:
        w = PIVOT_WIDTH
        if len(self._candles) < 2 * w + 1:
            return
        window = self._candles[-(2 * w + 1) :]
        candidate = window[w]
        others = window[:w] + window[w + 1 :]
        if all(candidate.high > o.high for o in others):
            self._add_touch(self.resistance_zones, Direction.SHORT, candidate.timestamp, candidate.high)
        if all(candidate.low < o.low for o in others):
            self._add_touch(self.support_zones, Direction.LONG, candidate.timestamp, candidate.low)

    def _add_touch(self, zones: list[Zone], direction: Direction, ts: datetime, price: float) -> None:
        for zone in zones:
            if abs(zone.price - price) <= self.cfg.tolerance_points:
                zone.touches.append((ts, price))
                return
        zones.append(Zone(direction=direction, touches=[(ts, price)]))

    def _prune_old(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.cfg.lookback_days)
        for zone in self.support_zones:
            zone.touches = [(ts, p) for ts, p in zone.touches if ts >= cutoff]
        for zone in self.resistance_zones:
            zone.touches = [(ts, p) for ts, p in zone.touches if ts >= cutoff]
        self.support_zones = [z for z in self.support_zones if z.touches]
        self.resistance_zones = [z for z in self.resistance_zones if z.touches]
        max_candles = self.cfg.lookback_days * 24 * (60 // self.cfg.timeframe_minutes) + 10
        if len(self._candles) > max_candles:
            self._candles = self._candles[-max_candles:]

    def reset(self) -> None:
        """Test/manual use only -- see the class docstring for why this is
        never called on a live day/night rollover."""
        self._bucket_start = None
        self._bucket_bars = []
        self._candles = []
        self.support_zones = []
        self.resistance_zones = []
