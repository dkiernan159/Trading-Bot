from src.models import Direction
from src.risk import compute_stop_target


def test_long_uses_farthest_structural_level_still_within_cap():
    """Two candidates (95.0, 90.0) both fit within the 100-point cap --
    the farther one (90.0) is used, not the nearer one (95.0), so the
    trade gets as much of the affordable, structurally-justified room as
    it can rather than defaulting to whichever level happens to be
    closest."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[95.0, 90.0, 105.0],
        max_stop_dollars=200.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 10.0
    assert result.stop_price == 90.0
    assert result.target_points == 20.0
    assert result.target_price == 120.0


def test_long_caps_stop_when_structural_level_too_far():
    # $200 / (2.0 point_value * 1 contract) = 100 point cap.
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[-50.0],
        max_stop_dollars=200.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 100.0
    assert result.stop_price == 0.0
    assert result.target_points == 200.0
    assert result.target_price == 300.0


def test_long_falls_back_to_cap_with_no_levels_below_entry():
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[105.0, 110.0],
        max_stop_dollars=200.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 100.0
    assert result.stop_price == 0.0


def test_short_uses_farthest_structural_level_still_within_cap():
    """Two candidates (110.0, 120.0) both fit within the 100-point cap --
    the farther one (120.0) is used, not the nearer one (110.0)."""
    result = compute_stop_target(
        direction=Direction.SHORT,
        entry_price=100.0,
        structural_levels=[110.0, 120.0, 95.0],
        max_stop_dollars=200.0,
        point_value=2.0,
        contracts=1,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 20.0
    assert result.stop_price == 120.0
    assert result.target_points == 40.0
    assert result.target_price == 60.0


def test_dollar_cap_shrinks_in_points_as_contract_size_scales_up():
    """The $200 cap stays fixed in dollars, so at more contracts it maps
    to fewer points -- $200 / (2.0 point_value * 4 contracts) = 25 points,
    versus 100 points at 1 contract."""
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[50.0],  # 50 points away -- beyond the 25pt cap at 4 contracts
        max_stop_dollars=200.0,
        point_value=2.0,
        contracts=4,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 25.0
    assert result.stop_price == 75.0
