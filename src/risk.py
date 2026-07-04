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
    min_stop_dollars: float,
    point_value: float,
    contracts: int,
    reward_risk_ratio: float,
) -> BracketLevels | None:
    """Stop is the *nearest* marked structural level beyond entry (previous
    day/Asia/London high-low, or opening range box edge -- see
    strategy.py's structural_levels) whose distance actually falls between
    min_stop_dollars and max_stop_dollars -- not necessarily the nearest
    level overall, since a closer level that's too tight to be a genuine
    invalidation point doesn't disqualify a different, farther one that's
    still realistic. Returns None -- meaning "don't take this trade" -- if
    no candidate falls in that band at all: either nothing exists beyond
    entry, everything beyond entry is farther than max_stop_dollars, or
    everything beyond entry is closer than min_stop_dollars. When a real
    level *is* within that band, target is always reward_risk_ratio x
    that level's actual distance.

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
    structure -- is what the evidence actually supports.

    Changed again 2026-07-04: previously, when no real level was within
    budget, the cap itself (max_stop_dollars converted to points) was
    used as the stop distance outright -- an arbitrary, structurally
    unjustified number. A real 7-day backtest's --verbose detail showed
    exactly this: both losing trades (2026-06-29, 2026-06-30) had no real
    level within $200 of entry and so defaulted straight to the full
    $200/100-point cap, while the two winning trades happened to have a
    real level just 8-31 points from entry, making their (correctly,
    proportionally smaller) 2:1 targets tiny by comparison -- $16.50 and
    $62.26 of wins couldn't offset two $200 losses. Rather than take a
    max-risk trade with no real invalidation point behind it, such setups
    are now skipped entirely by returning None; see strategy.py's
    WAIT_FILL handling for how a rejected anchor is excluded from being
    re-picked and the bot keeps hunting for a fresh one instead.

    Added a minimum 2026-07-04: a real 7-day backtest's --verbose detail
    across 13 trades showed stops under ~20 points (usually the
    opening-range box edge, which is often just where the breakout
    happened, not real structure) won only 1 of 7 times (14%), versus 3
    of 6 (50%) for trades with a wider, more genuine stop -- filtering
    the under-20-point group out entirely would have turned a 31% win
    rate / $192.50 net across 13 trades into a 50% win rate / $249.75 net
    across the remaining 6. A stop that tight is inside ordinary MNQ
    chop, not a real invalidation level, so it's now rejected the same
    way an out-of-budget stop is.

    Changed the selection itself 2026-07-04: previously, only the single
    nearest candidate was ever checked against the band, so if *that one*
    happened to be too close, the trade was skipped even when a second,
    farther candidate existed that would have cleared min_stop_dollars
    comfortably while still being well within max_stop_dollars. Since a
    trade needs multiple marked levels beyond entry (previous day, Asia,
    London, box) for this to matter, and the near-miss data motivating
    the floor above didn't distinguish "only candidate, too close" from
    "nearest candidate too close, but a farther one exists," this is a
    plausible source of some of the frequency lost to the floor -- fixed
    by picking the nearest candidate that clears the band, instead of
    checking only the nearest candidate overall. This is not the
    farthest-within-budget idea already tried and reverted above: it
    still prefers the nearest usable level, it just no longer lets one
    unrealistically-close level block a perfectly good farther one from
    ever being considered.)
    """
    max_stop_points = max_stop_dollars / (point_value * contracts)
    min_stop_points = min_stop_dollars / (point_value * contracts)

    if direction is Direction.LONG:
        distances = [entry_price - lvl for lvl in structural_levels if lvl < entry_price]
    else:
        distances = [lvl - entry_price for lvl in structural_levels if lvl > entry_price]

    # Nearest candidate whose distance actually falls in the realistic
    # band -- not the nearest candidate overall. A level just outside the
    # band (in either direction) doesn't disqualify a different, farther
    # real level that's still within budget; see revision history for why
    # this isn't the same as the "farthest within budget" idea already
    # tried and reverted (this still prefers nearest -- only among
    # candidates that clear the noise floor -- rather than preferring far
    # for its own sake).
    in_band = [d for d in distances if min_stop_points <= d <= max_stop_points]
    if not in_band:
        return None

    stop_points = min(in_band)
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
