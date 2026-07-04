from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from src.config import SessionConfig
from src.models import Bar

Zone = tuple[float, float]  # (low, high) of the 15m candle that set an extreme


@dataclass
class SessionLevelSet:
    """High/low reference levels marked out ahead of the NY session.

    Each level also has a "zone" -- the low/high range of the 15-minute
    candle that actually set that extreme, rather than a single exact tick.
    An FVG only needs to overlap this zone to count as "at" the level, not
    contain the precise price -- see strategy.py: _fvg_contains_key_level.
    """

    previous_day_high: float | None
    previous_day_low: float | None
    asia_high: float | None
    asia_low: float | None
    london_high: float | None
    london_low: float | None
    previous_day_high_zone: Zone | None = None
    previous_day_low_zone: Zone | None = None
    asia_high_zone: Zone | None = None
    asia_low_zone: Zone | None = None
    london_high_zone: Zone | None = None
    london_low_zone: Zone | None = None

    def all_levels(self) -> list[float]:
        """Exact extreme prices -- used for stop-loss placement (risk.py),
        which still wants the nearest single structural price, not a zone."""
        return [
            v
            for v in (
                self.previous_day_high,
                self.previous_day_low,
                self.asia_high,
                self.asia_low,
                self.london_high,
                self.london_low,
            )
            if v is not None
        ]


def _aggregate_to_15m(bars: list[Bar], tz: ZoneInfo) -> list[Bar]:
    """Aggregates 1-minute bars into 15-minute candles on wall-clock
    :00/:15/:30/:45 boundaries."""
    buckets: dict[tuple, list[Bar]] = {}
    for b in bars:
        local = b.timestamp.astimezone(tz)
        bucket_start = local.replace(minute=(local.minute // 15) * 15, second=0, microsecond=0)
        buckets.setdefault(bucket_start, []).append(b)

    candles = []
    for start in sorted(buckets):
        group = buckets[start]
        candles.append(
            Bar(
                timestamp=start,
                open=group[0].open,
                high=max(g.high for g in group),
                low=min(g.low for g in group),
                close=group[-1].close,
            )
        )
    return candles


def _zone_at_high(candles_15m: list[Bar]) -> Zone | None:
    if not candles_15m:
        return None
    extreme = max(candles_15m, key=lambda c: c.high)
    return (extreme.low, extreme.high)


def _zone_at_low(candles_15m: list[Bar]) -> Zone | None:
    if not candles_15m:
        return None
    extreme = min(candles_15m, key=lambda c: c.low)
    return (extreme.low, extreme.high)


class SessionLevels:
    """Tracks previous-day, Asia-session, and London-session high/low.

    Asia session is assumed to fall on the evening *before* the trading date
    (ET); London session is assumed to fall in the early morning *of* the
    trading date, ahead of the 9:30 NY open. See STRATEGY.md for why these
    windows are configurable assumptions.
    """

    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._bars: list[Bar] = []

    def add_bar(self, bar: Bar) -> None:
        self._bars.append(bar)

    def levels_for(self, trading_date: date) -> SessionLevelSet:
        prev_date = trading_date - timedelta(days=1)

        prev_day_bars = [
            b for b in self._bars if b.timestamp.astimezone(self.tz).date() == prev_date
        ]
        asia_bars = [
            b
            for b in prev_day_bars
            if self.cfg.asia_start <= b.timestamp.astimezone(self.tz).time() <= self.cfg.asia_end
        ]
        london_bars = [
            b
            for b in self._bars
            if b.timestamp.astimezone(self.tz).date() == trading_date
            and self.cfg.london_start <= b.timestamp.astimezone(self.tz).time() <= self.cfg.london_end
        ]

        prev_day_15m = _aggregate_to_15m(prev_day_bars, self.tz)
        asia_15m = _aggregate_to_15m(asia_bars, self.tz)
        london_15m = _aggregate_to_15m(london_bars, self.tz)

        return SessionLevelSet(
            previous_day_high=max((b.high for b in prev_day_bars), default=None),
            previous_day_low=min((b.low for b in prev_day_bars), default=None),
            asia_high=max((b.high for b in asia_bars), default=None),
            asia_low=min((b.low for b in asia_bars), default=None),
            london_high=max((b.high for b in london_bars), default=None),
            london_low=min((b.low for b in london_bars), default=None),
            previous_day_high_zone=_zone_at_high(prev_day_15m),
            previous_day_low_zone=_zone_at_low(prev_day_15m),
            asia_high_zone=_zone_at_high(asia_15m),
            asia_low_zone=_zone_at_low(asia_15m),
            london_high_zone=_zone_at_high(london_15m),
            london_low_zone=_zone_at_low(london_15m),
        )
