from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.config import FvgConfig
from src.models import Bar, Direction


@dataclass
class FairValueGap:
    """A 3-candle fair value gap on the 1-minute chart.

    direction is the trade direction this FVG supports: a bullish gap (price
    displaced upward, leaving untraded space below) supports LONG entries; a
    bearish gap supports SHORT entries.
    """

    direction: Direction
    gap_low: float
    gap_high: float
    formed_at: datetime

    @property
    def size(self) -> float:
        return self.gap_high - self.gap_low


class FvgDetector:
    """Detects 'strong' 1-minute fair value gaps.

    A gap counts as strong when both hold:
      - size >= cfg.min_gap_points
      - the middle (displacement) candle's body >= cfg.displacement_multiplier
        times the average candle range over the preceding cfg.lookback_bars
    See STRATEGY.md for why these thresholds are assumptions to tune.
    """

    def __init__(self, cfg: FvgConfig):
        self.cfg = cfg
        self._bars: list[Bar] = []

    def add_bar(self, bar: Bar) -> FairValueGap | None:
        self._bars.append(bar)
        max_len = self.cfg.lookback_bars + 10
        if len(self._bars) > max_len:
            self._bars = self._bars[-max_len:]

        if len(self._bars) < 3:
            return None

        c0, c1, c2 = self._bars[-3], self._bars[-2], self._bars[-1]

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
                )

        if c0.low > c2.high and c1.close < c1.open:
            gap_size = c0.low - c2.high
            if gap_size >= self.cfg.min_gap_points and is_strong:
                return FairValueGap(
                    direction=Direction.SHORT,
                    gap_low=c2.high,
                    gap_high=c0.low,
                    formed_at=c2.timestamp,
                )

        return None

    def _recent_average_range(self) -> float | None:
        history = self._bars[:-3]
        history = history[-self.cfg.lookback_bars :]
        if not history:
            return None
        return sum(b.high - b.low for b in history) / len(history)
