from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import load_config
from src.models import Bar, Direction
from src.fvg import FairValueGap
from src.overnight_strategy import OvernightMomentumStrategy, State

TZ = ZoneInfo("America/New_York")
# 19:00 ET on a Sunday evening -- t >= asia_start, so this whole overnight
# window's marked levels/trading_date is the *next* day, 2026-07-06.
NIGHT_START = datetime(2026, 7, 5, 19, 0, tzinfo=TZ)


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


def flat_bar(dt: datetime, price: float, spread: float = 0.4) -> Bar:
    return bar_at(dt, price, price + spread / 2, price - spread / 2, price)


def feed_quiet(strategy: OvernightMomentumStrategy, start: datetime, count: int, step_minutes: int, price: float):
    for i in range(count):
        signal = strategy.on_bar(flat_bar(start + timedelta(minutes=step_minutes * i), price))
        assert signal is None


def feed_large_anchor_fvg(strategy: OvernightMomentumStrategy, start: datetime) -> tuple[float, float, datetime]:
    """One bar per 15-minute bucket (see tests/test_fvg.py's bucket_bar) --
    8 quiet baseline candles, then a 3-candle bullish displacement pattern,
    then a flush bar that finalizes/detects it. Returns (gap_low, gap_high,
    locked_in_at)."""
    feed_quiet(strategy, start, 8, 15, 105.0)
    pattern_start = start + timedelta(minutes=15 * 8)

    assert strategy.on_bar(bar_at(pattern_start, 105.0, 105.3, 104.9, 105.05)) is None  # c0
    assert (
        strategy.on_bar(bar_at(pattern_start + timedelta(minutes=15), 105.05, 109.5, 105.0, 109.3)) is None
    )  # c1: displacement
    assert (
        strategy.on_bar(bar_at(pattern_start + timedelta(minutes=30), 109.3, 109.6, 109.1, 109.5)) is None
    )  # c2

    flush_time = pattern_start + timedelta(minutes=45)
    signal = strategy.on_bar(bar_at(flush_time, 109.5, 109.6, 109.4, 109.5))
    assert signal is None
    assert strategy.state is State.WAIT_ENTRY
    assert strategy._direction is Direction.LONG

    return 105.3, 109.1, flush_time


def test_outside_the_window_the_strategy_stays_idle():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    # Midday, nowhere near Asia (19:00) or London (02:00-05:00).
    midday = datetime(2026, 7, 6, 12, 0, tzinfo=TZ)
    signal = strategy.on_bar(flat_bar(midday, 100.0))
    assert signal is None
    assert strategy.state is State.IDLE
    assert strategy._direction is None


def test_entering_the_window_starts_the_anchor_hunt_directly_no_breakout():
    """Confirms the design point the user chose explicitly: no box/breakout
    step at all -- the window opening is enough to start hunting for a
    large FVG, whichever direction it turns out to be."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    signal = strategy.on_bar(flat_bar(NIGHT_START - timedelta(minutes=1), 100.0))
    assert signal is None
    assert strategy.state is State.IDLE

    signal = strategy.on_bar(flat_bar(NIGHT_START, 100.0))
    assert signal is None
    assert strategy.state is State.WAIT_ANCHOR


def test_full_anchor_then_nested_entry_fills_at_the_nested_fvgs_own_midpoint():
    """End-to-end: a large 15m FVG sets LONG direction directly, then a
    smaller 5m FVG that forms after the anchor locks in, whose own midpoint
    sits inside the anchor's gap, is the actual entry trigger."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)

    anchor_low, anchor_high, locked_in_at = feed_large_anchor_fvg(strategy, NIGHT_START)
    assert anchor_low == pytest.approx(105.3)
    assert anchor_high == pytest.approx(109.1)

    # Nested 5m pattern, one bar per 5-minute bucket, entirely after
    # locked_in_at -- deliberately small (gap size 1.55) so it clears
    # entry_5m's own min_gap_points (1.5) but stays below anchor_15m's
    # (2.0), and its midpoint (~107.08) lands inside [105.3, 109.1].
    feed_quiet(strategy, locked_in_at + timedelta(minutes=5), 8, 5, 106.0)
    nested_pattern_start = locked_in_at + timedelta(minutes=5 * 9)
    assert strategy.on_bar(bar_at(nested_pattern_start, 106.0, 106.3, 105.8, 106.05)) is None
    assert (
        strategy.on_bar(bar_at(nested_pattern_start + timedelta(minutes=5), 106.05, 108.0, 106.0, 107.9))
        is None
    )
    signal = strategy.on_bar(
        bar_at(nested_pattern_start + timedelta(minutes=10), 107.9, 108.1, 107.85, 108.0)
    )
    assert signal is None
    assert strategy.state is State.WAIT_ENTRY

    # The pattern (c0/c1/c2) is only finalized/detected once the *next*
    # bar arrives (same as tests/test_fvg.py's bucket_bar convention).
    nested_locked_time = nested_pattern_start + timedelta(minutes=15)
    signal = strategy.on_bar(bar_at(nested_locked_time, 108.0, 108.05, 107.95, 108.0))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    nested_gap_low, nested_gap_high = 106.3, 107.85
    midpoint = (nested_gap_low + nested_gap_high) / 2
    assert strategy._pending_limit_price == pytest.approx(midpoint)

    fill_time = nested_locked_time + timedelta(minutes=5)
    signal = strategy.on_bar(bar_at(fill_time, 107.9, 108.0, 107.0, 107.5))

    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.entry_price == pytest.approx(midpoint)
    assert signal.anchor_fvg.gap_low == pytest.approx(anchor_low)
    assert signal.anchor_fvg.gap_high == pytest.approx(anchor_high)
    assert signal.entry_fvg.gap_low == pytest.approx(nested_gap_low)
    # The anchor's own edges are included as structural stop candidates.
    assert anchor_low in signal.structural_levels
    assert anchor_high in signal.structural_levels
    assert strategy.state is State.IN_TRADE
    assert strategy.stats["fills"] == 1

    assert len(strategy.anchor_history) == 1
    assert strategy.anchor_history[0].outcome == "filled"


def test_nested_fvg_that_formed_before_the_anchor_locked_in_does_not_count():
    """Confirms the user's other explicit design choice: a nested FVG must
    be a genuine post-anchor retest, not a coincidentally-overlapping gap
    that was already sitting there when the anchor confirmed."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    anchor_low, anchor_high, locked_in_at = feed_large_anchor_fvg(strategy, NIGHT_START)

    # Directly seed a nested-shaped gap whose formed_at is *before*
    # locked_in_at -- white-box, since orchestrating this exact timing via
    # raw bars would just be re-deriving the same fact less directly.
    stale_gap = FairValueGap(
        direction=Direction.LONG,
        gap_low=106.3,
        gap_high=107.85,
        formed_at=locked_in_at - timedelta(minutes=5),
        timeframe_minutes=5,
    )
    strategy.entry_detector_5m._active.append(stale_gap)

    signal = strategy.on_bar(flat_bar(locked_in_at + timedelta(minutes=1), 107.0))
    assert signal is None
    assert strategy.state is State.WAIT_ENTRY
    assert strategy._nested_fvg is None


def test_nested_fvg_whose_midpoint_falls_outside_the_anchor_is_not_a_candidate():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    anchor_low, anchor_high, locked_in_at = feed_large_anchor_fvg(strategy, NIGHT_START)

    # Midpoint at 110 -- above anchor_high (109.1), so outside the gap.
    outside_gap = FairValueGap(
        direction=Direction.LONG,
        gap_low=109.8,
        gap_high=110.2,
        formed_at=locked_in_at + timedelta(minutes=1),
        timeframe_minutes=5,
    )
    strategy.entry_detector_5m._active.append(outside_gap)

    signal = strategy.on_bar(flat_bar(locked_in_at + timedelta(minutes=2), 107.0))
    assert signal is None
    assert strategy.state is State.WAIT_ENTRY
    assert strategy._nested_fvg is None


def test_mitigated_anchor_with_no_replacement_is_invalidated_not_left_stale():
    """Regression test for a bug in the original (removed) nested-FVG
    design: if the live anchor became mitigated with nothing to replace
    it, the old code's `if candidates:` guard silently did nothing,
    leaving self._anchor_fvg pointing at an already-dead gap forever. This
    strategy must instead explicitly invalidate and restart the hunt from
    scratch (direction included)."""
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    anchor_low, anchor_high, locked_in_at = feed_large_anchor_fvg(strategy, NIGHT_START)

    # A bar trading below gap_low mitigates the anchor (LONG gap); nothing
    # else is live to replace it.
    mitigate_time = locked_in_at + timedelta(minutes=1)
    signal = strategy.on_bar(bar_at(mitigate_time, 105.3, 105.3, 105.0, 105.1))

    assert signal is None
    assert strategy.state is State.WAIT_ANCHOR
    assert strategy._anchor_fvg is None
    assert strategy._direction is None
    assert strategy.stats["anchors_invalidated"] == 1

    invalidated = [a for a in strategy.anchor_history if a.outcome == "invalidated"]
    assert len(invalidated) == 1
    assert invalidated[0].gap_low == pytest.approx(anchor_low)


def test_a_nearer_fresher_anchor_supersedes_the_current_one():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    anchor_low, anchor_high, locked_in_at = feed_large_anchor_fvg(strategy, NIGHT_START)
    original_anchor = strategy._anchor_fvg

    nearer_anchor = FairValueGap(
        direction=Direction.LONG,
        gap_low=106.5,
        gap_high=107.5,
        formed_at=locked_in_at + timedelta(minutes=1),
        timeframe_minutes=30,
    )
    strategy.anchor_detector_30m._active.append(nearer_anchor)

    # Current price (107.0) is nearer to nearer_anchor's midpoint (107.0)
    # than to original_anchor's (107.2) -- not by much, but the point is
    # just that a fresher candidate is picked up at all, since a real
    # bar's `bar.close` decides distance.
    signal = strategy.on_bar(bar_at(locked_in_at + timedelta(minutes=2), 107.0, 107.05, 106.95, 107.0))

    assert signal is None
    assert strategy._anchor_fvg is nearer_anchor
    assert strategy.state is State.WAIT_ENTRY

    superseded = [a for a in strategy.anchor_history if a.outcome == "superseded"]
    assert len(superseded) == 1
    assert superseded[0].gap_low == pytest.approx(original_anchor.gap_low)


def test_no_real_structural_stop_within_budget_rejects_the_nested_entry_and_keeps_hunting():
    """Same $40-$200 stop-band rule as the day strategy (risk.py) applies
    here too -- if the nested entry's own fill has no real marked level
    within budget, it's skipped (not defaulted to the cap), and the anchor
    stays live for a different nested entry to be found."""
    cfg = load_test_config()
    cfg.strategy.min_stop_dollars = 40.0
    cfg.strategy.max_stop_dollars = 200.0
    strategy = OvernightMomentumStrategy(cfg)
    anchor_low, anchor_high, locked_in_at = feed_large_anchor_fvg(strategy, NIGHT_START)

    # Nested gap whose midpoint sits only ~1 point above the anchor's own
    # low (105.3) -- entry - anchor_low is well under the $40 floor
    # (0.5pt * $2/pt = $1), and no other structural level is anywhere near.
    tight_nested = FairValueGap(
        direction=Direction.LONG,
        gap_low=105.8,
        gap_high=106.2,
        formed_at=locked_in_at + timedelta(minutes=1),
        timeframe_minutes=5,
    )
    strategy.entry_detector_5m._active.append(tight_nested)

    signal = strategy.on_bar(bar_at(locked_in_at + timedelta(minutes=2), 106.0, 106.05, 105.95, 106.0))
    assert signal is None
    assert strategy.state is State.WAIT_FILL

    fill_time = locked_in_at + timedelta(minutes=3)
    signal = strategy.on_bar(bar_at(fill_time, 106.0, 106.05, 105.9, 106.0))

    assert signal is None
    assert strategy.state is State.WAIT_ENTRY
    assert strategy._nested_fvg is None
    # The anchor itself is untouched -- only the rejected nested candidate
    # is excluded from being re-picked.
    assert strategy._anchor_fvg is not None
    assert strategy._anchor_fvg.gap_low == pytest.approx(anchor_low)


def test_notify_trade_closed_stands_down_for_the_night_after_a_win():
    cfg = load_test_config()
    strategy = OvernightMomentumStrategy(cfg)
    feed_large_anchor_fvg(strategy, NIGHT_START)
    strategy.state = State.IN_TRADE

    strategy.notify_trade_closed(won=True)
    assert strategy.state is State.DONE_FOR_NIGHT

    signal = strategy.on_bar(flat_bar(NIGHT_START + timedelta(hours=1), 107.0))
    assert signal is None
    assert strategy.state is State.DONE_FOR_NIGHT
