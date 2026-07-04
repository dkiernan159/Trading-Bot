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

WICK = 0.2  # wide enough that a smooth 1-minute walk never forms its own 1-minute-scale gap


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
    3 consecutive bars ever form their own 1-minute gap. Both the 15m and
    5m detectors aggregate these into their own candles; deliberately
    building a 3-candle displacement pattern out of consecutive smooth
    segments at whichever timeframe is wanted is how these tests form a
    genuine (non-contaminating) FVG at that scale."""
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
    # Smaller lookback so the nested 5m FVG's baseline doesn't need 8
    # candles of setup for every test.
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
    assert strategy.state is State.WAIT_5M_FVG

    return gap_low, gap_high


def feed_5m_fvg_pattern(
    strategy: OpeningRangeStrategy, start: datetime, quiet_price: float = 106.0, c1_end: float = 108.1
) -> tuple[float, float]:
    """Feeds a real, dense 5-minute FVG: three consecutive 5-minute
    candles built from continuous 1-minute bars (quiet -> displacement ->
    quiet), then a flush bar to finalize detection. `start` must land on
    a 5-minute wall-clock boundary or the dense bars split across the
    wrong buckets. Returns the resulting (gap_low, gap_high). Makes no
    assertion about strategy state -- callers decide whether this should
    be picked up as an entry trigger. Default values (106.0, 108.1)
    produce a 106.3-107.9 gap, comfortably nested inside the 105.3-109.1
    anchor used throughout these tests; pass different values to place
    the gap elsewhere (e.g. partly outside the anchor's edges)."""
    c0_bars = smooth_walk_1m(start, 5, quiet_price, quiet_price + 0.1)
    c1_bars = smooth_walk_1m(start + timedelta(minutes=5), 5, quiet_price + 0.1, c1_end)
    c2_bars = smooth_walk_1m(start + timedelta(minutes=10), 5, c1_end, c1_end + 0.2)
    for b in c0_bars + c1_bars + c2_bars:
        signal = strategy.on_bar(b)
        assert signal is None

    gap_low = max(b.high for b in c0_bars)
    gap_high = min(b.low for b in c2_bars)

    flush_time = start + timedelta(minutes=15)
    signal = strategy.on_bar(flat_bar(flush_time, c1_end + 0.2))  # finalizes c2's 5m candle, detects the gap
    assert signal is None

    return gap_low, gap_high


def feed_nested_5m_fvg(strategy: OpeningRangeStrategy, start: datetime) -> tuple[float, float]:
    """Same as feed_5m_fvg_pattern, but asserts it was actually picked up
    as the pending entry trigger (used by tests where that's expected)."""
    gap_low, gap_high = feed_5m_fvg_pattern(strategy, start)
    assert strategy.state is State.WAIT_FILL
    return gap_low, gap_high


def test_full_breakout_15m_anchor_then_nested_5m_entry():
    """Breakout sets direction, a large 15m FVG anchors the move, a small
    5m FVG nested inside that anchor is the actual entry trigger (kept
    tighter than the anchor so the stop isn't sized off 15m-candle
    noise)."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    anchor_low, anchor_high = feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    assert anchor_low < anchor_high

    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=50)
    nested_low, nested_high = feed_nested_5m_fvg(strategy, nested_start)

    # The nested 5m gap is fully inside the 15m anchor.
    assert anchor_low <= nested_low
    assert nested_high <= anchor_high

    midpoint = (nested_low + nested_high) / 2
    fill_time = nested_start + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, 108.0, 108.1, 106.3, 106.8))  # retrace fills the midpoint

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == midpoint
    assert strategy.state is State.IN_TRADE


def test_nested_only_requires_midpoint_inside_anchor_not_full_containment():
    """A 5m FVG doesn't need to fit entirely inside the anchor's range --
    only its midpoint (the actual entry price) does. Requiring the whole
    gap to fit inside a tight anchor left very little room and was
    starving the bot of entries."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))  # anchor 105.3-109.1
    assert strategy._anchor_fvg.gap_low == pytest.approx(105.3)

    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=50)
    # This 5m FVG's low edge (104.9) falls OUTSIDE the anchor's low (105.3),
    # but its midpoint (105.7) is still inside the anchor's range.
    gap_low, gap_high = feed_5m_fvg_pattern(strategy, nested_start, quiet_price=104.6, c1_end=106.7)

    assert gap_low == pytest.approx(104.9)
    assert gap_high == pytest.approx(106.5)
    assert strategy.state is State.WAIT_FILL
    assert strategy._pending_fvg.gap_low == pytest.approx(104.9)
    assert strategy._pending_limit_price == pytest.approx(105.7)


def test_ignores_a_5m_fvg_embedded_in_the_anchors_own_displacement():
    """The 15m anchor's own smooth displacement leg, when the entry
    detector chops it into 5-minute buckets, can itself look like a
    qualifying 5m FVG -- it's naturally embedded in the very same move
    that built the anchor, not a separate, later retracement (real price
    action inside a 15-minute displacement candle isn't flat, so this
    happens routinely, not just in contrived cases). Since every one of
    these embedded candidates formed *before* the anchor locked in, none
    of them may be used as the entry trigger, even though they're sitting
    right there in the pool, nested inside the anchor's own range and
    never mitigated. Otherwise the bot would claim a stale gap the
    instant the anchor confirms, which looks like entering as the anchor
    forms rather than waiting for a genuine retest afterward."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    locked_in_at = strategy._anchor_locked_in_at

    embedded = strategy.fvg_detector_5m.unmitigated_in_direction(Direction.LONG)
    assert embedded != []  # the smooth ramp really does leave 5m-scale gaps behind
    assert all(fvg.formed_at <= locked_in_at for fvg in embedded)  # all pre-date the anchor

    # The very next bar after the anchor confirms must not produce an
    # entry, despite these embedded 5m candidates already sitting in the
    # pool, nested inside the anchor and unmitigated.
    check_time = locked_in_at + timedelta(minutes=1)
    signal = strategy.on_bar(bar_at(check_time, 109.5, 109.6, 109.0, 109.2))

    assert signal is None
    assert strategy.state is State.WAIT_5M_FVG
    assert strategy.stats["nested_5m_fvgs"] == 0


def test_anchor_updates_to_a_fresher_nearer_15m_fvg_without_mitigation():
    """If no nested 5m FVG ever forms inside the first 15m anchor, but a
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
    assert strategy.state is State.WAIT_5M_FVG


def test_anchor_persists_even_after_price_trades_through_it():
    """The 15m anchor is a fixed reference zone once picked -- unlike the
    nested 5m FVG (which does get abandoned if mitigated), the anchor
    itself is never re-evaluated or abandoned just because price later
    trades through its far side. A nested 5m FVG that forms afterward,
    still inside the original anchor's range, is used normally."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, _ = feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    assert strategy.state is State.WAIT_5M_FVG

    # Price trades straight through the anchor's low -- would have
    # abandoned the anchor under the old design; now it's just ignored.
    poke_time = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    signal = strategy.on_bar(bar_at(poke_time, anchor_low, anchor_low + 0.1, anchor_low - 1.0, anchor_low - 0.5))
    assert signal is None
    assert strategy.state is State.WAIT_5M_FVG

    # A nested 5m FVG still inside the same (unchanged) anchor forms
    # afterward and fills normally. Needs to land on a 5-minute-aligned
    # start, like every other dense pattern in these tests.
    nested_start = poke_time + timedelta(minutes=4)
    nested_low, nested_high = feed_nested_5m_fvg(strategy, nested_start)
    midpoint = (nested_low + nested_high) / 2
    fill_time = nested_start + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, 108.0, 108.1, 106.3, 106.8))

    assert signal is not None
    assert signal.entry_price == midpoint


def test_still_fills_even_when_the_bar_also_breaks_the_nested_fvgs_far_edge():
    """A resting limit order at the nested FVG's midpoint fills the
    instant price reaches it -- even if the same bar's range continues on
    to also break the gap's far edge. The midpoint sits strictly between
    the two edges, so price can never break the far edge without having
    already reached the midpoint first; there's no such thing as this
    pending entry getting "mitigated before it could fill"."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))

    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=50)
    nested_low, nested_high = feed_nested_5m_fvg(strategy, nested_start)
    midpoint = (nested_low + nested_high) / 2

    # This bar's low breaks straight through the nested gap's far (low)
    # edge -- well past the midpoint it necessarily crossed on the way
    # down.
    fill_time = nested_start + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, 108.0, 108.1, nested_low - 0.2, nested_low - 0.1))

    assert signal is not None
    assert signal.entry_price == midpoint
    assert strategy.state is State.IN_TRADE
    assert strategy.stats["fills"] == 1


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))
    nested_start = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=50)
    nested_low, nested_high = feed_nested_5m_fvg(strategy, nested_start)
    midpoint = (nested_low + nested_high) / 2
    fill_time = nested_start + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, 108.0, 108.1, 106.3, 106.8))
    assert signal is not None
    assert signal.entry_price == midpoint

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar_at(fill_time + timedelta(minutes=1), 106.8, 106.8, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_15M_FVG


def test_breakout_invalidated_when_price_closes_back_through_opposite_box_edge():
    """If price fully reverses -- closing back through the box's *opposite*
    edge -- while still waiting on an anchor/entry, the original breakout
    call is invalid and the state machine resets to WAIT_BREAKOUT instead
    of continuing to hunt for a same-direction anchor/entry somewhere price
    has already reversed away from. This is the bug behind a real live LONG
    entry found 133.5 points below the box low, well after the breakout
    had fully round-tripped and reversed."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)  # LONG breakout; box.low=99.5, box.high=101.0
    feed_large_15m_fvg(strategy, DAY + timedelta(minutes=45))  # anchors -> WAIT_5M_FVG
    assert strategy._anchor_fvg is not None

    reversal_time = DAY + timedelta(minutes=45) + timedelta(minutes=15 * 8) + timedelta(minutes=46)
    # Price fully reverses, closing back below the box's low (99.5) --
    # the opposite edge from the LONG breakout -- before any nested 5m
    # retest ever fires.
    signal = strategy.on_bar(bar_at(reversal_time, 100.0, 100.0, 98.0, 99.0))
    assert signal is None
    assert strategy.stats["breakouts_invalidated"] == 1
    assert strategy.state is State.WAIT_BREAKOUT
    assert strategy._breakout_direction is None
    assert strategy._anchor_fvg is None
    assert strategy._pending_fvg is None

    # A fresh breakout -- even in the opposite (SHORT) direction -- is
    # still detected normally afterward.
    signal = strategy.on_bar(bar_at(reversal_time + timedelta(minutes=1), 99.0, 99.0, 95.0, 95.0))
    assert signal is None
    assert strategy.state is State.WAIT_15M_FVG
    assert strategy._breakout_direction is Direction.SHORT


def test_stale_fvg_from_a_previous_day_is_not_available_as_todays_anchor():
    """A 15m FVG that formed on a previous trading day and was simply
    never revisited (so it's still technically unmitigated) must not be
    available as an anchor on a later day. Otherwise results depend on
    how far back the fed bar history happens to start -- a real bug
    found by comparing a 7-day and 30-day backtest that disagreed on the
    exact same calendar day."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)

    # Day 1 (July 5): forms a large bullish 15m FVG well *below* where
    # day 2's box/breakout will trade (90-94, vs. day 2's ~99.5-103) --
    # never touched again, so it stays genuinely unmitigated (not just
    # coincidentally pruned when day 2's lower prices are fed) all the
    # way to the check below.
    stale_start = PREV_DAY_BASE + timedelta(hours=2)
    feed_quiet_15m(strategy, stale_start, 8, 90.0)
    pattern_start = stale_start + timedelta(minutes=15 * 8)
    c0_bars = smooth_walk_1m(pattern_start, 15, 90.0, 90.1)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=15), 15, 90.1, 94.3)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=30), 15, 94.3, 94.5)
    for b in c0_bars + c1_bars + c2_bars:
        strategy.on_bar(b)
    strategy.on_bar(flat_bar(pattern_start + timedelta(minutes=45), 94.5))  # finalizes/detects the stale FVG

    assert strategy.fvg_detector_15m.unmitigated_in_direction(Direction.LONG) != []

    # Day 2 (July 6, "DAY"): normal box + LONG breakout. The stale July-5
    # gap must not be picked up as today's anchor.
    feed_box_and_breakout(strategy)

    # One more bar for the WAIT_15M_FVG candidate check to actually run
    # (the breakout bar itself only sets the state; it doesn't fall
    # through to check for an anchor in the same bar).
    signal = strategy.on_bar(bar_at(DAY + timedelta(minutes=45), 102.5, 102.8, 102.3, 102.6))

    assert signal is None
    assert strategy.state is State.WAIT_15M_FVG  # not WAIT_5M_FVG -- no anchor yet
    assert strategy._anchor_fvg is None


def test_stands_down_for_day_after_cutoff():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    strategy = OpeningRangeStrategy(cfg)

    # Jump straight to a bar past the no-new-entries cutoff (12:30 ET default).
    late_bar = bar_at(DAY.replace(hour=12, minute=35), 100.0, 100.5, 99.5, 100.0)
    signal = strategy.on_bar(late_bar)

    assert signal is None
    assert strategy.state is State.DONE_FOR_DAY
