from datetime import datetime
from zoneinfo import ZoneInfo

from src.fvg import FairValueGap
from src.models import Direction
from src.risk import compute_stop_target, find_structural_stop_price, round_to_tick

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 6, 10, 0, tzinfo=TZ)


def make_fvg(direction: Direction, gap_low: float, gap_high: float, timeframe_minutes: int = 5) -> FairValueGap:
    return FairValueGap(
        direction=direction, gap_low=gap_low, gap_high=gap_high, formed_at=NOW, timeframe_minutes=timeframe_minutes
    )


# ---------- compute_stop_target: validates a given stop_price against the $min-$max budget ----------


def test_returns_none_when_stop_price_is_none():
    """find_structural_stop_price returns None when neither a qualifying
    FVG nor a break-of-structure swing point exists -- compute_stop_target
    must treat that the same as any other "no real invalidation point"
    case: skip the trade."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=None,
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert result is None


def test_long_computes_stop_and_target_from_a_given_stop_price():
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=95.0,
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert result.stop_points == 5.0
    assert result.stop_price == 95.0
    assert result.target_points == 10.0
    assert result.target_price == 110.0


def test_short_computes_stop_and_target_from_a_given_stop_price():
    result = compute_stop_target(
        direction=Direction.SHORT,
        entry_price=100.0,
        stop_price=110.0,
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert result.stop_points == 10.0
    assert result.stop_price == 110.0
    assert result.target_points == 20.0
    assert result.target_price == 80.0


def test_returns_none_when_stop_price_too_far():
    # $200 / (2.0 point_value * 1 contract) = 100 point cap -- 150 points away is too far.
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=-50.0,
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert result is None


def test_returns_none_when_stop_price_too_close():
    """A real 7-day backtest showed trades whose stop landed under ~20
    points away won only 1 of 7 times, versus 3 of 6 for wider stops -- so
    a stop closer than min_stop_dollars is rejected the same way an
    out-of-budget one is. $40 / (2.0 * 1) = 20-point floor; 3 points away
    is too close."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=97.0,
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert result is None


def test_dollar_cap_shrinks_in_points_as_contract_size_scales_up():
    """The $200 cap stays fixed in dollars, so at more contracts it maps
    to fewer points -- $200 / (2.0 point_value * 4 contracts) = 25 points,
    versus 100 points at 1 contract. The same 50-point-away stop fits the
    budget at 1 contract but not at 4."""
    at_one_contract = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=50.0,
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert at_one_contract.stop_points == 50.0
    assert at_one_contract.stop_price == 50.0

    at_four_contracts = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=50.0,  # 50 points away -- beyond the 25pt cap at 4 contracts
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=4,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert at_four_contracts is None


def test_dollar_floor_grows_in_points_as_contract_size_scales_up():
    """The $40 floor stays fixed in dollars, so at more contracts it maps
    to more points -- $40 / (2.0 point_value * 1 contract) = 20 points,
    versus 5 points at 4 contracts. The same 10-point-away stop is too
    close at 1 contract but clears the (smaller) floor at 4."""
    at_one_contract = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=90.0,
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert at_one_contract is None

    at_four_contracts = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=90.0,
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,  # $40 / (2.0 * 4) = 5-point floor -- 10 points now clears it
        point_value=2.0,
        contracts=4,
        reward_risk_ratio=2.0,
        tick_size=0.01,
    )
    assert at_four_contracts.stop_points == 10.0


# ---------- find_structural_stop_price: break of structure first, else nearest strong 5m FVG ----------
# (priority flipped 2026-07-08 -- see the function's docstring for the real-data reasoning)


def test_long_stop_falls_back_to_the_nearest_qualifying_fvgs_outer_edge_below_entry_when_no_swing():
    """No swing point given -- falls back to FVG. Two candidate support
    FVGs below entry (98-99 and 90-92) -- the nearer one (98-99) wins, and
    the stop sits at its *outer* (low) edge, not its near edge."""
    nearer = make_fvg(Direction.LONG, gap_low=98.0, gap_high=99.0)
    farther = make_fvg(Direction.LONG, gap_low=90.0, gap_high=92.0)
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[nearer, farther],
        swing_high=None,
        swing_low=None,
    )
    assert stop.price == 98.0
    assert stop.source == "fvg"
    assert stop.fvg_size == 1.0


def test_short_stop_falls_back_to_the_nearest_qualifying_fvgs_outer_edge_above_entry_when_no_swing():
    nearer = make_fvg(Direction.SHORT, gap_low=101.0, gap_high=102.0)
    farther = make_fvg(Direction.SHORT, gap_low=108.0, gap_high=110.0)
    stop = find_structural_stop_price(
        direction=Direction.SHORT,
        entry_price=100.0,
        fvg_candidates=[nearer, farther],
        swing_high=None,
        swing_low=None,
    )
    assert stop.price == 102.0
    assert stop.source == "fvg"
    assert stop.fvg_size == 1.0


def test_fvg_candidates_on_the_wrong_side_of_entry_are_ignored():
    """A LONG-direction FVG whose gap sits *above* entry (not below) isn't
    a valid stop reference for a long -- must be ignored, same as if it
    didn't exist at all (no swing point given either, so this must fall
    all the way through to None)."""
    wrong_side = make_fvg(Direction.LONG, gap_low=101.0, gap_high=103.0)
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[wrong_side],
        swing_high=None,
        swing_low=None,
    )
    assert stop is None


def test_swing_point_is_the_primary_stop_for_long_and_short():
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[],
        swing_high=None,
        swing_low=93.0,
    )
    assert stop.price == 93.0
    assert stop.source == "swing"

    stop = find_structural_stop_price(
        direction=Direction.SHORT,
        entry_price=100.0,
        fvg_candidates=[],
        swing_high=107.0,
        swing_low=None,
    )
    assert stop.price == 107.0
    assert stop.source == "swing"


def test_a_swing_point_on_the_wrong_side_of_entry_does_not_count():
    """A "swing low" that's actually above entry can't be a long's stop --
    must fall through to None (no valid stop at all) rather than using it
    anyway."""
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[],
        swing_high=None,
        swing_low=101.0,
    )
    assert stop is None


def test_returns_none_when_neither_fvg_nor_swing_point_exists():
    """Reverted 2026-07-14 (the cap-fallback experiment from 2026-07-10 is
    gone, see the function's own docstring) -- back to skipping the trade
    entirely when nothing structural exists on the stop side at all."""
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[],
        swing_high=None,
        swing_low=None,
    )
    assert stop is None

    stop = find_structural_stop_price(
        direction=Direction.SHORT,
        entry_price=100.0,
        fvg_candidates=[],
        swing_high=None,
        swing_low=None,
    )
    assert stop is None


def test_prefer_swing_true_takes_priority_over_a_qualifying_fvg():
    """prefer_swing defaults to True -- used by the overnight strategy
    since a real 32-trade backtest showed swing-based stops winning 50%
    (+$55/trade) versus FVG-based stops winning only 35% (+$12/trade)
    there (see risk.py's find_structural_stop_price) -- even when the FVG
    would give a tighter stop, it must not be used if a swing point
    qualifies."""
    fvg = make_fvg(Direction.LONG, gap_low=98.0, gap_high=99.0)
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[fvg],
        swing_high=None,
        swing_low=90.0,  # farther than the FVG, but must still win
    )
    assert stop.price == 90.0
    assert stop.source == "swing"


def test_prefer_swing_false_takes_the_fvg_even_when_swing_is_closer():
    """prefer_swing=False -- used by the day (opening-range breakout)
    strategy, since a real 30-day backtest showed the opposite priority
    order made *that* strategy worse (30%->24% win rate,
    -$199.75->-$424.75 net): the FVG requirement filters entries down to
    ones with a real support/resistance gap nearby, and letting swing win
    let too many marginal setups through instead."""
    fvg = make_fvg(Direction.LONG, gap_low=90.0, gap_high=92.0)
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[fvg],
        swing_high=None,
        swing_low=98.0,  # closer than the FVG, but must not be used
        prefer_swing=False,
    )
    assert stop.price == 90.0
    assert stop.source == "fvg"


def test_falls_back_to_a_qualifying_fvg_when_no_swing_point_qualifies():
    fvg = make_fvg(Direction.LONG, gap_low=90.0, gap_high=92.0)
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[fvg],
        swing_high=None,
        swing_low=None,
    )
    assert stop.price == 90.0
    assert stop.source == "fvg"


def test_prefer_swing_false_still_falls_back_to_swing_when_no_fvg_qualifies():
    stop = find_structural_stop_price(
        direction=Direction.LONG,
        entry_price=100.0,
        fvg_candidates=[],
        swing_high=None,
        swing_low=93.0,
        prefer_swing=False,
    )
    assert stop.price == 93.0
    assert stop.source == "swing"


# ---------- round_to_tick / compute_stop_target's tick-alignment rounding ----------
# (added 2026-08-07 -- see round_to_tick's own docstring for the real bot.log
# evidence: entry/target prices routinely landed off the exchange's tick
# grid, silently rejected by the gateway and swallowed as an ordinary "not
# filled" case, costing roughly half of every real entry signal in the
# affected window)


def test_round_to_tick_snaps_to_the_nearest_multiple_of_tick_size():
    """The exact real-world case that surfaced this bug: a gap's midpoint
    of 29573.375 (from bot.log, 2026-08-06) is not a multiple of MNQ's
    real 0.25 tick_size -- the gateway rejected it outright with "Invalid
    limit price. Price is not aligned to tick size." 29573.375 is exactly
    equidistant between 29573.25 and 29573.5, so this isn't testing which
    way a tie breaks (not what the real bug was about) -- only that
    whichever way it goes, the result actually lands on the tick grid."""
    result = round_to_tick(29573.375, 0.25)
    assert result in (29573.25, 29573.5)


def test_round_to_tick_leaves_an_already_aligned_price_unchanged():
    assert round_to_tick(28602.0, 0.25) == 28602.0
    assert round_to_tick(100.0, 0.25) == 100.0


def test_round_to_tick_rounds_down_and_up_correctly():
    assert round_to_tick(100.1, 0.25) == 100.0  # closer to 100.0 than 100.25
    assert round_to_tick(100.2, 0.25) == 100.25  # closer to 100.25 than 100.0


def test_compute_stop_target_rounds_target_price_to_tick_size():
    """target_price is entry_price +/- a floating-point points distance --
    even with a clean entry_price and stop_price, the multiplication by
    reward_risk_ratio routinely produces a sub-tick result. Confirmed
    real bug: this was sent straight to the gateway unrounded and
    rejected."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=93.25,  # already tick-aligned, isolating this test to target_price's own rounding
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=1.67,  # the real config's current ratio
        tick_size=0.25,
    )
    # stop_points = 6.75, target_points = 6.75*1.67 = 11.2725, target_price
    # = 111.2725 unrounded -- not a multiple of 0.25, confirming this case
    # would have hit the real bug without the fix. 111.2725/0.25=445.09,
    # rounds to 445 -> 111.25 (a hardcoded expected value, not
    # round_to_tick itself, so this actually verifies the rounding rather
    # than restating it).
    assert result.target_price == 111.25


def test_compute_stop_target_rounds_stop_price_to_tick_size_too():
    """stop_price is normally real-market-derived and already aligned, but
    rounded defensively regardless -- and stop_points/target_points must
    reflect the *rounded* stop_price, not the original, so they stay
    internally consistent with what's actually sent to the broker."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=93.37,  # deliberately not tick-aligned
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
        tick_size=0.25,
    )
    # 93.37 / 0.25 = 373.48 -> rounds to 373 -> 373 * 0.25 = 93.25 (a
    # hardcoded expected value, not round_to_tick itself, so this
    # actually verifies the rounding rather than restating it).
    assert result.stop_price == 93.25
    assert result.stop_points == 100.0 - 93.25
