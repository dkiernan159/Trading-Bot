from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.config import SessionConfig
from src.models import Bar
from src.session_levels import SessionLevels

TZ = ZoneInfo("America/New_York")


def make_cfg() -> SessionConfig:
    return SessionConfig(
        timezone="America/New_York",
        asia_start=time(19, 0),
        asia_end=time(23, 59),
        london_start=time(2, 0),
        london_end=time(5, 0),
        ny_open=time(9, 30),
        opening_range_end=time(9, 45),
        no_new_entries_after=time(11, 30),
        flatten_by=time(11, 45),
    )


def test_previous_day_zone_is_the_15m_candle_that_set_the_extreme():
    levels = SessionLevels(make_cfg())
    base = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)  # previous day, well outside asia/london windows

    # Three separate 15-minute buckets: a baseline one, one that sets the
    # day's high (105), and one that sets the day's low (95).
    levels.add_bar(Bar(timestamp=base, open=100.0, high=101.0, low=99.0, close=100.0))
    levels.add_bar(Bar(timestamp=base + timedelta(minutes=15), open=100.0, high=105.0, low=100.0, close=104.0))
    levels.add_bar(Bar(timestamp=base + timedelta(minutes=30), open=100.0, high=101.0, low=95.0, close=98.0))

    result = levels.levels_for(datetime(2026, 7, 6).date())

    assert result.previous_day_high == 105.0
    assert result.previous_day_low == 95.0
    # The zone is just the one 15m candle that set the extreme, not the
    # whole day's range.
    assert result.previous_day_high_zone == (100.0, 105.0)
    assert result.previous_day_low_zone == (95.0, 101.0)


def test_zone_spans_multiple_bars_within_the_same_15m_bucket():
    levels = SessionLevels(make_cfg())
    base = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)

    # Two 1-minute bars in the same 15m bucket (6:00 and 6:01) -- the zone
    # should reflect both, not just one.
    levels.add_bar(Bar(timestamp=base, open=100.0, high=103.0, low=99.5, close=101.0))
    levels.add_bar(Bar(timestamp=base + timedelta(minutes=1), open=101.0, high=104.0, low=100.5, close=102.0))

    result = levels.levels_for(datetime(2026, 7, 6).date())

    assert result.previous_day_high == 104.0
    assert result.previous_day_high_zone == (99.5, 104.0)


def test_levels_and_zones_are_none_when_no_bars_fed():
    levels = SessionLevels(make_cfg())
    result = levels.levels_for(datetime(2026, 7, 6).date())  # no bars fed at all

    assert result.previous_day_high is None
    assert result.previous_day_high_zone is None
    assert result.all_levels() == []
