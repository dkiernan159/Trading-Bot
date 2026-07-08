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
    """A single bar standing in for a whole quiet, unchanging 5-minute
    candle -- safe to use sparsely (one bar per 5-minute mark) because a
    repeated, unchanging value can never itself form a gap, so it doesn't
    also register as a false FVG."""
    return bar_at(dt, price, price + spread / 2, price - spread / 2, price)


def smooth_walk_1m(start: datetime, minutes: int, start_price: float, end_price: float) -> list[Bar]:
    """`minutes` consecutive real 1-minute bars walking smoothly from
    start_price to end_price. The slope is gentle relative to WICK so no
    3 consecutive bars ever form their own 1-minute gap -- this lets the
    same price action build a genuine, non-contaminating 5m candle (the
    5m detector aggregates these into one candle)."""
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
    # real 13:30 ET cutoff -- push it out so the timing under test isn't
    # the no-new-entries cutoff (covered separately, see
    # test_stands_down_for_day_after_cutoff).
    cfg.session.no_new_entries_after = dtime(23, 59)
    # These fixtures' fixed box/previous-day levels routinely land within
    # the real config's 20-point minimum stop distance -- zero it out so
    # tests not specifically about that gate (covered separately, see
    # test_anchor_rejected_and_a_fresh_one_is_hunted_when_no_real_level_is_within_budget
    # and test_anchor_rejected_when_the_only_real_level_is_too_close) aren't
    # incidentally exercising it too.
    cfg.strategy.min_stop_dollars = 0.0
    # All the existing gap-math assertions in this file were written
    # against the exact midpoint (entry_retracement_pct 0.5) -- keep that
    # here so this real config's default (loosened for more fills, see
    # config.yaml) doesn't change unrelated tests. The retracement
    # fraction itself is covered separately, see
    # test_entry_fills_at_a_shallower_retracement_than_the_midpoint.
    cfg.strategy.entry_retracement_pct = 0.5
    return cfg


def feed_previous_day_levels(strategy: OpeningRangeStrategy):
    """Marks a previous-day high of 105 and low of 95 -- dashboard/chart
    display only now (see risk.py's find_structural_stop_price), no
    longer used for stop placement."""
    strategy.on_bar(bar_at(PREV_DAY_BASE, 100.0, 101.0, 99.0, 100.0))
    strategy.on_bar(bar_at(PREV_DAY_BASE + timedelta(minutes=15), 100.0, 105.0, 100.0, 104.0))
    strategy.on_bar(bar_at(PREV_DAY_BASE + timedelta(minutes=30), 100.0, 101.0, 95.0, 98.0))


def feed_premarket_swing_low(strategy: OpeningRangeStrategy, swing_low: float = 90.0) -> float:
    """Feeds a short, genuine 1-minute price dip well before 9:30 ET on
    DAY itself (MARKING_LEVELS doesn't gate swing_tracker.add_bar -- see
    on_bar), forming a real confirmed break-of-structure swing low at
    exactly `swing_low` (see swing_points.py) -- the fallback stop
    candidate the new stop rule (risk.py's find_structural_stop_price)
    uses when no strong 5m FVG qualifies. Deliberately NOT built from an
    FVG-style displacement (each leg here is far under min_gap_points, and
    only spans one 5-minute bucket, so no 5m or 1m FVG forms) -- so unlike
    a real support-zone FVG, this can never accidentally also get picked
    up as a candidate *anchor* (which only searches fvg_detector_5m/1m's
    pools, never the swing tracker), which would otherwise contaminate
    these tests' anchor_history/state assertions with an extra premature
    pick-and-supersede cycle."""
    start = DAY - timedelta(hours=2)
    lows = [swing_low + 3.0, swing_low + 1.5, swing_low, swing_low + 1.5, swing_low + 3.0]
    for i, low in enumerate(lows):
        strategy.on_bar(bar_at(start + timedelta(minutes=i), low + 0.3, low + 0.6, low, low + 0.3))
    return swing_low


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
    assert strategy.state is State.WAIT_5M_FVG


def feed_quiet_5m(strategy: OpeningRangeStrategy, start: datetime, count: int, price: float):
    for i in range(count):
        signal = strategy.on_bar(flat_bar(start + timedelta(minutes=5 * i), price))
        assert signal is None


def feed_large_5m_fvg(
    strategy: OpeningRangeStrategy, start: datetime, quiet_price: float = 105.0, c1_end: float = 109.3
) -> tuple[float, float]:
    """Feeds 8 quiet 5m baseline candles at quiet_price, then a real
    (dense, 1-minute resolution) displacement move from quiet_price+0.1 up
    to c1_end that forms a large bullish 5m FVG, then a flush bar to
    finalize detection. Returns the resulting (gap_low, gap_high) -- its
    own midpoint is now the entry trigger, so the strategy lands in
    WAIT_FILL once this returns. Default values (105.0, 109.3) produce
    the same 105.3-109.1 gap used throughout these tests; pass different
    values to build a second, distinct anchor elsewhere on the chart.
    `start` must land on a 5-minute wall-clock boundary, like every other
    bucket boundary in these tests, or the dense c0/c1/c2 bars split
    across the wrong buckets."""
    feed_quiet_5m(strategy, start, 8, quiet_price)
    pattern_start = start + timedelta(minutes=5 * 8)

    c0_bars = smooth_walk_1m(pattern_start, 5, quiet_price, quiet_price + 0.1)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=5), 5, quiet_price + 0.1, c1_end)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=10), 5, c1_end, c1_end + 0.2)
    for b in c0_bars + c1_bars + c2_bars:
        signal = strategy.on_bar(b)
        assert signal is None

    gap_low = max(b.high for b in c0_bars)
    gap_high = min(b.low for b in c2_bars)

    flush_time = pattern_start + timedelta(minutes=15)
    signal = strategy.on_bar(flat_bar(flush_time, c1_end + 0.2))  # finalizes c2's 5m candle, detects the FVG
    assert signal is None
    assert strategy.state is State.WAIT_FILL
    assert strategy._pending_limit_price == pytest.approx((gap_low + gap_high) / 2)

    return gap_low, gap_high


def test_full_breakout_then_fill_at_the_anchors_own_midpoint():
    """Breakout sets direction, a large 5m FVG anchors the move, and its
    own midpoint (not a further nested structure) is the entry trigger --
    a limit order rests there and fills the instant price retraces back
    to it."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)

    anchor_low, anchor_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    midpoint = (anchor_low + anchor_high) / 2

    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low, anchor_low + 0.1))

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == pytest.approx(midpoint)
    assert signal.anchor_fvg.gap_low == pytest.approx(anchor_low)
    assert strategy.state is State.IN_TRADE
    assert strategy.stats["fills"] == 1

    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "filled"
    assert strategy.anchor_history[0].gap_low == pytest.approx(anchor_low)
    assert strategy.anchor_history[0].ended_at == fill_time


def test_entry_fills_at_a_shallower_retracement_than_the_midpoint():
    """With entry_retracement_pct loosened below 0.5, the resting limit
    price sits closer to the gap's near edge than the exact midpoint --
    scaling with that anchor's own width -- so a shallower retracement
    fills the trade, one that wouldn't have reached the old exact
    midpoint at all. Added after a real 30-day --near-miss backtest
    showed "superseded" (anchors replaced before ever filling) was by far
    the largest bucket -- several anchors sat live for 1-2+ hours before
    being replaced, suggesting price often approached but didn't quite
    reach the exact midpoint."""
    cfg = load_test_config()
    cfg.strategy.entry_retracement_pct = 0.35
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)

    # Built by hand rather than via feed_large_5m_fvg, since that helper
    # asserts the pending price lands at the exact midpoint -- not true
    # once entry_retracement_pct is loosened.
    start = DAY + timedelta(minutes=45)
    quiet_price, c1_end = 105.0, 109.3
    feed_quiet_5m(strategy, start, 8, quiet_price)
    pattern_start = start + timedelta(minutes=5 * 8)
    c0_bars = smooth_walk_1m(pattern_start, 5, quiet_price, quiet_price + 0.1)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=5), 5, quiet_price + 0.1, c1_end)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=10), 5, c1_end, c1_end + 0.2)
    for b in c0_bars + c1_bars + c2_bars:
        assert strategy.on_bar(b) is None
    anchor_low = max(b.high for b in c0_bars)
    anchor_high = min(b.low for b in c2_bars)
    flush_time = pattern_start + timedelta(minutes=15)
    assert strategy.on_bar(flat_bar(flush_time, c1_end + 0.2)) is None
    assert strategy.state is State.WAIT_FILL

    midpoint = (anchor_low + anchor_high) / 2
    width = anchor_high - anchor_low
    shallow_entry = anchor_high - 0.35 * width

    assert shallow_entry > midpoint  # closer to the near/top edge than the exact midpoint
    assert strategy._pending_limit_price == pytest.approx(shallow_entry)

    # This bar retraces down to the shallow entry point but stops well
    # short of the exact midpoint -- would never have filled under the
    # original (0.5) design.
    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(
        bar_at(fill_time, anchor_high, anchor_high + 0.1, shallow_entry - 0.01, shallow_entry)
    )

    assert signal is not None
    assert signal.entry_price == pytest.approx(shallow_entry)
    assert strategy.state is State.IN_TRADE


def test_a_1m_fvg_can_anchor_and_fill_a_trade_on_its_own():
    """Added at the user's explicit request: a real 7-day backtest was
    finding only 2 trades against a hard requirement of >=1/trading day,
    and the funnel showed plenty of 5m anchors forming but few retracing
    all the way back to fill. A 1-minute-timeframe FVG (fvg_detector_1m,
    config.yaml: strategy.entry_fvg) is now an alternative anchor source,
    pooled alongside the 5m one -- not nested inside it, not requiring a
    5m FVG to also exist -- so a real displacement move that only forms a
    1-minute-scale gap can still anchor and fill a trade on its own."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)  # LONG breakout at DAY + 30 minutes

    pattern_start = DAY + timedelta(minutes=31)
    baseline_start = pattern_start
    for i in range(8):
        signal = strategy.on_bar(bar_at(baseline_start + timedelta(minutes=i), 103.0, 103.05, 102.95, 103.0))
        assert signal is None

    c0_time = baseline_start + timedelta(minutes=8)
    signal = strategy.on_bar(bar_at(c0_time, 103.0, 103.05, 102.95, 103.0))
    assert signal is None
    # c1: a much larger displacement than the earlier 15m-nested design
    # ever needed -- entry_fvg.min_gap_points was raised from 0.5 to 12
    # after real data showed every trade with a gap under 14 points lost.
    signal = strategy.on_bar(bar_at(c0_time + timedelta(minutes=1), 103.0, 120.3, 103.0, 120.2))  # c1: displacement
    assert signal is None
    signal = strategy.on_bar(bar_at(c0_time + timedelta(minutes=2), 120.2, 120.5, 120.05, 120.4))  # c2: confirms gap
    assert signal is None
    # One more bar to finalize c2's 1-minute "candle" (each fed bar already
    # is one, at this timeframe) and trigger detection.
    signal = strategy.on_bar(bar_at(c0_time + timedelta(minutes=3), 120.4, 120.45, 120.35, 120.4))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    anchor_low, anchor_high = 103.05, 120.05  # c0.high, c2.low
    midpoint = (anchor_low + anchor_high) / 2
    assert strategy._anchor_fvg.timeframe_minutes == 1
    assert strategy._pending_limit_price == pytest.approx(midpoint)

    fill_time = c0_time + timedelta(minutes=4)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low, anchor_low + 0.05))

    assert signal is not None
    assert signal.entry_price == pytest.approx(midpoint)
    assert signal.anchor_fvg.timeframe_minutes == 1
    assert strategy.state is State.IN_TRADE


def test_anchor_stays_live_and_moves_the_resting_price_while_waiting_to_fill():
    """If price never retraces to the first anchor's midpoint, but a
    second, later 5m FVG forms further along the same move (nearer to
    current price), the bot switches to it -- moving the resting limit
    order to the new anchor's own midpoint -- without the first anchor
    ever being mitigated. This is what keeps the bot from getting stuck
    on the very first anchor of the session for the rest of the trading
    window."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)

    first_low, first_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    assert strategy._anchor_fvg.gap_low == pytest.approx(first_low)
    assert strategy._pending_limit_price == pytest.approx((first_low + first_high) / 2)

    # A second 5m FVG forms further along the same LONG move, well above
    # (and never dipping back into) the first anchor -- it was never
    # mitigated, it's just superseded by something more current. Must
    # land on a 5-minute-aligned start, like every other bucket boundary
    # in these tests, or the dense c0/c1/c2 bars split across buckets.
    second_start = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=20)
    second_low, second_high = feed_large_5m_fvg(strategy, second_start, quiet_price=109.6, c1_end=113.3)

    assert second_low > first_high  # a distinct, higher zone -- first anchor untouched
    assert strategy._anchor_fvg.gap_low == pytest.approx(second_low)
    assert strategy._pending_limit_price == pytest.approx((second_low + second_high) / 2)
    assert strategy.state is State.WAIT_FILL

    # The first anchor is recorded as superseded, not lost silently.
    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "superseded"
    assert strategy.anchor_history[0].gap_low == pytest.approx(first_low)


def test_fills_the_instant_price_reaches_the_midpoint_even_if_the_bar_also_breaks_the_far_edge():
    """A resting limit order at the anchor's own midpoint fills the
    instant price reaches it -- even if the same bar's range continues on
    to also break the anchor's far edge. The midpoint sits strictly
    between the two edges, so price can never break the far edge without
    having already reached the midpoint first; there's no such thing as
    this pending entry getting "mitigated before it could fill" (nor is
    there a separate "the anchor got mitigated while we waited" outcome
    anymore -- reaching the far edge and filling are the same event)."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    midpoint = (anchor_low + anchor_high) / 2

    # This bar's low breaks straight through the anchor's far (low) edge
    # -- well past the midpoint it necessarily crossed on the way down.
    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low - 0.5, anchor_low - 0.3))

    assert signal is not None
    assert signal.entry_price == pytest.approx(midpoint)
    assert strategy.state is State.IN_TRADE
    assert strategy.stats["fills"] == 1


def test_reenters_after_stop_out_when_setup_reforms():
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    midpoint = (anchor_low + anchor_high) / 2

    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low, anchor_low + 0.1))
    assert signal is not None
    assert signal.entry_price == pytest.approx(midpoint)

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_BREAKOUT

    # A new breakout forms below the box low (99.5) -- setup reforms as SHORT.
    signal = strategy.on_bar(bar_at(fill_time + timedelta(minutes=1), 106.8, 106.8, 99.0, 99.0))
    assert signal is None
    assert strategy.state is State.WAIT_5M_FVG


def test_notify_entry_not_filled_keeps_hunting_within_the_same_breakout():
    """Live trading only: a resting limit order can fail to actually fill
    even after on_bar already committed to IN_TRADE (see
    notify_entry_not_filled's own docstring). Unlike a real stop-out, this
    must not reset all the way back to WAIT_BREAKOUT -- the breakout
    thesis itself was never invalidated, only this one entry attempt
    didn't happen -- and a fresh anchor must still be found and filled
    afterward within that same breakout."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    midpoint = (anchor_low + anchor_high) / 2

    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low, anchor_low + 0.1))
    assert signal is not None
    assert signal.entry_price == pytest.approx(midpoint)
    assert strategy.state is State.IN_TRADE

    strategy.notify_entry_not_filled()
    assert strategy.state is State.WAIT_5M_FVG
    assert strategy._breakout_direction is Direction.LONG  # not reset -- same breakout thesis

    # A second, distinct anchor further along the same LONG move -- no new
    # breakout needed -- still gets found and can still fill normally.
    second_start = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=20)
    second_low, second_high = feed_large_5m_fvg(strategy, second_start, quiet_price=109.6, c1_end=113.3)
    second_midpoint = (second_low + second_high) / 2

    second_fill_time = second_start + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(
        bar_at(second_fill_time, second_high, second_high + 0.1, second_low, second_low + 0.1)
    )

    assert signal is not None
    assert signal.entry_price == pytest.approx(second_midpoint)
    assert strategy.state is State.IN_TRADE


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
    feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))  # anchors -> WAIT_FILL
    assert strategy._anchor_fvg is not None

    reversal_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    # Price fully reverses, closing back below the box's low (99.5) --
    # the opposite edge from the LONG breakout -- without ever retracing
    # up to the anchor's own midpoint first.
    signal = strategy.on_bar(bar_at(reversal_time, 100.0, 100.0, 98.0, 99.0))
    assert signal is None
    assert strategy.stats["breakouts_invalidated"] == 1
    assert strategy.state is State.WAIT_BREAKOUT
    assert strategy._breakout_direction is None
    assert strategy._anchor_fvg is None
    assert strategy._pending_limit_price is None

    # A fresh breakout -- even in the opposite (SHORT) direction -- is
    # still detected normally afterward.
    signal = strategy.on_bar(bar_at(reversal_time + timedelta(minutes=1), 99.0, 99.0, 95.0, 95.0))
    assert signal is None
    assert strategy.state is State.WAIT_5M_FVG
    assert strategy._breakout_direction is Direction.SHORT

    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "invalidated"
    assert strategy.anchor_history[0].ended_at == reversal_time


def test_anchor_recorded_as_session_ended_when_cutoff_hits_before_it_fills():
    """An anchor that's still live (never filled, never superseded, never
    invalidated) when the no-new-entries cutoff arrives shows up in the
    history as "session_ended" -- ran out of real time to retrace, not
    abandoned for any other reason."""
    cfg = load_test_config()
    cfg.session.no_new_entries_after = dtime(13, 30)
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    assert strategy.state is State.WAIT_FILL

    # Price never comes back down to the midpoint; the cutoff arrives first.
    late_bar = bar_at(DAY.replace(hour=13, minute=35), anchor_high, anchor_high + 0.5, anchor_high - 0.1, anchor_high)
    signal = strategy.on_bar(late_bar)

    assert signal is None
    assert strategy.state is State.DONE_FOR_DAY
    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "session_ended"
    assert strategy.anchor_history[0].gap_low == pytest.approx(anchor_low)
    assert strategy.anchor_history[0].ended_at == late_bar.timestamp

    # The cutoff close must actually clear the anchor -- otherwise it sits
    # around stale and gets silently re-recorded by _start_new_day's
    # defensive close on the next day, doubling up this same anchor in
    # the history with an inflated (real bug: sometimes multi-day)
    # duration. A real 30-day --near-miss backtest showed exactly this:
    # every single "session_ended" anchor appeared twice.
    next_day_bar = bar_at(late_bar.timestamp + timedelta(days=1), anchor_high, anchor_high, anchor_high, anchor_high)
    signal = strategy.on_bar(next_day_bar)
    assert signal is None
    assert len(strategy.anchor_history) == 1  # not re-recorded


def test_stale_fvg_from_a_previous_day_is_not_available_as_todays_anchor():
    """A 5m FVG that formed on a previous trading day and was simply
    never revisited (so it's still technically unmitigated) must not be
    available as an anchor on a later day. Otherwise results depend on
    how far back the fed bar history happens to start -- a real bug
    found by comparing a 7-day and 30-day backtest that disagreed on the
    exact same calendar day."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)

    # Day 1 (July 5): forms a large bullish 5m FVG well *below* where
    # day 2's box/breakout will trade (90-94, vs. day 2's ~99.5-103) --
    # never touched again, so it stays genuinely unmitigated (not just
    # coincidentally pruned when day 2's lower prices are fed) all the
    # way to the check below.
    stale_start = PREV_DAY_BASE + timedelta(hours=2)
    feed_quiet_5m(strategy, stale_start, 8, 90.0)
    pattern_start = stale_start + timedelta(minutes=5 * 8)
    c0_bars = smooth_walk_1m(pattern_start, 5, 90.0, 90.1)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=5), 5, 90.1, 94.3)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=10), 5, 94.3, 94.5)
    for b in c0_bars + c1_bars + c2_bars:
        strategy.on_bar(b)
    strategy.on_bar(flat_bar(pattern_start + timedelta(minutes=15), 94.5))  # finalizes/detects the stale FVG

    assert strategy.fvg_detector_5m.unmitigated_in_direction(Direction.LONG) != []

    # Day 2 (July 6, "DAY"): normal box + LONG breakout. The stale July-5
    # gap must not be picked up as today's anchor.
    feed_box_and_breakout(strategy)

    # One more bar for the WAIT_5M_FVG candidate check to actually run
    # (the breakout bar itself only sets the state; it doesn't fall
    # through to check for an anchor in the same bar).
    signal = strategy.on_bar(bar_at(DAY + timedelta(minutes=45), 102.5, 102.8, 102.3, 102.6))

    assert signal is None
    assert strategy.state is State.WAIT_5M_FVG  # not WAIT_FILL -- no anchor yet
    assert strategy._anchor_fvg is None


def test_anchor_rejected_and_a_fresh_one_is_hunted_when_no_real_level_is_within_budget():
    """If price retraces to an anchor's midpoint but no strong 5m FVG or
    1m break-of-structure swing point sits within the $200 stop budget
    beyond it (see risk.py's find_structural_stop_price), the trade is
    skipped -- recorded as "no_valid_stop" -- instead of defaulting to an
    arbitrary max-risk stop with nothing real behind it. The rejected
    anchor isn't silently re-offered forever, and the bot keeps hunting: a
    different anchor that *does* sit near a real stop candidate fills
    normally."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    # The only real stop candidate in these fixtures (a swing low at 90) --
    # comfortably within budget for the second anchor below, but still
    # 100+ points away from the first (too far, deliberately).
    feed_premarket_swing_low(strategy)
    feed_box_and_breakout(strategy)

    # This anchor's midpoint (~212.2) sits well over 100 points above the
    # only real stop candidate in these fixtures (the swing low at 90) --
    # past the $200/100-point cap.
    first_low, first_high = feed_large_5m_fvg(
        strategy, DAY + timedelta(minutes=45), quiet_price=210.0, c1_end=214.3
    )

    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, first_high, first_high + 0.1, first_low, first_low + 0.1))

    assert signal is None  # rejected, not entered
    assert strategy.state is State.WAIT_5M_FVG
    assert strategy._anchor_fvg is None
    assert strategy.stats["fills"] == 0
    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "no_valid_stop"
    assert strategy.anchor_history[0].gap_low == pytest.approx(first_low)

    # The rejected anchor is still sitting, unmitigated, in the detector's
    # pool -- but it must not be re-offered as a candidate just because
    # nothing fresher has formed yet.
    signal_again = strategy.on_bar(bar_at(fill_time + timedelta(minutes=1), first_high, first_high, first_high, first_high))
    assert signal_again is None
    assert strategy.state is State.WAIT_5M_FVG
    assert len(strategy.anchor_history) == 1  # not re-rejected

    # A different anchor, close enough to the swing low at 90 to have a
    # valid stop, forms next and fills normally.
    second_start = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=20)
    second_low, second_high = feed_large_5m_fvg(strategy, second_start, quiet_price=100.0, c1_end=104.3)
    second_midpoint = (second_low + second_high) / 2

    second_fill_time = second_start + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(
        bar_at(second_fill_time, second_high, second_high + 0.1, second_low, second_low + 0.1)
    )

    assert signal is not None
    assert signal.entry_price == pytest.approx(second_midpoint)
    assert strategy.state is State.IN_TRADE
    assert strategy.stats["fills"] == 1
    assert len(strategy.anchor_history) == 2
    assert strategy.anchor_history[1].outcome == "filled"
    assert strategy.anchor_history[1].gap_low == pytest.approx(second_low)


def test_anchor_rejected_when_the_only_real_level_is_too_close():
    """The mirror image of the too-far case: a real 7-day backtest showed
    stops under ~20 points losing 6 of 7 times. Below that floor
    (min_stop_dollars), a trade is skipped just like it would be above the
    cap -- there's no real invalidation point behind a stop that tight,
    only ordinary chop."""
    cfg = load_test_config()
    cfg.strategy.min_stop_dollars = 40.0  # $40 / (point_value 2.0 * 1 contract) = 20-point floor
    strategy = OpeningRangeStrategy(cfg)

    feed_previous_day_levels(strategy)
    # A real swing low at 100, but too close (7.2 points) to the anchor's
    # own midpoint (107.2) below to clear the 20-point floor.
    feed_premarket_swing_low(strategy, swing_low=100.0)
    feed_box_and_breakout(strategy)

    first_low, first_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))

    fill_time = DAY + timedelta(minutes=45) + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, first_high, first_high + 0.1, first_low, first_low + 0.1))

    assert signal is None
    assert strategy.state is State.WAIT_5M_FVG
    assert strategy.stats["fills"] == 0
    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "no_valid_stop"


def test_stands_down_for_day_after_cutoff():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    strategy = OpeningRangeStrategy(cfg)

    # Jump straight to a bar past the no-new-entries cutoff (13:30 ET default).
    late_bar = bar_at(DAY.replace(hour=13, minute=35), 100.0, 100.5, 99.5, 100.0)
    signal = strategy.on_bar(late_bar)

    assert signal is None
    assert strategy.state is State.DONE_FOR_DAY


def test_status_snapshot_reflects_current_hunt_state():
    """Dashboard-only diagnostic (src/runner.py writes this out after every
    bar) -- must reflect the box/anchor/pending-entry state accurately at
    each stage, not just at rest."""
    cfg = load_test_config()
    strategy = OpeningRangeStrategy(cfg)

    idle = strategy.status_snapshot()
    assert idle["state"] == "MARKING_LEVELS"
    assert idle["direction"] is None
    assert idle["box_high"] is None
    assert idle["anchor_gap_low"] is None
    assert idle["pending_limit_price"] is None

    feed_previous_day_levels(strategy)
    feed_box_and_breakout(strategy)
    mid_hunt = strategy.status_snapshot()
    assert mid_hunt["state"] == "WAIT_5M_FVG"
    assert mid_hunt["direction"] == "long"
    assert mid_hunt["box_high"] is not None

    anchor_low, anchor_high = feed_large_5m_fvg(strategy, DAY + timedelta(minutes=45))
    waiting_fill = strategy.status_snapshot()
    assert waiting_fill["state"] == "WAIT_FILL"
    assert waiting_fill["anchor_gap_low"] == pytest.approx(anchor_low)
    assert waiting_fill["anchor_gap_high"] == pytest.approx(anchor_high)
    assert waiting_fill["pending_limit_price"] == pytest.approx((anchor_low + anchor_high) / 2)
