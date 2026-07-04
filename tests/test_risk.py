from src.models import Direction
from src.risk import compute_stop_target


def test_long_uses_nearby_structural_level_within_cap():
    """Two candidates (95.0, 90.0) both fit within the 100-point cap --
    the nearer one (95.0) is used, not the farther one (90.0). A real
    30-day backtest tried the opposite (farthest-within-cap) and found
    it made losing trades lose more without turning any into wins --
    see risk.py's revision history."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[95.0, 90.0, 105.0],
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 5.0
    assert result.stop_price == 95.0
    assert result.target_points == 10.0
    assert result.target_price == 110.0


def test_long_returns_none_when_structural_level_too_far():
    # $200 / (2.0 point_value * 1 contract) = 100 point cap -- the only
    # candidate (-50.0) is 150 points away, so there's no real level
    # within budget and the trade is skipped rather than defaulting to
    # an arbitrary 100-point stop with nothing structural behind it.
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[-50.0],
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result is None


def test_long_returns_none_with_no_levels_below_entry():
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[105.0, 110.0],
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result is None


def test_long_returns_none_when_structural_level_too_close():
    """A real 7-day backtest showed trades whose stop landed under ~20
    points away (usually the box edge -- close because that's where the
    breakout happened, not real structure) won only 1 of 7 times, versus
    3 of 6 for wider stops -- so a level closer than min_stop_dollars is
    now rejected the same way an out-of-budget one is, rather than taken
    as a noise-sized "stop." $40 / (2.0 * 1) = 20-point floor; the only
    candidate (97.0) is just 3 points away."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[97.0],
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result is None


def test_long_returns_none_when_only_the_nearest_level_is_too_close_even_if_a_farther_one_exists():
    """The nearest candidate (97.0, 3 points away) is too close to be a
    real invalidation point. A second, farther candidate (70.0, 30 points
    away) does clear the 20-point floor -- but the trade is still
    skipped rather than reaching past the too-close nearest level to use
    it. (Briefly changed to prefer this farther-but-valid level 2026-07-04;
    reverted the same day after a real 30-day backtest showed every trade
    recovered that way -- 5 of them -- lost, all landing on Asia/London
    levels reached by skipping a tighter box edge, the same failure shape
    as the farthest-within-budget experiment. See risk.py's revision
    history.)"""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[97.0, 70.0, 105.0],
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result is None


def test_short_uses_nearby_structural_level_within_cap():
    """Two candidates (110.0, 120.0) both fit within the 100-point cap --
    the nearer one (110.0) is used, not the farther one (120.0)."""
    result = compute_stop_target(
        direction=Direction.SHORT,
        entry_price=100.0,
        structural_levels=[110.0, 120.0, 95.0],
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 10.0
    assert result.stop_price == 110.0
    assert result.target_points == 20.0
    assert result.target_price == 80.0


def test_dollar_cap_shrinks_in_points_as_contract_size_scales_up():
    """The $200 cap stays fixed in dollars, so at more contracts it maps
    to fewer points -- $200 / (2.0 point_value * 4 contracts) = 25 points,
    versus 100 points at 1 contract. The same real level (50 points away)
    fits the budget at 1 contract but not at 4, where it's now skipped
    rather than falling back to an arbitrary stop."""
    level = [50.0]
    at_one_contract = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=level,
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert at_one_contract.stop_points == 50.0
    assert at_one_contract.stop_price == 50.0

    at_four_contracts = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=level,  # 50 points away -- beyond the 25pt cap at 4 contracts
        max_stop_dollars=200.0,
        min_stop_dollars=0.0,
        point_value=2.0,
        contracts=4,
        reward_risk_ratio=2.0,
    )
    assert at_four_contracts is None


def test_dollar_floor_grows_in_points_as_contract_size_scales_up():
    """The $40 floor stays fixed in dollars, so at more contracts it maps
    to more points -- $40 / (2.0 point_value * 1 contract) = 20 points,
    versus 5 points at 4 contracts. The same real level (10 points away)
    is too close at 1 contract but clears the (smaller) floor at 4."""
    level = [90.0]  # 10 points away
    at_one_contract = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=level,
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert at_one_contract is None

    at_four_contracts = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=level,
        max_stop_dollars=200.0,
        min_stop_dollars=40.0,  # $40 / (2.0 * 4) = 5-point floor -- 10 points now clears it
        point_value=2.0,
        contracts=4,
        reward_risk_ratio=2.0,
    )
    assert at_four_contracts.stop_points == 10.0
