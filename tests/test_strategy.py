from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import load_config
from src.models import Bar, Direction
from src.strategy import OpeningRangeStrategy, State

TZ = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)  # a Monday
PREV_DAY_BASE = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)  # well before 9:30

WICK = 0.2  # wide enough that a smooth 1-minute walk never forms its own 1m gap


def bar_at(dt: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=dt, open=o, high=h, low=l, close=c)


def flat_bar(dt: datetime, price: float, spread: float = 0.5) -> Bar:
    """A single bar standing in for a whole quiet, unchanging 15-minute
    candle -- safe to use sparsely (one bar per 15-minute mark) because a
    repeated, unchanging value can never itself form a gap, so it doesn't
    also register as a false 1-minute FVG."""
    return bar_at(dt, price, price + spread / 2, price - spread / 2, price)


def smooth_walk_1m(start: datetime, minutes: int, start_price: float, end_price: float) -> list[Bar]:
    """`minutes` consecutive real 1-minute bars walking smoothly from
    start_price to end_price. The slope is gentle relative to WICK so no
    3 consecutive bars ever form their own 1-minute gap -- this lets the
    same price action build a genuine, non-contaminating 15m candle (the
    15m detector aggregates these into one candle; the 1m detector sees
    each individually but finds nothing gap-shaped in a smooth ramp)."""
    bars = []
    for i in range(minutes):
        o = start_price + (end_price - start_price) * i / minutes
        c = start_price + (end_price - start_price) * (i + 1) / minutes
        h, l = (o, c) if o >= c else (c, o)
        bars.append(bar_at(start + timedelta(minutes=i), o, h + WICK, l - WICK, c))
    return bars


def load_test_config():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    # These tests exercise the box/breakout/FVG mechanics well past the
    # real 11:30 ET cutoff -- push it out so the timing under test isn't
    # the no-new-entries cutoff (covered separately, see
    # test_stands_down_for_day_after_cutoff).
    cfg.session.no_new_entries_after = dtime(23, 59)
    # Smaller lookback so the nested 1m FVG's baseline doesn't need 20
    # bars of setup for every test.
    cfg.strategy.entry_fvg.lookback_bars = 5
    return cfg


def feed_previous_day_levels(strategy: OpeningRangeStrategy):
    """Marks a previous-day high of 105 and low of 95 (kept only for stop-
    loss structural-level reference now, not for entry gating)."""
    strategy.on_bar(bar_at(PREV_DAY_BASE, 100.0, 101.0, 99.0, 100.0))
    strategy.on_bar(bar_at(PREV_DAY_BASE + timedelta(minutes=15), 100.0, 105.0, 100.0, 104.0))
    strategy.on_bar(bar_at(PREV_DAY_BASE + timedelta(minutes=30), 100.0, 101.0, 95.0, 98.0))


def feed_box_and_breakout(strategy: OpeningRangeStrategy):
    """9:30 forms the box (high=101/low=99.5), 9:45 closes it, 10:00
    breaks out above it (LONG)."""
    signal = strategy.on_bar(bar_at(DAY, 100.0, 101.0, 99.5, 100.5))
    assert signal is None
    signal = strategy.on_bar(bar_at(DAY + timedelta(minutes=15), 100.5, 101.2, 100.0, 100.8))
    assert signal is None
    assert strategy.state is State.WAIT_BREAKOUT
    signal = strategy.on_bar(bar_at(DAY + timedelta(minutes=30), 100.8, 103.0, 100.7, 102.5))
    assert signal is None
    assert strategy.state is State.WAIT_15M_FVG


def feed_quiet_15m(strategy: OpeningRangeStrategy, start: datetime, count: int, price: float):
    for i in range(count):
        signal = strategy.on_bar(flat_bar(start + timedelta(minutes=15 * i), price))
        assert signal is None


def feed_large_15m_fvg(strategy: OpeningRangeStrategy, start: datetime) -> tuple[float, float]:
    """Feeds 8 quiet 15m baseline candles, then a real (dense, 1-minute
    resolution) displacement move from 105.1 up to 109.3 that forms a
    large bullish 15m FVG, then a flush bar to finalize detection.
    Returns the resulting (gap_low, gap_high)."""
    feed_quiet_15m(strategy, start, 8, 105.0)
    pattern_start = start + timedelta(minutes=15 * 8)

    c0_bars = smooth_walk_1m(pattern_start, 15, 105.0, 105.1)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=15), 15, 105.1, 109.3)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=30), 15, 109.3, 109.5)
    for b in c0_bars + c1_bars + c2_bars:
        signal = strategy.on_bar(b)
        assert signal is None

    gap_low = max(b.high for b in c0_bars)
    gap_high = min(b.low for b in c2_bars)

    flush_time = pattern_start + timedelta(minutes=45)
    signal = strategy.on_bar(flat_bar(flush_time, 109.5))  # finalizes c2's 15m candle, detects the FVG
    assert signal is None
    assert strategy.state is State.WAIT_1M_FVG

    return gap_low, gap_high


def feed_nested_1m_fvg(strategy: OpeningRangeStrategy, start: datetime) -> tuple[float, float]:
    """Feeds a small 1-minute FVG (baseline + 3-candle pattern + flush)
    around 106-107.5, nested inside the 105.x-109.x anchor. Returns the
    resulting (gap_low, gap_high)."""
    for i in range(5):
        strategy.on_bar(bar_at(start + timedelta(minutes=i), 106.0, 106.2, 105.9, 106.0))
    strategy.on_bar(bar_at(start + timedelta(minutes=5), 106.0, 106.2, 105.9, 106.0))  # c0
    strategy.on_bar(bar_at(start + timedelta(minutes=6), 106.0, 107.5, 105.9, 107.4))  # c1: displacement
    strategy.on_bar(bar_at(start + timedelta(minutes=7), 107.4, 107.6, 107.0, 107.5))  # c2: confirms gap
    signal = strategy.on_bar(bar_at(start + timedelta(minutes=8), 107.5, 107.6, 107.4, 107.5))  # flush
    assert signal is None
    assert strategy.state is State.WAIT_FILL
    return 106.2, 107.0  # gap_low (c0.high), gap_high (c2.low)


def test_full_breakout_15m_anchor_then_nested_1m_entry():
    """Breakout sets direction, a large 15m FVG anchors the move, a small
    1m FVG nested inside that anchor is the actual entry trigger (kept
    tight so the stop isn't sized off 15m-candle noise)."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    anchor_low, anchor_high = feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    assert anchor_low < anchor_high

    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    nested_low, nested_high = feed_nested_1m_fvg(strategy, nested_start)

    # The nested 1m gap is fully inside the 15m anchor.
    assert anchor_low <= nested_low
    assert nested_high <= anchor_high

    midpoint = (nested_low + nested_high) / 2
    fill_time = nested_start + timedelta(minutes=9)
    signal = strategy.on_bar(bar_at(fill_time, 107.5, 107.6, 106.3, 106.8))  # retrace fills the midpoint

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == midpoint
    assert strategy.state is State.IN_TRADE


def test_abandons_15m_anchor_mitigated_before_a_nested_entry_forms():
    """If the 15m anchor FVG gets mitigated (price breaks its far/low
    side) before any nested 1m FVG ever forms inside it, the anchor is
    abandoned and the bot goes back to looking for a fresh one."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, _ = feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    assert strategy.state is State.WAIT_1M_FVG

    mitigate_time = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    signal = strategy.on_bar(bar_at(mitigate_time, anchor_low, anchor_low + 0.1, anchor_low - 1.0, anchor_low - 0.5))

    assert signal is None
    assert strategy.state is State.WAIT_15M_FVG
    assert strategy.stats["anchor_15m_fvgs_mitigated_before_entry"] == 1


def test_abandons_nested_1m_fvg_mitigated_before_fill():
    """If the nested 1m entry FVG gets mitigated before price retraces to
    its midpoint, it's abandoned (same as the old single-stage mitigation
    rule) but the 15m anchor itself is untouched, so the bot keeps
    watching for another nested 1m FVG inside the same anchor."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))

    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    nested_low, _ = feed_nested_1m_fvg(strategy, nested_start)

    # Breaches the nested gap's low (106.2) without breaching the wider
    # 15m anchor's low (105.3), isolating this to a nested-only mitigation.
    mitigate_time = nested_start + timedelta(minutes=9)
    signal = strategy.on_bar(bar_at(mitigate_time, 107.5, 107.6, nested_low - 0.2, nested_low - 0.1))

    assert signal is None
    assert strategy.state is State.WAIT_1M_FVG
    assert strategy.stats["entry_1m_fvgs_mitigated_before_fill"] == 1
    assert strategy.stats["fills"] == 0


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    nested_low, nested_high = feed_nested_1m_fvg(strategy, nested_start)
    midpoint = (nested_low + nested_high) / 2
    fill_time = nested_start + timedelta(minutes=9)
    signal = strategy.on_bar(bar_at(fill_time, 107.5, 107.6, 106.3, 106.8))
    assert signal is not None
    assert signal.entry_price == midpoint

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar_at(fill_time + timedelta(minutes=1), 106.8, 106.8, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_15M_FVG


def test_stands_down_for_day_after_cutoff():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    strategy = OpeningRangeStrategy(cfg)

    # Jump straight to a bar past the no-new-entries cutoff (11:30 ET default).
    late_bar = bar_at(DAY.replace(hour=11, minute=35), 100.0, 100.5, 99.5, 100.0)
    signal = strategy.on_bar(late_bar)

    assert signal is None
    assert strategy.state is State.DONE_FOR_DAY
