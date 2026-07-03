from src.models import Direction
from src.risk import compute_stop_target


def test_long_uses_nearby_structural_level_within_cap():
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[95.0, 90.0, 105.0],
        max_stop_points=15.0,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 5.0
    assert result.stop_price == 95.0
    assert result.target_points == 10.0
    assert result.target_price == 110.0


def test_long_caps_stop_when_structural_level_too_far():
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[80.0],
        max_stop_points=15.0,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 15.0
    assert result.stop_price == 85.0
    assert result.target_points == 30.0
    assert result.target_price == 130.0


def test_long_falls_back_to_cap_with_no_levels_below_entry():
    result = compute_stop_target(
        direction=Direction.LONG,
        entry_price=100.0,
        structural_levels=[105.0, 110.0],
        max_stop_points=15.0,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 15.0
    assert result.stop_price == 85.0


def test_short_uses_nearby_structural_level_within_cap():
    result = compute_stop_target(
        direction=Direction.SHORT,
        entry_price=100.0,
        structural_levels=[110.0, 120.0, 95.0],
        max_stop_points=15.0,
        reward_risk_ratio=2.0,
    )
    assert result.stop_points == 10.0
    assert result.stop_price == 110.0
    assert result.target_points == 20.0
    assert result.target_price == 80.0
