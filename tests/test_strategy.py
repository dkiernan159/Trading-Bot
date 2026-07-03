from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import load_config
from src.models import Bar, Direction
from src.strategy import OpeningRangeStrategy, State

TZ = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)  # a Monday


def bar(minutes_from_open: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=DAY + timedelta(minutes=minutes_from_open), open=o, high=h, low=l, close=c)


def load_test_config():
    from pathlib import Path

    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def feed_breakout_retest_and_fvg(strategy: OpeningRangeStrategy):
    """Drives the strategy through: box formation (9:30-9:44), the 9:45
    'box formed' bar, a breakout above the box, a retest, and a strong
    bullish 1m FVG -- returning the final (entry) signal."""
    signal = None

    # 15 x 1m bars building the 9:30-9:45 box: high=101.0, low=99.5
    for i in range(15):
        signal = strategy.on_bar(bar(i, 100.0, 101.0, 99.5, 100.5))
        assert signal is None

    # 9:45 bar marks the box formed, does not extend it
    signal = strategy.on_bar(bar(15, 100.5, 101.2, 100.0, 100.8))
    assert signal is None
    assert strategy.state is State.WAIT_BREAKOUT

    # 9:46 breakout above box high (101.0)
    signal = strategy.on_bar(bar(16, 100.8, 102.5, 100.7, 102.3))
    assert signal is None
    assert strategy.state is State.WAIT_RETEST

    # 9:47 -- no retest yet (low stays above box high)
    signal = strategy.on_bar(bar(17, 102.3, 103.0, 101.5, 102.8))
    assert signal is None
    assert strategy.state is State.WAIT_RETEST

    # 9:48 -- retest touches the broken box high (101.0)
    signal = strategy.on_bar(bar(18, 102.8, 103.0, 100.8, 101.5))
    assert signal is None
    assert strategy.state is State.WAIT_FVG

    # 9:49 (c0), 9:50 (c1 displacement), 9:51 (c2) -- strong bullish FVG
    signal = strategy.on_bar(bar(19, 101.5, 101.8, 101.0, 101.6))
    assert signal is None
    signal = strategy.on_bar(bar(20, 101.6, 104.6, 101.5, 104.5))
    assert signal is None
    signal = strategy.on_bar(bar(21, 104.5, 104.8, 104.0, 104.6))

    return signal


def test_full_breakout_retest_fvg_entry_sequence():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    signal = feed_breakout_retest_and_fvg(strategy)

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == 104.6
    assert strategy.state is State.IN_TRADE


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    signal = feed_breakout_retest_and_fvg(strategy)
    assert signal is not None

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar(22, 104.6, 104.6, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_RETEST


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
