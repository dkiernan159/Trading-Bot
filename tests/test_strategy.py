from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
    # real 12:30 ET cutoff -- push it out so the timing under test isn't
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


def feed_large_15m_fvg(
    strategy: OpeningRangeStrategy, start: datetime, quiet_price: float = 105.0, c1_end: float = 109.3
) -> tuple[float, float]:
    """Feeds 8 quiet 15m baseline candles at quiet_price, then a real
    (dense, 1-minute resolution) displacement move from quiet_price+0.1 up
    to c1_end that forms a large bullish 15m FVG, then a flush bar to
    finalize detection. Returns the resulting (gap_low, gap_high). Default
    values (105.0, 109.3) produce the same 105.3-109.1 gap used throughout
    these tests; pass different values to build a second, distinct anchor
    elsewhere on the chart."""
    feed_quiet_15m(strategy, start, 8, quiet_price)
    pattern_start = start + timedelta(minutes=15 * 8)

    c0_bars = smooth_walk_1m(pattern_start, 15, quiet_price, quiet_price + 0.1)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=15), 15, quiet_price + 0.1, c1_end)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=30), 15, c1_end, c1_end + 0.2)
    for b in c0_bars + c1_bars + c2_bars:
        signal = strategy.on_bar(b)
        assert signal is None

    gap_low = max(b.high for b in c0_bars)
    gap_high = min(b.low for b in c2_bars)

    flush_time = pattern_start + timedelta(minutes=45)
    signal = strategy.on_bar(flat_bar(flush_time, c1_end + 0.2))  # finalizes c2's 15m candle, detects the FVG
    assert signal is None
    assert strategy.state is State.WAIT_1M_FVG

    return gap_low, gap_high


def feed_1m_fvg_pattern(strategy: OpeningRangeStrategy, start: datetime) -> tuple[float, float]:
    """Feeds a small 1-minute FVG (baseline + 3-candle pattern + flush)
    around 106-107.5. Returns the resulting (gap_low, gap_high). Makes no
    assertion about strategy state -- callers decide whether this should
    be picked up as an entry trigger."""
    for i in range(5):
        strategy.on_bar(bar_at(start + timedelta(minutes=i), 106.0, 106.2, 105.9, 106.0))
    strategy.on_bar(bar_at(start + timedelta(minutes=5), 106.0, 106.2, 105.9, 106.0))  # c0
    strategy.on_bar(bar_at(start + timedelta(minutes=6), 106.0, 107.5, 105.9, 107.4))  # c1: displacement
    strategy.on_bar(bar_at(start + timedelta(minutes=7), 107.4, 107.6, 107.0, 107.5))  # c2: confirms gap
    strategy.on_bar(bar_at(start + timedelta(minutes=8), 107.5, 107.6, 107.4, 107.5))  # flush -- detects it
    return 106.2, 107.0  # gap_low (c0.high), gap_high (c2.low)


def feed_nested_1m_fvg(strategy: OpeningRangeStrategy, start: datetime) -> tuple[float, float]:
    """Same as feed_1m_fvg_pattern, but asserts it was actually picked up
    as the pending entry trigger (used by tests where that's expected)."""
    gap_low, gap_high = feed_1m_fvg_pattern(strategy, start)
    assert strategy.state is State.WAIT_FILL
    return gap_low, gap_high


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


def test_nested_only_requires_midpoint_inside_anchor_not_full_containment():
    """A 1m FVG doesn't need to fit entirely inside the anchor's range --
    only its midpoint (the actual entry price) does. Requiring the whole
    gap to fit inside a tight anchor left very little room and was
    starving the bot of entries."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))  # anchor 105.3-109.1
    assert strategy._anchor_fvg.gap_low == pytest.approx(105.3)

    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    # This 1m FVG's low edge (104.9) falls OUTSIDE the anchor's low (105.3),
    # but its midpoint (105.7) is still inside the anchor's range.
    strategy.on_bar(bar_at(nested_start, 104.7, 104.9, 104.5, 104.7))  # c0
    strategy.on_bar(bar_at(nested_start + timedelta(minutes=1), 104.7, 106.7, 104.5, 106.6))  # c1: displacement
    strategy.on_bar(bar_at(nested_start + timedelta(minutes=2), 106.6, 106.8, 106.5, 106.7))  # c2: confirms 104.9-106.5
    signal = strategy.on_bar(bar_at(nested_start + timedelta(minutes=3), 106.7, 106.8, 106.6, 106.7))  # flush

    assert signal is None
    assert strategy.state is State.WAIT_FILL
    assert strategy._pending_fvg.gap_low == pytest.approx(104.9)
    assert strategy._pending_limit_price == pytest.approx(105.7)


def feed_large_15m_fvg_with_embedded_1m_impostor(strategy: OpeningRangeStrategy, start: datetime) -> dict:
    """Same overall 15m anchor as feed_large_15m_fvg (gap 105.3-109.1,
    from a displacement move 105.1 -> 109.3), but the displacement leg
    isn't a perfectly smooth ramp this time -- partway through it, price
    pauses and displaces again over 3 sharp 1-minute candles, leaving
    behind a small, genuine 1-minute FVG (105.9-106.7) that survives
    unmitigated (price only continues upward afterward, on its way to
    109.3). This mirrors what a real displacement leg looks like --
    real price action isn't a smooth ramp -- and reproduces the exact
    scenario that let the bot claim a stale, embedded 1m gap as its
    entry trigger instead of waiting for a fresh retest after the anchor
    locked in."""
    feed_quiet_15m(strategy, start, 8, 105.0)
    pattern_start = start + timedelta(minutes=15 * 8)

    c0_bars = smooth_walk_1m(pattern_start, 15, 105.0, 105.1)

    c1_start = pattern_start + timedelta(minutes=15)
    c1_bars = smooth_walk_1m(c1_start, 6, 105.1, 105.7)  # smooth run-up
    c1_bars += [
        bar_at(c1_start + timedelta(minutes=6), 105.7, 105.9, 105.6, 105.75),  # embedded c0''
        bar_at(c1_start + timedelta(minutes=7), 105.75, 107.2, 105.6, 107.1),  # embedded c1'': displacement
        bar_at(c1_start + timedelta(minutes=8), 107.1, 107.3, 106.7, 107.2),  # embedded c2'': confirms 105.9-106.7
    ]
    c1_bars += smooth_walk_1m(c1_start + timedelta(minutes=9), 6, 107.2, 109.3)  # smooth run-up, resumes

    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=30), 15, 109.3, 109.5)
    for b in c0_bars + c1_bars + c2_bars:
        signal = strategy.on_bar(b)
        assert signal is None

    anchor_low = max(b.high for b in c0_bars)
    anchor_high = min(b.low for b in c2_bars)

    flush_time = pattern_start + timedelta(minutes=45)
    signal = strategy.on_bar(flat_bar(flush_time, 109.5))  # finalizes c2's 15m candle, detects the anchor
    assert signal is None
    assert strategy.state is State.WAIT_1M_FVG

    return {
        "anchor_low": anchor_low,
        "anchor_high": anchor_high,
        "impostor_low": 105.9,
        "impostor_high": 106.7,
        "flush_time": flush_time,
    }


def test_ignores_a_1m_fvg_embedded_in_the_anchors_own_displacement():
    """A 1-minute FVG that formed as part of the same displacement move
    that built the 15m anchor -- not a separate, later retracement --
    must not be used as the entry trigger, even though it's nested inside
    the anchor's range and never gets mitigated. Otherwise the bot claims
    a stale gap the instant the anchor confirms, which looks like
    entering as the anchor forms rather than waiting for a genuine retest
    afterward."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    info = feed_large_15m_fvg_with_embedded_1m_impostor(strategy, DAY + timedelta(minutes=45))
    assert info["anchor_low"] <= info["impostor_low"]
    assert info["impostor_high"] <= info["anchor_high"]  # geometrically nested, but embedded/stale

    # This bar dips right into the impostor's gap (would fill its 106.3
    # midpoint if it were wrongly considered) without breaching the
    # anchor's own low -- must NOT produce an entry.
    check_time = info["flush_time"] + timedelta(minutes=1)
    signal = strategy.on_bar(bar_at(check_time, 109.5, 109.6, 106.2, 106.3))

    assert signal is None
    assert strategy.state is State.WAIT_1M_FVG
    assert strategy.stats["nested_1m_fvgs"] == 0


def test_anchor_updates_to_a_fresher_nearer_15m_fvg_without_mitigation():
    """If no nested 1m FVG ever forms inside the first 15m anchor, but a
    second, later 15m FVG forms further along the same move (nearer to
    current price), the bot switches to it -- without the first anchor
    ever being mitigated. This is what keeps the bot from getting stuck
    on the very first anchor of the session for the rest of the trading
    window."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    first_low, first_high = feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    assert strategy._anchor_fvg.gap_low == pytest.approx(first_low)

    # A second 15m FVG forms further along the same LONG move, well above
    # (and never dipping back into) the first anchor -- it was never
    # mitigated, it's just superseded by something more current. Must
    # land on a 15-minute-aligned start, like every other bucket boundary
    # in these tests, or the dense c0/c1/c2 bars split across buckets.
    second_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=60)
    second_low, second_high = feed_large_15m_fvg(strategy, second_start, quiet_price=109.6, c1_end=113.3)

    assert second_low > first_high  # a distinct, higher zone -- first anchor untouched
    assert strategy._anchor_fvg.gap_low == pytest.approx(second_low)
    assert strategy.state is State.WAIT_1M_FVG


def test_anchor_persists_even_after_price_trades_through_it():
    """The 15m anchor is a fixed reference zone once picked -- unlike the
    nested 1m FVG (which does get abandoned if mitigated), the anchor
    itself is never re-evaluated or abandoned just because price later
    trades through its far side. A nested 1m FVG that forms afterward,
    still inside the original anchor's range, is used normally."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, _ = feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    assert strategy.state is State.WAIT_1M_FVG

    # Price trades straight through the anchor's low -- would have
    # abandoned the anchor under the old design; now it's just ignored.
    poke_time = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    signal = strategy.on_bar(bar_at(poke_time, anchor_low, anchor_low + 0.1, anchor_low - 1.0, anchor_low - 0.5))
    assert signal is None
    assert strategy.state is State.WAIT_1M_FVG

    # A nested 1m FVG still inside the same (unchanged) anchor forms
    # afterward and fills normally.
    nested_start = poke_time + timedelta(minutes=1)
    nested_low, nested_high = feed_nested_1m_fvg(strategy, nested_start)
    midpoint = (nested_low + nested_high) / 2
    fill_time = nested_start + timedelta(minutes=9)
    signal = strategy.on_bar(bar_at(fill_time, 107.5, 107.6, 106.3, 106.8))

    assert signal is not None
    assert signal.entry_price == midpoint


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

    # Jump straight to a bar past the no-new-entries cutoff (12:30 ET default).
    late_bar = bar_at(DAY.replace(hour=12, minute=35), 100.0, 100.5, 99.5, 100.0)
    signal = strategy.on_bar(late_bar)

    assert signal is None
    assert strategy.state is State.DONE_FOR_DAY
