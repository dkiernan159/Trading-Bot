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
        point_value=2.0,
        contracts=4,
        reward_risk_ratio=2.0,
    )
    assert at_four_contracts is None
