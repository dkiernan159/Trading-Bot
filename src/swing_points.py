from __future__ import annotations

from datetime import datetime

from src.models import Bar


class SwingPointTracker:
    """Tracks the most recent confirmed 1-minute break-of-structure swing
    high/low -- the stop-placement fallback (see risk.py's
    find_structural_stop_price) used when no qualifying 5m FVG sits on the
    stop side of an entry.

    A bar is a swing high/low using a standard N-bar fractal pivot: its
    high (low) is strictly greater (less) than every one of the
    PIVOT_WIDTH bars immediately before and after it. ASSUMPTION:
    PIVOT_WIDTH=2 (a 5-bar pivot) is a common, simple default for this
    kind of detector -- watch real backtests/live behavior and tune if it
    turns out too tight (whipsawed by noise) or too wide (misses genuine
    structure).

    Confirmation necessarily lags by PIVOT_WIDTH bars, since a pivot can't
    be confirmed until the bars after it are known -- fed 1-minute bars
    directly (no internal bucketing, unlike FvgDetector, since "1-minute"
    already is the base timeframe here).
    """

    PIVOT_WIDTH = 2

    def __init__(self) -> None:
        self._bars: list[Bar] = []
        self.most_recent_swing_high: float | None = None
        self.most_recent_swing_low: float | None = None
        # Added 2026-08-20 (see runner.py's _maybe_move_stop_to_breakeven):
        # the breakeven-hold-if-structure-supports-it feature needs to tell
        # a swing point that formed *during* a given trade (real, fresh
        # structure) apart from one that already existed before entry
        # (stale, not evidence of anything new) -- confirmed the candidate
        # bar itself (not "now") since a pivot lags PIVOT_WIDTH bars behind
        # confirmation, and the pivot's own formation time is what actually
        # matters here, not when the tracker happened to notice it.
        self.most_recent_swing_high_at: datetime | None = None
        self.most_recent_swing_low_at: datetime | None = None

    def add_bar(self, bar: Bar) -> None:
        self._bars.append(bar)
        w = self.PIVOT_WIDTH
        # Only ever need the last (2w + 1) bars to confirm a pivot -- trim
        # aggressively (a little slack added) so this can't grow unbounded
        # over a long session.
        max_len = 2 * w + 1 + 50
        if len(self._bars) > max_len:
            self._bars = self._bars[-max_len:]

        if len(self._bars) < 2 * w + 1:
            return

        window = self._bars[-(2 * w + 1) :]
        candidate = window[w]
        others = window[:w] + window[w + 1 :]
        if all(candidate.high > o.high for o in others):
            self.most_recent_swing_high = candidate.high
            self.most_recent_swing_high_at = candidate.timestamp
        if all(candidate.low < o.low for o in others):
            self.most_recent_swing_low = candidate.low
            self.most_recent_swing_low_at = candidate.timestamp

    def reset(self) -> None:
        """Called at day/night session boundaries, same as the FVG
        detectors' clear_active_gaps -- a swing point from a previous
        session shouldn't linger indefinitely as a stop reference."""
        self._bars = []
        self.most_recent_swing_high = None
        self.most_recent_swing_low = None
        self.most_recent_swing_high_at = None
        self.most_recent_swing_low_at = None
