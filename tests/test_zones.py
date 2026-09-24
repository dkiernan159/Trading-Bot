from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import ZoneConfig
from src.models import Bar, Direction
from src.zones import ZoneTracker

TZ = ZoneInfo("America/New_York")
START = datetime(2026, 9, 21, 9, 30, tzinfo=TZ)


def cfg(**overrides) -> ZoneConfig:
    base = dict(
        enabled=True,
        timeframe_minutes=15,
        lookback_days=7,
        tolerance_points=30,
        min_touches=2,
        confirmation_minutes=15,
    )
    base.update(overrides)
    return ZoneConfig(**base)


def candle_at(k: int, high: float, low: float, start: datetime = START) -> Bar:
    """One bar per 15-minute-aligned bucket -- feeding exactly one bar per
    bucket means that bar's own high/low become the finalized candle's
    high/low directly (see ZoneTracker._finalize_bucket)."""
    ts = start + timedelta(minutes=15 * k)
    return Bar(timestamp=ts, open=(high + low) / 2, high=high, low=low, close=(high + low) / 2)


def feed(tracker: ZoneTracker, highs_lows: list[tuple[float, float]], start: datetime = START) -> None:
    for k, (h, l) in enumerate(highs_lows):
        tracker.add_bar(candle_at(k, h, l, start))
    # A candle only finalizes once the *next* bucket's bar arrives (see
    # ZoneTracker.add_bar) -- flush the last one with one extra bar past
    # the window, same pattern as test_strategy.py's feed_large_5m_fvg.
    last_h, last_l = highs_lows[-1]
    tracker.add_bar(candle_at(len(highs_lows), last_h, last_l, start))


def test_no_zone_before_enough_candles():
    tracker = ZoneTracker(cfg(), TZ)
    feed(tracker, [(101, 99), (102, 100), (103, 101)])  # only 3 candles, need 2*2+1=5
    assert tracker.opposing_zone(Direction.SHORT, 100.0) is None
    assert tracker.opposing_zone(Direction.LONG, 100.0) is None


def test_single_touch_is_not_yet_significant():
    """A lone swing low forms a candidate zone with 1 touch -- below
    cfg.min_touches (2), so it must not be offered as a real zone yet."""
    tracker = ZoneTracker(cfg(min_touches=2), TZ)
    feed(tracker, [(101, 99), (103, 100), (105, 95), (102, 98), (100, 97)])  # swing low at 95
    assert len(tracker.support_zones) == 1
    assert tracker.support_zones[0].touch_count == 1
    assert tracker.opposing_zone(Direction.SHORT, 95.0) is None


def test_second_nearby_touch_makes_the_zone_significant():
    """A second swing low within tolerance_points of the first joins the
    same zone -- now with 2 touches it qualifies as a real, opposing
    zone for a SHORT."""
    tracker = ZoneTracker(cfg(min_touches=2, tolerance_points=10), TZ)
    highs_lows = [
        (101, 99), (103, 100), (105, 95), (102, 98), (100, 97),  # swing low #1 at 95 (k=2)
        (103, 99), (106, 101), (108, 98), (104, 100), (102, 103),  # swing low #2 at 98 (k=7)
    ]
    feed(tracker, highs_lows)
    assert len(tracker.support_zones) == 1
    zone = tracker.support_zones[0]
    assert zone.touch_count == 2
    assert zone.price == (95.0 + 98.0) / 2

    found = tracker.opposing_zone(Direction.SHORT, 96.5)
    assert found is zone


def test_a_touch_outside_tolerance_starts_a_separate_zone():
    tracker = ZoneTracker(cfg(min_touches=1, tolerance_points=5), TZ)
    highs_lows = [
        (101, 99), (103, 100), (105, 95), (102, 98), (100, 97),  # swing low at 95
        (150, 149), (153, 150), (155, 130), (152, 148), (150, 147),  # swing low at 130 -- far away
    ]
    feed(tracker, highs_lows)
    assert len(tracker.support_zones) == 2
    prices = sorted(z.price for z in tracker.support_zones)
    assert prices == [95.0, 130.0]


def test_opposing_zone_only_returns_resistance_for_a_long_and_support_for_a_short():
    tracker = ZoneTracker(cfg(min_touches=1, tolerance_points=30), TZ)
    # A swing high (resistance) and a swing low (support) both near 100.
    highs_lows = [
        (101, 99), (103, 97), (110, 90), (103, 97), (101, 99),
    ]
    feed(tracker, highs_lows)
    assert len(tracker.resistance_zones) == 1
    assert len(tracker.support_zones) == 1

    assert tracker.opposing_zone(Direction.LONG, 100.0) is tracker.resistance_zones[0]
    assert tracker.opposing_zone(Direction.SHORT, 100.0) is tracker.support_zones[0]


def test_opposing_zone_respects_proximity():
    tracker = ZoneTracker(cfg(min_touches=1, tolerance_points=10), TZ)
    feed(tracker, [(101, 99), (103, 100), (105, 95), (102, 98), (100, 97)])  # swing low at 95
    assert tracker.opposing_zone(Direction.SHORT, 95.0) is not None
    assert tracker.opposing_zone(Direction.SHORT, 500.0) is None  # nowhere near


def test_touches_older_than_lookback_days_age_out():
    """Confirmed real 2026-09-24: the whole point is multi-day memory, but
    it must still bound itself -- a touch from well outside
    cfg.lookback_days shouldn't keep a zone alive forever."""
    tracker = ZoneTracker(cfg(min_touches=2, tolerance_points=10, lookback_days=3), TZ)
    day1 = [(101, 99), (103, 100), (105, 95), (102, 98), (100, 97)]  # swing low at 95, day 1
    feed(tracker, day1, start=START)

    assert tracker.support_zones[0].touch_count == 1

    # A second touch 10 days later, well past the 3-day lookback -- by
    # the time it's added, the first touch should already have aged out,
    # so this becomes the sole touch of what's really a fresh zone, not
    # a second touch on an old one.
    later_start = START + timedelta(days=10)
    day2 = [(103, 99), (106, 101), (108, 96), (104, 100), (102, 103)]  # swing low at 96
    feed(tracker, day2, start=later_start)

    assert len(tracker.support_zones) == 1
    assert tracker.support_zones[0].touch_count == 1
    assert tracker.support_zones[0].price == 96.0


def test_touches_within_lookback_days_both_count():
    tracker = ZoneTracker(cfg(min_touches=2, tolerance_points=10, lookback_days=7), TZ)
    day1 = [(101, 99), (103, 100), (105, 95), (102, 98), (100, 97)]  # swing low at 95
    feed(tracker, day1, start=START)

    later_start = START + timedelta(days=2)  # within the 7-day lookback
    day2 = [(103, 99), (106, 101), (108, 96), (104, 100), (102, 103)]  # swing low at 96
    feed(tracker, day2, start=later_start)

    assert len(tracker.support_zones) == 1
    assert tracker.support_zones[0].touch_count == 2


def test_reset_clears_everything():
    tracker = ZoneTracker(cfg(min_touches=1), TZ)
    feed(tracker, [(101, 99), (103, 100), (105, 95), (102, 98), (100, 97)])
    assert tracker.support_zones

    tracker.reset()

    assert tracker.support_zones == []
    assert tracker.resistance_zones == []
    assert tracker.opposing_zone(Direction.SHORT, 95.0) is None
