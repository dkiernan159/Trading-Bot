from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import load_config
from src.models import Bar, Direction
from src.strategy import OpeningRangeStrategy, State

TZ = ZoneInfo("America/New_York")
PREV_DAY = datetime(2026, 7, 5, 7, 0, tzinfo=TZ)  # before 9:30, so it's just recorded as history
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)  # a Monday


def bar(minutes_from_open: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=DAY + timedelta(minutes=minutes_from_open), open=o, high=h, low=l, close=c)


def load_test_config():
    from pathlib import Path

    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def feed_box_breakout_and_key_level_fvg(strategy: OpeningRangeStrategy):
    """Drives the strategy through: marking a previous day high/low of
    105/95, box formation (9:30-9:45, high=101/low=99.5), a breakout above
    the box, a quiet run-up, then a strong bullish FVG (gap 103.6-106.0)
    that contains the previous-day high (105) -- which rests a limit order
    at the FVG midpoint (104.8) -- and finally a retrace bar that fills it.
    Returns the final (entry) signal."""
    signal = strategy.on_bar(
        Bar(timestamp=PREV_DAY, open=100.0, high=105.0, low=95.0, close=100.0)
    )
    assert signal is None

    # 15 x 1m bars building the 9:30-9:45 box: high=101.0, low=99.5
    for i in range(15):
        signal = strategy.on_bar(bar(i, 100.0, 101.0, 99.5, 100.5))
        assert signal is None

    # 9:45 bar marks the box formed, does not extend it
    signal = strategy.on_bar(bar(15, 100.5, 101.2, 100.0, 100.8))
    assert signal is None
    assert strategy.state is State.WAIT_BREAKOUT

    # 9:46 breakout above box high (101.0)
    signal = strategy.on_bar(bar(16, 100.8, 103.0, 100.7, 102.5))
    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_FVG

    # 20 quiet/doji baseline bars (O == C, so no FVG can match here regardless
    # of gap size) establishing the average range used for FVG strength
    for i in range(17, 37):
        signal = strategy.on_bar(bar(i, 103.0, 103.5, 102.5, 103.0))
        assert signal is None
        assert strategy.state is State.WAIT_KEY_LEVEL_FVG

    # c0: quiet candle
    signal = strategy.on_bar(bar(37, 103.0, 103.6, 102.7, 103.3))
    assert signal is None

    # c1: strong bullish displacement candle (body 3.7, avg range ~1.0)
    signal = strategy.on_bar(bar(38, 103.3, 107.2, 103.2, 107.0))
    assert signal is None

    # c2: confirms the gap (c0.high=103.6 < c2.low=106.0) -- and this gap
    # (103.6-106.0) contains the previous-day high of 105 -> valid key-level FVG.
    signal = strategy.on_bar(bar(39, 107.0, 107.5, 106.0, 107.3))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    # retrace bar: low (104.5) reaches the FVG midpoint (104.8) -> fills
    signal = strategy.on_bar(bar(40, 107.3, 107.5, 104.5, 105.0))

    return signal


def test_full_breakout_key_level_fvg_fill_sequence():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    signal = feed_box_breakout_and_key_level_fvg(strategy)

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == 104.8  # midpoint of the 103.6-106.0 FVG
    assert strategy.state is State.IN_TRADE


def test_does_not_enter_on_fvg_without_a_key_level():
    """A strong, correctly-directed FVG that does NOT contain any marked
    key level must not trigger an entry -- confirms the key-level
    requirement is actually enforced, not just direction + strength."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    # Same previous-day levels (105/95) and box/breakout as the passing case.
    strategy.on_bar(Bar(timestamp=PREV_DAY, open=100.0, high=105.0, low=95.0, close=100.0))
    for i in range(15):
        strategy.on_bar(bar(i, 100.0, 101.0, 99.5, 100.5))
    strategy.on_bar(bar(15, 100.5, 101.2, 100.0, 100.8))
    strategy.on_bar(bar(16, 100.8, 103.0, 100.7, 102.5))
    for i in range(17, 37):
        strategy.on_bar(bar(i, 103.0, 103.5, 102.5, 103.0))

    # Same shape of FVG as before, but shifted down so its gap (100.6-103.0)
    # sits well below the previous-day high (105) and previous-day low (95)
    # doesn't fall inside it either -- no key level in range.
    strategy.on_bar(bar(37, 103.0, 100.6, 99.7, 100.3))  # note: deliberately low c0
    signal = strategy.on_bar(bar(38, 100.3, 104.2, 100.2, 104.0))
    assert signal is None
    signal = strategy.on_bar(bar(39, 104.0, 104.5, 103.0, 104.3))

    assert signal is None
    assert strategy.state is State.WAIT_KEY_LEVEL_FVG  # never advanced to WAIT_FILL


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    signal = feed_box_breakout_and_key_level_fvg(strategy)
    assert signal is not None

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar(41, 105.0, 105.0, 99.0, 99.0))
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
