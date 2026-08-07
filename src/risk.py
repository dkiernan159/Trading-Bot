from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.config import RiskLimitsConfig
from src.fvg import FairValueGap
from src.models import Direction


@dataclass
class BracketLevels:
    stop_price: float
    target_price: float
    stop_points: float
    target_points: float


@dataclass
class StopCandidate:
    """A resolved stop price plus which rule produced it -- diagnostic
    only (see backtest.py's --verbose trade detail), so a hypothesis like
    "the FVGs backing these stops aren't strong enough" can actually be
    checked against real data instead of guessed at. `fvg_size` is
    `gap_high - gap_low` of the FVG used, None when source is "swing"."""

    price: float
    source: str  # "fvg" | "swing"
    fvg_gap_low: float | None = None
    fvg_gap_high: float | None = None

    @property
    def fvg_size(self) -> float | None:
        if self.fvg_gap_low is None or self.fvg_gap_high is None:
            return None
        return self.fvg_gap_high - self.fvg_gap_low


def round_to_tick(price: float, tick_size: float) -> float:
    """Confirmed live 2026-08-07: entry_price (an FVG retracement --
    gap_high - pct*width) and target_price (entry_price +/- stop_points*
    reward_risk_ratio) are both plain float arithmetic with no rounding,
    so they routinely land on sub-tick values (e.g. a gap 29568.75-29578.00
    at the exact midpoint retraces to 29573.375 -- not a multiple of MNQ's
    0.25 tick_size). The gateway rejects these outright with "Invalid
    limit price. Price is not aligned to tick size," caught by
    _enter_trade's existing try/except and silently treated as an
    ordinary "not filled" case -- indistinguishable from a normal missed
    fill. Real bot.log tracebacks showed this had been happening since at
    least 2026-08-04 (predates the 2026-08-06 contract-size scale-up, so
    it isn't related to that change) -- roughly half of every real
    "anchor ended (filled)" signal in the affected window, since a real
    gap's exact midpoint is only tick-aligned when its width happens to
    be an even number of ticks. An exact half-tick tie is rare (entry is
    a fraction of a real gap's width, not a value chosen to land exactly
    on one) and Python's round() ties-to-even behavior is an
    unremarkable, defensible choice either way -- not worth a custom
    tie-break for this."""
    return round(price / tick_size) * tick_size


def find_structural_stop_price(
    direction: Direction,
    entry_price: float,
    fvg_candidates: list[FairValueGap],
    swing_high: float | None,
    swing_low: float | None,
    prefer_swing: bool = True,
) -> StopCandidate | None:
    """Where the stop goes: either the outer edge of the nearest strong 5m
    FVG sitting on the stop side of entry (below entry for a LONG, above
    for a SHORT -- the same "strong" 5m FVGs already used for anchor
    selection, fvg_candidates being the caller's
    fvg_detector_5m.unmitigated_in_direction(direction) pool, same
    direction as the trade: a LONG-direction gap is a bullish/support gap,
    which is what should sit *below* a long entry), or the most recent
    1-minute break-of-structure swing point on that same side (see
    swing_points.py) -- whichever `prefer_swing` says to try first, with
    the other used as a fallback if the first doesn't qualify. Returns
    None if neither exists, meaning "no real invalidation point behind
    this entry at all" -- skip the trade (see compute_stop_target).

    Originally (2026-07-08, at the user's explicit correction replacing
    the previous-day/Asia/London/box-level approach -- see
    compute_stop_target's revision history) FVG was always tried first.
    **`prefer_swing` added the same day, later**, after a real 32-trade
    overnight backtest's --verbose detail (StopCandidate's
    stop_source/stop_fvg_size fields, added earlier that day) showed
    swing-based stops winning 50% (net +$665 across 12 trades, +$55/trade)
    versus FVG-based stops winning only 35% (net +$248 across 20 trades,
    +$12/trade) there -- and FVG size didn't predict the difference (the
    worst bucket was mid-sized 20-30pt gaps, not the smallest ones), so
    it's the source itself, not gap size, that discriminates. But
    re-running the *day* (opening-range breakout) strategy's own 30-day
    backtest with swing preferred made it measurably worse (10->25 trades,
    30%->24% win rate, -$199.75->-$424.75 net): the FVG-first rule had
    been usefully filtering day-strategy entries down to ones with a real
    support/resistance gap nearby, and swing-first let in a lot of
    marginal setups that used to get skipped as no_valid_stop. So each
    strategy passes its own `prefer_swing` based on its own real data
    (see strategy.py: prefer_swing=False; overnight_strategy.py:
    prefer_swing=True) rather than sharing one global priority order.

    **Cap fallback added 2026-07-10, then reverted 2026-07-14:** briefly
    fell back to the max_stop_dollars budget as a last-resort stop
    distance (`source="cap"`) instead of returning None, at the user's
    explicit instruction and against known contrary evidence (this same
    idea had already been tried once, under the old level-based design,
    and reverted for losing -- see compute_stop_target's revision
    history). It was flagged clearly before making the change; the user
    chose to proceed anyway to see if it held up under the newer
    swing/FVG-based design. Within days, a real live trade whose stop
    landed on an exact round 100.00-point distance (precisely
    max_stop_dollars/point_value/contracts, not a number any real
    structural level would coincidentally produce) lost -$200 -- the same
    failure shape as before. The user reverted it immediately on seeing
    that: "if the trade isn't strong and doesn't have a good stop loss
    point below a break of structure or resistance level then we
    shouldn't take it." Back to returning None when neither a qualifying
    FVG nor swing point exists -- this rule has now failed the same way
    twice, under two different anchor-selection designs, so it should not
    be tried a third time without a fundamentally different justification
    than "let more trades through."""
    if direction is Direction.LONG:
        swing_candidate = (
            StopCandidate(price=swing_low, source="swing")
            if swing_low is not None and swing_low < entry_price
            else None
        )
        below = [g for g in fvg_candidates if g.gap_high < entry_price]
        fvg_candidate = None
        if below:
            nearest = max(below, key=lambda g: g.gap_high)
            fvg_candidate = StopCandidate(
                price=nearest.gap_low, source="fvg", fvg_gap_low=nearest.gap_low, fvg_gap_high=nearest.gap_high
            )
    else:
        swing_candidate = (
            StopCandidate(price=swing_high, source="swing")
            if swing_high is not None and swing_high > entry_price
            else None
        )
        above = [g for g in fvg_candidates if g.gap_low > entry_price]
        fvg_candidate = None
        if above:
            nearest = min(above, key=lambda g: g.gap_low)
            fvg_candidate = StopCandidate(
                price=nearest.gap_high, source="fvg", fvg_gap_low=nearest.gap_low, fvg_gap_high=nearest.gap_high
            )

    first, second = (swing_candidate, fvg_candidate) if prefer_swing else (fvg_candidate, swing_candidate)
    return first if first is not None else second


def compute_stop_target(
    direction: Direction,
    entry_price: float,
    stop_price: float | None,
    max_stop_dollars: float,
    min_stop_dollars: float,
    point_value: float,
    contracts: int,
    reward_risk_ratio: float,
    tick_size: float,
) -> BracketLevels | None:
    """Validates a candidate stop_price (see find_structural_stop_price)
    against the $min-$max stop budget and computes the target at
    reward_risk_ratio x the resulting distance. Returns None -- meaning
    "don't take this trade" -- if stop_price is None (no real invalidation
    point found at all), if the distance is farther out than
    max_stop_dollars allows, or if it's closer than min_stop_dollars:
    either way, a stop this far or this close isn't a genuine, reasonably-
    sized invalidation point, so there's nothing sound to size a trade
    against.

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

    Briefly changed the selection itself again 2026-07-04: tried picking
    the nearest candidate that clears the band, instead of checking only
    the nearest candidate overall, so a too-close level (usually the box
    edge) wouldn't block a farther, still-in-budget one from ever being
    considered. Reverted the same day: a real 30-day backtest's
    `--verbose` detail showed this recovered exactly 5 trades (matching
    the drop in "no_valid_stop" near-misses), and all 5 went 0-for-5
    (2026-06-11, -12, -17, -19, -29), every one of them landing on an
    Asia or London session level reached by skipping a tighter box-edge
    candidate. Meanwhile the 6 trades that didn't need to skip anything
    held their existing 50% win rate. This is the same failure shape as
    the farthest-within-budget experiment above, just reached by a
    narrower path (only kicking in when the nearest level fails the
    floor, rather than for every trade) -- reaching past the nearest
    level for a "more valid-looking" one keeps producing worse trades,
    not better ones, so nearest-only stands: if the single nearest level
    doesn't clear the band, the trade is skipped, full stop, rather than
    hunting for a farther substitute.)

    Replaced entirely 2026-07-08 at the user's explicit correction: the
    marked previous-day/Asia/London high-low and opening-range box edges
    above are no longer used as stop candidates at all. The user's own
    read: "the stop should be set at either below or above respectively
    the nearest strong 5 minute FVG based on if the entry is long or
    short. Or if no strong 5 min fvg exists, the stop should be slightly
    above the nearest break of structure, which means the most recent
    high/low respectively for a long or short entry on the chart." See
    find_structural_stop_price for that selection; this function's own
    job shrank to just validating whatever stop_price it's given against
    the $min-$max budget and computing the target -- it no longer
    searches a candidate list itself.

    Note (2026-07-10, reverted 2026-07-14): the "skipped entirely"
    behavior described just above (2026-07-04, when no real level was
    within budget) was briefly reversed -- find_structural_stop_price
    stopped returning None, falling back to the max_stop_dollars cap as a
    last resort instead of letting the caller skip the trade. Reverted
    four days later after a real live trade with an exact round
    100-point (cap-distance) stop lost $200, repeating the same failure
    this project had already seen once before under the old level-based
    design. find_structural_stop_price returns None again in the
    "nothing structural exists" case; this function's own behavior here
    was never changed either time -- see find_structural_stop_price's own
    docstring for the full history.
    """
    if stop_price is None:
        return None

    # Confirmed live 2026-08-07 (see round_to_tick's own docstring):
    # round stop_price to the exchange's tick grid before computing
    # anything from it, so stop_points/target_points/target_price below
    # are all internally consistent with the actual price sent to the
    # broker -- stop_price is real-market-derived and should already be
    # aligned, but rounding defensively costs nothing.
    stop_price = round_to_tick(stop_price, tick_size)

    max_stop_points = max_stop_dollars / (point_value * contracts)
    min_stop_points = min_stop_dollars / (point_value * contracts)
    stop_points = abs(entry_price - stop_price)

    if not (min_stop_points <= stop_points <= max_stop_points):
        return None

    target_points = stop_points * reward_risk_ratio

    if direction is Direction.LONG:
        target_price = entry_price + target_points
    else:
        target_price = entry_price - target_points

    # target_price is entry_price +/- a floating-point points distance, so
    # it routinely lands off the tick grid even when entry_price and
    # stop_price are both clean -- the gateway rejects an unaligned LIMIT
    # price outright, and that failure was being silently swallowed as an
    # ordinary "not filled" case, costing roughly half of every real entry
    # signal in the affected window (see round_to_tick's own docstring).
    target_price = round_to_tick(target_price, tick_size)

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
        # max_trades_per_day removed 2026-07-14 at the user's explicit
        # instruction -- None means no cap on trade count; the $ loss
        # limit below and kill_switch above remain the real safety net.
        if self.cfg.max_trades_per_day is not None and self.trades_today >= self.cfg.max_trades_per_day:
            return False
        if self.realized_pnl_today <= -abs(self.cfg.max_daily_loss_dollars):
            return False
        return True
