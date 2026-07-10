from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import load_config
from src.fvg import FairValueGap
from src.models import Bar, Direction
from src.overnight_strategy import OvernightMomentumStrategy, State

TZ = ZoneInfo("America/New_York")
# 19:00 ET on a Sunday evening -- t >= asia_start, so this whole overnight
# window's marked levels/trading_date is the *next* day, 2026-07-06.
NIGHT_START = datetime(2026, 7, 5, 19, 0, tzinfo=TZ)

WICK = 0.2  # wide enough that a smooth 1-minute walk never forms its own 1-minute-scale gap


def load_test_config():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    # Same rationale as tests/test_strategy.py's load_test_config: keep these
    # synthetic fixtures from incidentally tripping the (shared) $40 stop
    # floor, which isn't what these tests are about.
    cfg.strategy.min_stop_dollars = 0.0
    cfg.strategy.entry_retracement_pct = 0.5
    return cfg


def bar_at(dt: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=dt, open=o, high=h, low=l, close=c)


def flat_bar(dt: datetime, price: float, spread: float = 0.5) -> Bar:
    return bar_at(dt, price, price + spread / 2, price - spread / 2, price)


def smooth_walk_1m(start: datetime, minutes: int, start_price: float, end_price: float) -> list[Bar]:
    """Same helper as tests/test_strategy.py -- `minutes` consecutive real
    1-minute bars walking smoothly from start_price to end_price, gentle
    enough relative to WICK that no 3 consecutive bars form their own gap."""
    bars = []
    for i in range(minutes):
        o = start_price + (end_price - start_price) * i / minutes
        c = start_price + (end_price - start_price) * (i + 1) / minutes
        h, l = (o, c) if o >= c else (c, o)
        bars.append(bar_at(start + timedelta(minutes=i), o, h + WICK, l - WICK, c))
    return bars


def feed_swing_low_after_window_opens(
    strategy: OvernightMomentumStrategy, night_start: datetime, swing_low: float = 90.0
) -> datetime:
    """Assumes the window-opening bar has already been fed at night_start
    (so _start_new_night already ran) -- feeds 5 more real 1-minute bars
    right after it, forming a genuine confirmed break-of-structure swing
    low at exactly `swing_low` (see swing_points.py), the LONG-trade stop
    fallback risk.py's find_structural_stop_price uses when no strong 5m
    FVG qualifies. Deliberately NOT built from an FVG-style displacement
    (each leg is far under min_gap_points, and spans only one 5-minute
    bucket) -- so unlike a real support-zone FVG, this can never
    accidentally also get picked up as a candidate *anchor* (which only
    searches fvg_detector_5m/1m's pools, never the swing tracker), which
    would otherwise contaminate these tests' anchor_history/state
    assertions with an extra premature pick-and-supersede cycle. Returns a
    5-minute-aligned timestamp right after these bars, to build the actual
    anchor from (feed_large_5m_fvg requires a 5-minute-aligned start, like
    every other bucket boundary in these tests)."""
    start = night_start + timedelta(minutes=5)
    lows = [swing_low + 3.0, swing_low + 1.5, swing_low, swing_low + 1.5, swing_low + 3.0]
    for i, low in enumerate(lows):
        strategy.on_bar(bar_at(start + timedelta(minutes=i), low + 0.3, low + 0.6, low, low + 0.3))
    return night_start + timedelta(minutes=10)


def feed_swing_high_after_window_opens(
    strategy: OvernightMomentumStrategy, night_start: datetime, swing_high: float = 120.0
) -> datetime:
    """Mirror of feed_swing_low_after_window_opens, for the SHORT-trade
    break-of-structure fallback."""
    start = night_start + timedelta(minutes=5)
    highs = [swing_high - 3.0, swing_high - 1.5, swing_high, swing_high - 1.5, swing_high - 3.0]
    for i, high in enumerate(highs):
        strategy.on_bar(bar_at(start + timedelta(minutes=i), high - 0.3, high, high - 0.6, high - 0.3))
    return night_start + timedelta(minutes=10)


def feed_quiet_5m(strategy: OvernightMomentumStrategy, start: datetime, count: int, price: float):
    for i in range(count):
        signal = strategy.on_bar(flat_bar(start + timedelta(minutes=5 * i), price))
        assert signal is None


def feed_large_5m_fvg(
    strategy: OvernightMomentumStrategy, start: datetime, quiet_price: float = 105.0, c1_end: float = 109.3
) -> tuple[float, float]:
    """Same construction as tests/test_strategy.py's own helper of the same
    name -- 8 quiet 5m baseline candles, then a real displacement move that
    forms a large bullish 5m FVG, then a flush bar. Returns (gap_low,
    gap_high); the strategy lands in WAIT_FILL once this returns."""
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
    signal = strategy.on_bar(flat_bar(flush_time, c1_end + 0.2))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    return gap_low, gap_high


def test_outside_the_window_the_strategy_stays_idle():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    midday = datetime(2026, 7, 6, 12, 0, tzinfo=TZ)
    signal = strategy.on_bar(flat_bar(midday, 100.0))
    assert signal is None
    assert strategy.state is State.IDLE
    assert strategy._direction is None


def test_entering_the_window_starts_the_fvg_hunt_directly_no_breakout():
    """No box/breakout step at all -- the window opening is enough to start
    hunting for a large FVG, whichever direction it turns out to be."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    signal = strategy.on_bar(flat_bar(NIGHT_START - timedelta(minutes=1), 100.0))
    assert signal is None
    assert strategy.state is State.IDLE

    signal = strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    assert signal is None
    assert strategy.state is State.WAIT_FVG


def test_full_fvg_then_fill_at_its_own_midpoint_sets_long_direction_directly():
    """A large 5m FVG, with no breakout precondition, both sets LONG
    direction and anchors the move -- its own midpoint is the entry."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    anchor_start = feed_swing_low_after_window_opens(strategy, NIGHT_START)
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, anchor_start)
    midpoint = (anchor_low + anchor_high) / 2
    assert strategy._direction is Direction.LONG
    assert strategy._pending_limit_price == pytest.approx(midpoint)

    fill_time = anchor_start + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low, anchor_low + 0.1))

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == pytest.approx(midpoint)
    assert signal.anchor_fvg.gap_low == pytest.approx(anchor_low)
    assert strategy.state is State.IN_TRADE
    assert strategy.stats["fills"] == 1

    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "filled"


def test_full_fvg_then_fill_sets_short_direction_directly():
    """Same as above but a bearish FVG -- confirms the pooled-both-
    directions search (new in this strategy, since the day strategy never
    needs to search SHORT while breakout direction is fixed LONG) actually
    picks up a SHORT-direction gap correctly."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    strategy.on_bar(flat_bar(NIGHT_START, 105.0))
    anchor_start = feed_swing_high_after_window_opens(strategy, NIGHT_START, swing_high=120.0)
    feed_quiet_5m(strategy, anchor_start, 8, 105.0)
    pattern_start = anchor_start + timedelta(minutes=5 * 8)

    c0_bars = smooth_walk_1m(pattern_start, 5, 105.0, 104.9)
    c1_bars = smooth_walk_1m(pattern_start + timedelta(minutes=5), 5, 104.9, 100.7)
    c2_bars = smooth_walk_1m(pattern_start + timedelta(minutes=10), 5, 100.7, 100.5)
    for b in c0_bars + c1_bars + c2_bars:
        assert strategy.on_bar(b) is None

    gap_low = max(b.high for b in c2_bars)  # SHORT gap: c2's high side is the near edge
    gap_high = min(b.low for b in c0_bars)

    flush_time = pattern_start + timedelta(minutes=15)
    signal = strategy.on_bar(flat_bar(flush_time, 100.5 - 0.2))
    assert signal is None
    assert strategy.state is State.WAIT_FILL
    assert strategy._direction is Direction.SHORT

    midpoint = (gap_low + gap_high) / 2
    assert strategy._pending_limit_price == pytest.approx(midpoint)

    fill_time = flush_time + timedelta(minutes=1)
    signal = strategy.on_bar(bar_at(fill_time, gap_low, gap_high, gap_low - 0.1, gap_high - 0.1))
    assert signal is not None
    assert signal.direction is Direction.SHORT
    assert strategy.state is State.IN_TRADE


def test_a_1m_fvg_can_anchor_and_fill_a_trade_on_its_own():
    """A 1-minute-timeframe FVG (fvg_detector_1m, config.yaml:
    strategy.entry_fvg), pooled alongside the 5m one, can anchor and fill a
    trade without any 5m FVG involved -- mirrors
    tests/test_strategy.py's test of the same name for the day strategy."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 103.0))
    anchor_start = feed_swing_low_after_window_opens(strategy, NIGHT_START)

    baseline_start = anchor_start
    for i in range(8):
        signal = strategy.on_bar(bar_at(baseline_start + timedelta(minutes=i), 103.0, 103.05, 102.95, 103.0))
        assert signal is None

    c0_time = baseline_start + timedelta(minutes=8)
    assert strategy.on_bar(bar_at(c0_time, 103.0, 103.05, 102.95, 103.0)) is None
    # min_gap_points for entry_fvg (1m) is 12 -- a real 25-trade backtest
    # showed every trade with a gap under 14 points lost (see config.yaml).
    assert strategy.on_bar(bar_at(c0_time + timedelta(minutes=1), 103.0, 120.3, 103.0, 120.2)) is None
    assert strategy.on_bar(bar_at(c0_time + timedelta(minutes=2), 120.2, 120.5, 120.05, 120.4)) is None
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
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))

    first_low, first_high = feed_large_5m_fvg(strategy, NIGHT_START)
    assert strategy._anchor_fvg.gap_low == pytest.approx(first_low)

    second_start = NIGHT_START + timedelta(minutes=5 * 8) + timedelta(minutes=20)
    second_low, second_high = feed_large_5m_fvg(strategy, second_start, quiet_price=109.6, c1_end=113.3)

    assert second_low > first_high
    assert strategy._anchor_fvg.gap_low == pytest.approx(second_low)
    assert strategy._pending_limit_price == pytest.approx((second_low + second_high) / 2)
    assert strategy.state is State.WAIT_FILL

    superseded = [a for a in strategy.anchor_history if a.outcome == "superseded"]
    assert len(superseded) == 1
    assert superseded[0].gap_low == pytest.approx(first_low)


def test_anchor_is_abandoned_as_stale_once_price_runs_too_far_without_filling():
    """User's explicit instruction, 2026-07-09: don't "hedge the whole
    night" on the first anchor found -- a real overnight session sat in
    WAIT_FILL for hours while price ran ~52 points past a SHORT anchor's
    own entry with no fresher FVG ever qualifying to supersede it. Once
    price moves more than half the $200 stop budget (50 points, at this
    config's point_value=2.0/contract_size=1) past the pending entry
    without retracing to fill it, the anchor is dropped and the strategy
    goes back to plain hunting instead of waiting on a now-stale level
    indefinitely."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))

    feed_large_5m_fvg(strategy, NIGHT_START)  # LONG anchor; fills on a dip, not a rally
    assert strategy.state is State.WAIT_FILL
    entry = strategy._pending_limit_price

    # Price runs well past the entry (up, away from the dip that would
    # fill a LONG) without ever retracing back down to it.
    runaway_time = NIGHT_START + timedelta(minutes=5 * 8) + timedelta(minutes=30)
    signal = strategy.on_bar(flat_bar(runaway_time, entry + 60.0))

    assert signal is None
    assert strategy.state is State.WAIT_FVG
    assert strategy.stats["stale_abandoned"] == 1
    stale = [a for a in strategy.anchor_history if a.outcome == "stale"]
    assert len(stale) == 1


def test_stale_anchor_is_not_immediately_re_picked_after_being_abandoned():
    """Regression test for a real live bug (2026-07-09): the first
    implementation of stale-anchor abandonment didn't exclude the dropped
    anchor from re-selection, so the exact same gap -- still the nearest
    unmitigated candidate in the pool -- got immediately re-picked next
    bar and immediately re-tripped the same stale check, over and over
    (bot.log showed one real gap marked "stale" ~15 times in a row instead
    of the strategy ever moving on). Once abandoned, the same anchor must
    not resurface."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))

    feed_large_5m_fvg(strategy, NIGHT_START)
    entry = strategy._pending_limit_price
    abandoned_gap_id = id(strategy._anchor_fvg)

    runaway_time = NIGHT_START + timedelta(minutes=5 * 8) + timedelta(minutes=30)
    strategy.on_bar(flat_bar(runaway_time, entry + 60.0))
    assert strategy.state is State.WAIT_FVG

    # Price stays right where it is -- the only candidate FVG in the pool
    # is the one that was just abandoned, so it must not get re-picked.
    signal = strategy.on_bar(flat_bar(runaway_time + timedelta(minutes=1), entry + 60.0))

    assert signal is None
    assert strategy.state is State.WAIT_FVG
    assert strategy._anchor_fvg is None
    assert strategy.stats["stale_abandoned"] == 1  # not re-tripped a second time
    assert abandoned_gap_id in strategy._rejected_anchor_ids


def test_falls_back_to_the_cap_when_no_structural_stop_exists_at_all():
    """User's explicit instruction, 2026-07-10, against known contrary
    evidence (flagged before making this change -- see
    find_structural_stop_price's docstring): rather than skip a trade
    entirely when neither a qualifying FVG nor a swing point exists on
    the stop side at all, take it anyway using the full max_stop_dollars
    budget as the stop distance. The tight anchor here can't be its own
    stop (see find_structural_stop_price's docstring for why), and
    nothing else was constructed, so this exercises the pure "nothing at
    all" case -- not a too-tight real level (see the test right after
    this one for that, still-rejected, case)."""
    cfg = load_test_config()
    cfg.strategy.min_stop_dollars = 40.0
    cfg.strategy.max_stop_dollars = 200.0
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 105.0))

    # A gap directly seeded so its midpoint sits only ~0.5 points from the
    # only nearby structural level (itself, effectively) -- white-box,
    # since deriving this from raw bars would just be re-deriving the same
    # fact less directly.
    tight_gap = FairValueGap(
        direction=Direction.LONG,
        gap_low=104.9,
        gap_high=105.1,
        formed_at=NIGHT_START,
        timeframe_minutes=5,
    )
    strategy.fvg_detector_5m._active.append(tight_gap)

    # A tight bar that doesn't dip below gap_low (104.9) -- a wider
    # flat_bar spread would immediately mitigate the seeded gap before it
    # could ever be picked as a candidate.
    signal = strategy.on_bar(bar_at(NIGHT_START + timedelta(minutes=1), 105.0, 105.02, 104.98, 105.0))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    fill_time = NIGHT_START + timedelta(minutes=2)
    signal = strategy.on_bar(bar_at(fill_time, 105.0, 105.05, 104.8, 104.9))

    # $200 max_stop_dollars / (2.0 point_value * 1 contract) = 100-point cap.
    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == pytest.approx(105.0)
    assert signal.stop_price == pytest.approx(5.0)
    assert signal.stop_source == "cap"
    assert strategy.state is State.IN_TRADE
    assert strategy.anchor_history[-1].outcome == "filled"


def test_a_too_tight_real_swing_point_is_still_rejected_not_overridden_by_the_cap():
    """The cap fallback above only applies when NEITHER a qualifying FVG
    nor a swing point exists at all -- a real swing point that's simply
    too close for min_stop_dollars must still be rejected (no_valid_stop),
    not silently replaced by the cap."""
    cfg = load_test_config()
    cfg.strategy.min_stop_dollars = 40.0
    cfg.strategy.max_stop_dollars = 200.0
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 105.0))

    tight_gap = FairValueGap(
        direction=Direction.LONG,
        gap_low=104.9,
        gap_high=105.1,
        formed_at=NIGHT_START,
        timeframe_minutes=5,
    )
    strategy.fvg_detector_5m._active.append(tight_gap)
    # $40 min_stop_dollars / (2.0 point_value * 1 contract) = 20-point
    # floor -- a swing low only ~0.11 points below the 105.0 entry is a
    # real, existing structural point, just far too tight to be a genuine
    # invalidation level.
    strategy.swing_tracker.most_recent_swing_low = 104.89

    signal = strategy.on_bar(bar_at(NIGHT_START + timedelta(minutes=1), 105.0, 105.02, 104.98, 105.0))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    fill_time = NIGHT_START + timedelta(minutes=2)
    signal = strategy.on_bar(bar_at(fill_time, 105.0, 105.05, 104.8, 104.9))

    assert signal is None
    assert strategy.state is State.WAIT_FVG
    assert strategy._anchor_fvg is None
    assert strategy.anchor_history[-1].outcome == "no_valid_stop"


def test_notify_trade_closed_stands_down_for_the_night_after_a_win():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    strategy.state = State.IN_TRADE

    strategy.notify_trade_closed(won=True)
    assert strategy.state is State.DONE_FOR_NIGHT

    signal = strategy.on_bar(flat_bar(NIGHT_START + timedelta(hours=1), 107.0))
    assert signal is None
    assert strategy.state is State.DONE_FOR_NIGHT


def test_notify_trade_closed_reenters_the_hunt_after_a_stop_when_allowed():
    cfg = load_test_config()
    assert cfg.strategy.reentry.allow_reentry_after_stop is True
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    strategy.state = State.IN_TRADE

    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_FVG
    assert strategy._direction is None


def test_max_trades_per_night_stands_down_once_the_cap_is_reached():
    """Regression test for the max_trades_per_night cap (config.yaml):
    a real 30-day --overnight backtest showed nights with 2+ trades
    (reentries after a stop) performing far worse (29% WR) than
    single-trade nights (67% WR). Even though allow_reentry_after_stop is
    true, the strategy must stand down for the rest of the night once its
    own trade count for that night hits the cap."""
    cfg = load_test_config()
    cfg.strategy.overnight.max_trades_per_night = 2
    assert cfg.strategy.reentry.allow_reentry_after_stop is True
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))

    strategy.state = State.IN_TRADE
    strategy._trades_tonight = 1
    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.WAIT_FVG  # first reentry still allowed (1 < cap of 2)

    strategy.state = State.IN_TRADE
    strategy._trades_tonight = 2
    strategy.notify_trade_closed(won=False)
    assert strategy.state is State.DONE_FOR_NIGHT  # cap reached -- no further reentry

    signal = strategy.on_bar(flat_bar(NIGHT_START + timedelta(hours=1), 107.0))
    assert signal is None
    assert strategy.state is State.DONE_FOR_NIGHT


def test_max_trades_per_night_resets_for_a_fresh_night():
    cfg = load_test_config()
    cfg.strategy.overnight.max_trades_per_night = 1
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    strategy._trades_tonight = 1

    next_night = NIGHT_START + timedelta(days=1)
    signal = strategy.on_bar(flat_bar(next_night, 100.0))
    assert signal is None
    assert strategy.state is State.WAIT_FVG
    assert strategy._trades_tonight == 0


def test_status_snapshot_reflects_current_hunt_state():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    idle = strategy.status_snapshot()
    assert idle["state"] == "IDLE"
    assert idle["direction"] is None
    assert idle["anchor_gap_low"] is None
    assert idle["pending_limit_price"] is None

    strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, NIGHT_START)

    waiting_fill = strategy.status_snapshot()
    assert waiting_fill["state"] == "WAIT_FILL"
    assert waiting_fill["direction"] == "long"
    assert waiting_fill["anchor_gap_low"] == pytest.approx(anchor_low)
    assert waiting_fill["anchor_gap_high"] == pytest.approx(anchor_high)
    assert waiting_fill["pending_limit_price"] == pytest.approx((anchor_low + anchor_high) / 2)


def test_notify_entry_not_filled_goes_back_to_hunting_instead_of_staying_stuck():
    """Confirmed live 2026-07-08: this method didn't exist at all before --
    a real live entry-order failure left Runner._enter_trade's not-filled
    path calling it, raising AttributeError and leaving self.state stuck at
    IN_TRADE forever, with no trade ever recorded and no further hunting
    for the rest of the process's life."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    anchor_start = feed_swing_low_after_window_opens(strategy, NIGHT_START)
    anchor_low, anchor_high = feed_large_5m_fvg(strategy, anchor_start)
    fill_time = anchor_start + timedelta(minutes=5 * 8) + timedelta(minutes=16)
    signal = strategy.on_bar(bar_at(fill_time, anchor_high, anchor_high + 0.1, anchor_low, anchor_low + 0.1))
    assert signal is not None
    assert strategy.state is State.IN_TRADE  # as if on_bar just fired this signal

    strategy.notify_entry_not_filled()  # must not raise AttributeError

    assert strategy.state is State.WAIT_FVG
