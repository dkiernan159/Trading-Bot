from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.models import Bar
from src.swing_points import SwingPointTracker

TZ = ZoneInfo("America/New_York")
START = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bar_at(k: int, high: float, low: float) -> Bar:
    ts = START + timedelta(minutes=k)
    return Bar(timestamp=ts, open=(high + low) / 2, high=high, low=low, close=(high + low) / 2)


def feed(tracker: SwingPointTracker, highs_lows: list[tuple[float, float]]) -> None:
    for k, (h, l) in enumerate(highs_lows):
        tracker.add_bar(bar_at(k, h, l))


def test_no_swing_point_before_enough_bars_arrive():
    tracker = SwingPointTracker()
    feed(tracker, [(101, 99), (102, 100), (103, 101)])  # only 3 bars, need 2*2+1=5
    assert tracker.most_recent_swing_high is None
    assert tracker.most_recent_swing_low is None


def test_detects_a_confirmed_swing_high():
    """Bar index 2's high (105) is strictly greater than both bars before
    and both bars after it -- a real local peak."""
    tracker = SwingPointTracker()
    highs_lows = [
        (101, 99),
        (103, 100),
        (105, 101),  # candidate peak
        (102, 98),
        (100, 97),
    ]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_high == 105
    assert tracker.most_recent_swing_low is None


def test_detects_a_confirmed_swing_low():
    tracker = SwingPointTracker()
    highs_lows = [
        (103, 99),
        (102, 97),
        (101, 95),  # candidate trough
        (102, 96),
        (103, 98),
    ]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_low == 95
    assert tracker.most_recent_swing_high is None


def test_a_tie_does_not_count_as_a_swing_point():
    """Strict inequality only -- a bar tied with a neighbor isn't a real
    local extreme."""
    tracker = SwingPointTracker()
    highs_lows = [
        (101, 99),
        (103, 100),
        (103, 101),  # tied with bar at index 1 -- not a strict peak
        (102, 98),
        (100, 97),
    ]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_high is None


def test_most_recent_swing_point_overwrites_an_older_one():
    tracker = SwingPointTracker()
    # First swing high at index 2 (105), then a second, later one at index 6 (110).
    highs_lows = [
        (101, 99),
        (103, 100),
        (105, 101),
        (102, 98),
        (100, 97),
        (108, 96),
        (110, 95),  # candidate peak (confirmed once bars 7, 8 arrive)
        (109, 94),
        (107, 93),
    ]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_high == 110


def test_reset_clears_history_and_confirmed_points():
    tracker = SwingPointTracker()
    highs_lows = [(101, 99), (103, 100), (105, 101), (102, 98), (100, 97)]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_high == 105

    tracker.reset()

    assert tracker.most_recent_swing_high is None
    assert tracker.most_recent_swing_low is None
    assert tracker.most_recent_swing_high_at is None
    assert tracker.most_recent_swing_low_at is None
    # Feeding fewer than 2*PIVOT_WIDTH+1 bars post-reset must not resurrect
    # anything from before the reset.
    feed(tracker, [(101, 99), (102, 100)])
    assert tracker.most_recent_swing_high is None


# ---------- formation-time tracking ----------
# (added 2026-08-20: the breakeven-hold-if-structure-supports-it feature
# needs to tell a swing point that formed *during* a given trade apart from
# one that already existed before entry -- see runner.py's
# _maybe_move_stop_to_breakeven)


def test_swing_high_records_the_confirming_bars_own_timestamp():
    """The candidate bar's own timestamp (index 2, k=2), not "now"/the
    timestamp of whichever later bar happened to confirm it -- a pivot
    lags PIVOT_WIDTH bars behind confirmation, and it's the pivot's own
    formation time that matters for "did this form after entry", not when
    the tracker happened to notice it."""
    tracker = SwingPointTracker()
    highs_lows = [(101, 99), (103, 100), (105, 101), (102, 98), (100, 97)]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_high_at == START + timedelta(minutes=2)


def test_swing_low_records_the_confirming_bars_own_timestamp():
    tracker = SwingPointTracker()
    highs_lows = [(103, 99), (102, 97), (101, 95), (102, 96), (103, 98)]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_low_at == START + timedelta(minutes=2)


def test_swing_high_timestamp_updates_alongside_a_newer_swing_high():
    tracker = SwingPointTracker()
    highs_lows = [
        (101, 99),
        (103, 100),
        (105, 101),  # swing high at k=2
        (102, 98),
        (100, 97),
        (108, 96),
        (110, 95),  # newer swing high at k=6
        (109, 94),
        (107, 93),
    ]
    feed(tracker, highs_lows)
    assert tracker.most_recent_swing_high == 110
    assert tracker.most_recent_swing_high_at == START + timedelta(minutes=6)
