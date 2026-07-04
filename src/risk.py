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
    """Stop is the *farthest* marked structural level beyond entry that
    still fits within the max_stop_dollars budget (previous day/Asia/
    London high-low or opening range box edge -- see strategy.py's
    structural_levels), so the dollar risk never exceeds that cap
    regardless of which structural level ends up used, but a nearby level
    doesn't automatically win over a farther one that's still affordable.
    If nothing fits within budget at all (the nearest real level is
    farther out than the cap allows, or there's no level on that side at
    all), the cap itself is used as the stop distance outright. Target is
    always reward_risk_ratio x the actual stop distance used.

    Deliberately picks the farthest-within-budget level, not the nearest
    one: a real 30-day backtest showed every trade whose stop landed on
    the nearest available level (typically the opening-range box edge,
    which is often close simply because that's where the breakout itself
    happened) lost, while every trade that fell back to the full budget
    won or lost like a normal 2:1 setup -- "nearest" was consistently
    finding a minor speed bump, not a real invalidation point. Using
    whichever real level maximizes the affordable stop distance still
    respects "market structure validates it" (the level is real and
    marked, not arbitrary) while not leaving budget unused just because
    a closer, weaker level happened to exist too.
    """
    max_stop_points = max_stop_dollars / (point_value * contracts)

    if direction is Direction.LONG:
        candidates = [lvl for lvl in structural_levels if lvl < entry_price and entry_price - lvl <= max_stop_points]
        farthest = min(candidates) if candidates else None
        structural_distance = (entry_price - farthest) if farthest is not None else None
    else:
        candidates = [lvl for lvl in structural_levels if lvl > entry_price and lvl - entry_price <= max_stop_points]
        farthest = max(candidates) if candidates else None
        structural_distance = (farthest - entry_price) if farthest is not None else None

    if structural_distance is None:
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
