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
    """Marks a previous-day high of 105 and low of 95."""
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
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST


def feed_quiet_baseline(strategy: OpeningRangeStrategy, start: int, end: int, o: float, h: float, l: float, c: float):
    for i in range(start, end):
        signal = strategy.on_bar(bar(i, o, h, l, c))
        assert signal is None


def test_full_breakout_retest_then_fvg_fill_sequence():
    """Price retests the previous-day high (105) after breakout, THEN a
    strong FVG forms well away from that level (105.4-107.0, nowhere near
    95 or 105) -- and still triggers, because the FVG no longer needs to
    overlap the level, only follow a retest of one."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    # bar 17: retest -- range 102.3-105.5 touches the previous-day high (105)
    signal = strategy.on_bar(bar(17, 102.5, 105.5, 102.3, 105.0))
    assert signal is None
    assert strategy.state is State.WAIT_FVG

    feed_quiet_baseline(strategy, 18, 38, 105.0, 105.5, 104.5, 105.0)

    signal = strategy.on_bar(bar(38, 105.0, 105.4, 104.7, 105.1))  # c0
    assert signal is None
    signal = strategy.on_bar(bar(39, 105.1, 108.2, 105.0, 108.0))  # c1: displacement
    assert signal is None
    signal = strategy.on_bar(bar(40, 108.0, 108.5, 107.0, 108.3))  # c2: confirms gap 105.4-107.0
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    signal = strategy.on_bar(bar(41, 108.3, 108.5, 105.8, 106.5))  # retrace fills the 106.2 midpoint

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == 106.2  # midpoint of 105.4-107.0
    assert strategy.state is State.IN_TRADE


def test_does_not_enter_on_fvg_before_any_retest():
    """A strong, correctly-directed FVG that forms before price ever
    touches a marked key level must not trigger -- the retest has to
    happen first. Uses a price range (62-67.5) that never crosses either
    marked level (95 or 105), so the displacement candle's own wide range
    can't accidentally satisfy the retest."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    feed_quiet_baseline(strategy, 17, 37, 63.0, 63.5, 62.5, 63.0)

    strategy.on_bar(bar(37, 63.0, 63.6, 62.7, 63.3))  # c0
    signal = strategy.on_bar(bar(38, 63.3, 67.2, 63.2, 67.0))  # c1: displacement
    assert signal is None
    signal = strategy.on_bar(bar(39, 67.0, 67.5, 66.0, 67.3))  # c2: confirms gap 63.6-66.0

    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST  # never advanced to WAIT_FVG


def test_abandons_fvg_that_gets_mitigated_before_fill():
    """If price blows straight through the far side of the pending FVG
    (105.4-107.0, LONG, midpoint 106.2) instead of retracing cleanly to the
    midpoint, the gap is mitigated/broken and must NOT be treated as a
    fill -- even though the same bar's low also crosses the midpoint. The
    bot should abandon it and go back to watching for a fresh FVG."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    strategy.on_bar(bar(17, 102.5, 105.5, 102.3, 105.0))
    feed_quiet_baseline(strategy, 18, 38, 105.0, 105.5, 104.5, 105.0)
    strategy.on_bar(bar(38, 105.0, 105.4, 104.7, 105.1))  # c0
    strategy.on_bar(bar(39, 105.1, 108.2, 105.0, 108.0))  # c1: displacement
    strategy.on_bar(bar(40, 108.0, 108.5, 107.0, 108.3))  # c2: confirms gap 105.4-107.0
    assert strategy.state is State.WAIT_FILL

    # Instead of retracing to the 106.2 midpoint, price drops clean through
    # the whole gap and beyond its far (low) edge of 105.4.
    signal = strategy.on_bar(bar(41, 108.3, 108.5, 105.0, 105.2))

    assert signal is None
    assert strategy.state is State.WAIT_FVG
    assert strategy.stats["fvgs_mitigated_before_fill"] == 1
    assert strategy.stats["fills"] == 0


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    strategy.on_bar(bar(17, 102.5, 105.5, 102.3, 105.0))
    feed_quiet_baseline(strategy, 18, 38, 105.0, 105.5, 104.5, 105.0)
    strategy.on_bar(bar(38, 105.0, 105.4, 104.7, 105.1))
    strategy.on_bar(bar(39, 105.1, 108.2, 105.0, 108.0))
    strategy.on_bar(bar(40, 108.0, 108.5, 107.0, 108.3))
    signal = strategy.on_bar(bar(41, 108.3, 108.5, 105.8, 106.5))
    assert signal is not None

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar(42, 106.5, 106.5, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST


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
