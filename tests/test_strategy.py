from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import load_config
from src.models import Bar, Direction
from src.strategy import OpeningRangeStrategy, State

TZ = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)  # a Monday
PREV_DAY_BASE = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)  # well before 9:30


def bar(minutes_from_open: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=DAY + timedelta(minutes=minutes_from_open), open=o, high=h, low=l, close=c)


def load_test_config():
    from pathlib import Path

    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def feed_previous_day_levels(strategy: OpeningRangeStrategy):
    """Marks a previous-day high of 105 (set by a 15m candle spanning
    100-105) and a previous-day low of 95 (set by a 15m candle spanning
    95-101) -- each in its own 15-minute bucket, so the "zone" around each
    extreme is narrow rather than spanning the whole day."""
    strategy.on_bar(Bar(timestamp=PREV_DAY_BASE, open=100.0, high=101.0, low=99.0, close=100.0))
    strategy.on_bar(
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=15), open=100.0, high=105.0, low=100.0, close=104.0)
    )
    strategy.on_bar(
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=30), open=100.0, high=101.0, low=95.0, close=98.0)
    )


def feed_box_and_breakout(strategy: OpeningRangeStrategy):
    for i in range(15):
        signal = strategy.on_bar(bar(i, 100.0, 101.0, 99.5, 100.5))
        assert signal is None
    signal = strategy.on_bar(bar(15, 100.5, 101.2, 100.0, 100.8))
    assert signal is None
    assert strategy.state is State.WAIT_BREAKOUT
    signal = strategy.on_bar(bar(16, 100.8, 103.0, 100.7, 102.5))
    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_FVG


def feed_quiet_baseline(strategy: OpeningRangeStrategy):
    for i in range(17, 37):
        signal = strategy.on_bar(bar(i, 103.0, 103.5, 102.5, 103.0))
        assert signal is None


def test_full_breakout_key_level_fvg_fill_sequence():
    """The FVG (101.5-104.0) overlaps the previous-day-high zone (100-105)
    without containing the exact tick (105) -- confirming the "around that
    resistance area" rule, not exact containment."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_quiet_baseline(strategy)

    signal = strategy.on_bar(bar(37, 101.2, 101.5, 101.0, 101.4))  # c0
    assert signal is None
    signal = strategy.on_bar(bar(38, 101.4, 105.2, 101.3, 105.0))  # c1: displacement
    assert signal is None
    signal = strategy.on_bar(bar(39, 105.0, 105.3, 104.0, 105.1))  # c2: confirms gap 101.5-104.0
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    signal = strategy.on_bar(bar(40, 105.1, 105.5, 102.5, 103.0))  # retrace fills the 102.75 midpoint

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == 102.75  # midpoint of 101.5-104.0
    assert strategy.state is State.IN_TRADE


def test_does_not_enter_on_fvg_without_a_key_level():
    """A strong, correctly-directed FVG whose gap (110-113) doesn't overlap
    either marked zone (100-105 or 95-101) must not trigger an entry."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_quiet_baseline(strategy)

    strategy.on_bar(bar(37, 109.5, 110.0, 109.3, 109.8))  # c0
    signal = strategy.on_bar(bar(38, 109.8, 114.2, 109.7, 114.0))  # c1: displacement
    assert signal is None
    signal = strategy.on_bar(bar(39, 114.0, 114.5, 113.0, 114.2))  # c2: confirms gap 110-113

    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_FVG  # never advanced to WAIT_FILL


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_quiet_baseline(strategy)
    strategy.on_bar(bar(37, 101.2, 101.5, 101.0, 101.4))
    strategy.on_bar(bar(38, 101.4, 105.2, 101.3, 105.0))
    strategy.on_bar(bar(39, 105.0, 105.3, 104.0, 105.1))
    signal = strategy.on_bar(bar(40, 105.1, 105.5, 102.5, 103.0))
    assert signal is not None

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar(41, 103.0, 103.0, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_FVG


def test_stands_down_for_day_after_cutoff():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    # Jump straight to a bar past the no-new-entries cutoff (11:30 ET default).
    late_bar = Bar(
        timestamp=DAY.replace(hour=11, minute=35),
        open=100.0,
        high=100.5,
        low=99.5,
        close=100.0,
    )
    signal = strategy.on_bar(late_bar)

    assert signal is None
    assert strategy.state is State.DONE_FOR_DAY
