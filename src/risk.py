from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.config import RiskLimitsConfig
from src.models import Direction


@dataclass
class BracketLevels:
    stop_price: float
    target_price: float
    stop_points: float
    target_points: float


def compute_stop_target(
    direction: Direction,
    entry_price: float,
    structural_levels: list[float],
    max_stop_dollars: float,
    point_value: float,
    contracts: int,
    reward_risk_ratio: float,
) -> BracketLevels:
    """Stop is the nearest marked structural level beyond entry (previous
    day/Asia/London high-low, or opening range box edge -- see
    strategy.py's structural_levels), capped at whatever max_stop_dollars
    is worth in points at the current contract size, so the dollar risk
    never exceeds that cap regardless of which structural level ends up
    nearest. Target is always reward_risk_ratio x the actual stop
    distance used.

    (Revision history: briefly changed 2026-07-04 to pick the *farthest*
    level within budget instead of the nearest, on the theory that
    "nearest" was consistently just the opening-range box edge -- close
    because that's where the breakout happened, not a real invalidation
    point -- after a 7-trade sample showed 0 of 4 nearest-level trades
    winning versus 2 of 3 cap-based trades winning. Reverted the same
    day: re-running the identical 7 trades with farthest-within-budget
    selection only actually changed the stop for 2 of them (2026-06-11,
    2026-06-23) -- both were already losses, and the wider stop just
    made them lose *more* ($47.75->$93.24 and $115.25->$174.76) without
    turning either into a win. Win rate stayed at 2/7 (28.6%) but net P&L
    dropped from $354.25 to $249.26. Those 2 trades weren't stopped out
    by noise that a wider stop would have ridden through -- they were
    setups that kept moving against the position regardless, so nearest
    -- the more conservative choice when both are equally "real"
    structure -- is what the evidence actually supports.)
    """
    max_stop_points = max_stop_dollars / (point_value * contracts)

    if direction is Direction.LONG:
        candidates = [lvl for lvl in structural_levels if lvl < entry_price]
        nearest = max(candidates) if candidates else None
        structural_distance = (entry_price - nearest) if nearest is not None else None
    else:
        candidates = [lvl for lvl in structural_levels if lvl > entry_price]
        nearest = min(candidates) if candidates else None
        structural_distance = (nearest - entry_price) if nearest is not None else None

    if structural_distance is None or structural_distance > max_stop_points:
        stop_points = max_stop_points
    else:
        stop_points = structural_distance

    target_points = stop_points * reward_risk_ratio

    if direction is Direction.LONG:
        stop_price = entry_price - stop_points
        target_price = entry_price + target_points
    else:
        stop_price = entry_price + stop_points
        target_price = entry_price - target_points

    return BracketLevels(
        stop_price=stop_price,
        target_price=target_price,
        stop_points=stop_points,
        target_points=target_points,
    )


class DailyRiskState:
    """Tracks trade count / realized P&L for the current trading day and
    enforces the account-protection limits in config.yaml (risk_limits)."""

    def __init__(self, cfg: RiskLimitsConfig):
        self.cfg = cfg
        self.trades_today: int = 0
        self.realized_pnl_today: float = 0.0
        self._day: date | None = None

    def reset_if_new_day(self, trading_date: date) -> None:
        if self._day != trading_date:
            self._day = trading_date
            self.trades_today = 0
            self.realized_pnl_today = 0.0

    def record_trade_result(self, pnl_dollars: float) -> None:
        self.trades_today += 1
        self.realized_pnl_today += pnl_dollars

    def can_take_new_trade(self) -> bool:
        if self.cfg.kill_switch:
            return False
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False
        if self.realized_pnl_today <= -abs(self.cfg.max_daily_loss_dollars):
            return False
        return True
