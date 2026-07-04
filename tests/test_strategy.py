from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import load_config
from src.models import Bar, Direction
from src.strategy import OpeningRangeStrategy, State

TZ = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)  # a Monday
PREV_DAY_BASE = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)  # well before 9:30


def bar15(k: int, o: float, h: float, l: float, c: float) -> Bar:
    """Bar `k` stands in for one whole 15-minute candle -- DAY + 15*k
    minutes -- since the FVG detector now runs on 15m candles, not 1m."""
    return Bar(timestamp=DAY + timedelta(minutes=15 * k), open=o, high=h, low=l, close=c)


def load_test_config():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    # These tests exercise the box/breakout/retest/FVG mechanics across
    # several 15-minute candles, which comfortably exceeds the real
    # 11:30 ET cutoff -- push it out so the timing under test isn't the
    # no-new-entries cutoff (that's covered separately, see
    # test_stands_down_for_day_after_cutoff).
    cfg.session.no_new_entries_after = dtime(23, 59)
    return cfg


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
    """Bar 0 (9:30) forms the box (high=101/low=99.5), bar 1 (9:45) closes
    it, bar 2 (10:00) breaks out above it (LONG)."""
    signal = strategy.on_bar(bar15(0, 100.0, 101.0, 99.5, 100.5))
    assert signal is None
    signal = strategy.on_bar(bar15(1, 100.5, 101.2, 100.0, 100.8))
    assert signal is None
    assert strategy.state is State.WAIT_BREAKOUT
    signal = strategy.on_bar(bar15(2, 100.8, 103.0, 100.7, 102.5))
    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST


def feed_quiet_15m(strategy: OpeningRangeStrategy, start_k: int, count: int, o: float, h: float, l: float, c: float):
    for k in range(start_k, start_k + count):
        signal = strategy.on_bar(bar15(k, o, h, l, c))
        assert signal is None


def test_full_breakout_retest_then_fvg_fill_sequence():
    """Price retests the previous-day high (105) after breakout, then 8
    quiet 15m candles establish the average-range baseline, then a strong
    15m FVG forms (105.4-109.0) and the resulting midpoint limit (107.2)
    fills on a retrace."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    # bar 3 (10:15): retest -- range 102.3-105.5 touches the previous-day high (105)
    signal = strategy.on_bar(bar15(3, 102.5, 105.5, 102.3, 105.0))
    assert signal is None
    assert strategy.state is State.WAIT_FVG

    feed_quiet_15m(strategy, 4, 8, 105.0, 105.5, 104.5, 105.0)  # bars 4-11: baseline

    signal = strategy.on_bar(bar15(12, 105.0, 105.4, 104.7, 105.1))  # c0
    assert signal is None
    signal = strategy.on_bar(bar15(13, 105.1, 109.5, 105.0, 109.3))  # c1: displacement
    assert signal is None
    signal = strategy.on_bar(bar15(14, 109.3, 109.8, 109.0, 109.5))  # c2: confirms gap 105.4-109.0
    assert signal is None
    signal = strategy.on_bar(bar15(15, 109.5, 109.6, 109.4, 109.5))  # flush -- finalizes c2, detects the FVG
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    signal = strategy.on_bar(bar15(16, 109.5, 109.8, 106.0, 107.5))  # retrace fills the 107.2 midpoint

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == 107.2  # midpoint of 105.4-109.0
    assert strategy.state is State.IN_TRADE


def test_uses_a_pre_existing_unmitigated_fvg_once_retest_completes():
    """A strong, correctly-directed FVG that forms in a completely
    unrelated price area (62-68, nowhere near the marked levels or the
    box) *before* the retest happens must sit unused until the retest
    occurs -- but once it does, that already-formed, still-unmitigated
    FVG is used immediately (no need to wait for a fresh one to form
    after the retest)."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    feed_quiet_15m(strategy, 3, 8, 63.0, 63.5, 62.5, 63.0)  # bars 3-10: baseline, still WAIT_KEY_LEVEL_RETEST
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST

    strategy.on_bar(bar15(11, 63.0, 63.6, 62.7, 63.3))  # c0
    strategy.on_bar(bar15(12, 63.3, 68.5, 63.2, 68.3))  # c1: displacement
    strategy.on_bar(bar15(13, 68.3, 68.8, 67.6, 68.5))  # c2: confirms gap 63.6-67.6
    signal = strategy.on_bar(bar15(14, 68.5, 68.6, 68.4, 68.5))  # flush -- detects the FVG

    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST  # still hasn't been retested

    # bar 15: retest -- range 102.3-105.5 touches the previous-day high (105)
    signal = strategy.on_bar(bar15(15, 102.5, 105.5, 102.3, 105.0))
    assert signal is None
    assert strategy.state is State.WAIT_FVG

    # bar 16: the state machine picks up the already-formed, still-unmitigated
    # FVG from before the retest -- straight to WAIT_FILL, no new FVG needed.
    signal = strategy.on_bar(bar15(16, 68.5, 68.6, 68.4, 68.5))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    signal = strategy.on_bar(bar15(17, 68.5, 68.6, 64.0, 65.0))  # retrace fills the 65.6 midpoint

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == 65.6  # midpoint of 63.6-67.6


def test_abandons_fvg_that_gets_mitigated_before_fill():
    """If price blows straight through the far side of the pending FVG
    (105.4-109.0, LONG, midpoint 107.2) instead of retracing cleanly to
    the midpoint, the gap is mitigated/broken and must NOT be treated as
    a fill -- even though the same bar's low also crosses the midpoint.
    The bot should abandon it and go back to watching for a fresh FVG."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    strategy.on_bar(bar15(3, 102.5, 105.5, 102.3, 105.0))
    feed_quiet_15m(strategy, 4, 8, 105.0, 105.5, 104.5, 105.0)
    strategy.on_bar(bar15(12, 105.0, 105.4, 104.7, 105.1))  # c0
    strategy.on_bar(bar15(13, 105.1, 109.5, 105.0, 109.3))  # c1: displacement
    strategy.on_bar(bar15(14, 109.3, 109.8, 109.0, 109.5))  # c2: confirms gap 105.4-109.0
    strategy.on_bar(bar15(15, 109.5, 109.6, 109.4, 109.5))  # flush
    assert strategy.state is State.WAIT_FILL

    # Instead of retracing to the 107.2 midpoint, price drops clean through
    # the whole gap and beyond its far (low) edge of 105.4.
    signal = strategy.on_bar(bar15(16, 109.5, 109.8, 105.0, 105.2))

    assert signal is None
    assert strategy.state is State.WAIT_FVG
    assert strategy.stats["fvgs_mitigated_before_fill"] == 1
    assert strategy.stats["fills"] == 0


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    strategy.on_bar(bar15(3, 102.5, 105.5, 102.3, 105.0))
    feed_quiet_15m(strategy, 4, 8, 105.0, 105.5, 104.5, 105.0)
    strategy.on_bar(bar15(12, 105.0, 105.4, 104.7, 105.1))
    strategy.on_bar(bar15(13, 105.1, 109.5, 105.0, 109.3))
    strategy.on_bar(bar15(14, 109.3, 109.8, 109.0, 109.5))
    strategy.on_bar(bar15(15, 109.5, 109.6, 109.4, 109.5))
    signal = strategy.on_bar(bar15(16, 109.5, 109.8, 106.0, 107.5))
    assert signal is not None

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar15(17, 107.5, 107.5, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_RETEST


def test_stands_down_for_day_after_cutoff():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
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
