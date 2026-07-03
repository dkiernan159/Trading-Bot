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
    max_stop_points: float,
    reward_risk_ratio: float,
) -> BracketLevels:
    """Stop is the nearest marked structural level beyond entry (previous
    day/Asia/London high-low, opening range box edge), capped at
    max_stop_points so it never exceeds what the reward:risk ratio implies.
    Target is always reward_risk_ratio x the actual stop distance used.
    """
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
