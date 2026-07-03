from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import SessionConfig
from src.models import Bar
from src.opening_range import OpeningRangeBox

TZ = ZoneInfo("America/New_York")


def make_cfg() -> SessionConfig:
    from datetime import time

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


def bar_at(dt: datetime, h: float, l: float) -> Bar:
    return Bar(timestamp=dt, open=(h + l) / 2, high=h, low=l, close=(h + l) / 2)


def test_box_accumulates_high_low_and_forms_at_945():
    box = OpeningRangeBox(make_cfg())
    day = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)

    highs_lows = [(101.0, 99.5), (100.5, 99.0), (102.0, 100.0)]
    for i, (h, l) in enumerate(highs_lows):
        box.add_bar(bar_at(day + timedelta(minutes=i), h, l))
        assert not box.is_formed

    assert box.high == 102.0
    assert box.low == 99.0

    # bar at 9:45 itself does not extend the box, but marks it formed
    box.add_bar(bar_at(day.replace(hour=9, minute=45), 500.0, 1.0))
    assert box.is_formed
    assert box.high == 102.0
    assert box.low == 99.0


def test_box_resets_automatically_on_new_day():
    box = OpeningRangeBox(make_cfg())
    day1 = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)
    day2 = datetime(2026, 7, 7, 9, 30, tzinfo=TZ)

    box.add_bar(bar_at(day1, 101.0, 99.5))
    box.add_bar(bar_at(day1.replace(hour=9, minute=45), 500.0, 1.0))
    assert box.is_formed

    box.add_bar(bar_at(day2, 50.0, 40.0))
    assert not box.is_formed
    assert box.high == 50.0
    assert box.low == 40.0
