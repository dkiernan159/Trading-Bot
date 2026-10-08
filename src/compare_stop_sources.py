from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading. David: "I feel like
the stop parameters are too strict... we should also use basic
break-of-structure analysis to determine a good stop... I'd rather take
the trade and get stopped at a reasonable point that makes logic sense
than never take it at all." Then, before building anything: "which of
these options would make money if we backtested over the trades?"

Important context this script exists to test against: risk.py's own
history already documents two earlier, separate attempts at exactly
this trade-off (take the trade anyway when no qualifying FVG/swing stop
exists) -- both tried, both reverted after real live losses, the second
time on David's own explicit instruction to revert ("if the trade isn't
strong and doesn't have a good stop loss point below a break of
structure or resistance level then we shouldn't take it"). This script
doesn't re-litigate that from memory -- it checks two concrete looser
break-of-structure definitions against every real no_valid_stop anchor
in recent history and reports actual win/loss/$ outcomes, so the
decision is made on fresh evidence, not on repeating a tried idea.

Two candidate methods tested, independently reconstructed from real
bars (no changes to any strategy class):

  A. Looser fractal pivot: swing_points.py's existing SwingPointTracker
     uses PIVOT_WIDTH=2 (a bar must beat 2 bars on each side -- a 5-bar
     pivot). Method A uses PIVOT_WIDTH=1 (a 3-bar pivot) instead, so
     swing points confirm faster and more often.

  B. Plain N-bar extreme, no fractal confirmation at all: the highest
     high / lowest low over the last LOOKBACK_BARS bars before the
     no-valid-stop moment. ASSUMPTION: LOOKBACK_BARS=10, untested,
     picked as a simple default.

For every no_valid_stop anchor (both strategies), recomputes the real
entry price (pure function of the anchor's own gap bounds, same formula
each strategy's own _entry_price uses), finds what each method's stop
would have been as of that exact bar, validates it against the real
$min-$max stop budget via compute_stop_target (unchanged), and -- if
valid -- replays real subsequent bars to see whether that trade would
have hit its stop or target first.

Run on the VPS (needs .env credentials and network access to the
ProjectX Gateway), same as src/backtest.py:

    python -m src.compare_stop_sources
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from src.backtest import fetch_recent_bars, run_backtest, run_overnight_backtest
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import BotConfig, load_config
from src.models import Bar, Direction
from src.risk import compute_stop_target, round_to_tick
from src.strategy import AnchorRecord

DAYS = 14
LOOKBACK_BARS = 10


def _entry_price(cfg: BotConfig, direction: Direction, gap_low: float, gap_high: float) -> float:
    pct = cfg.strategy.entry_retracement_pct
    width = gap_high - gap_low
    price = gap_high - pct * width if direction is Direction.LONG else gap_low + pct * width
    return round_to_tick(price, cfg.instrument.tick_size)


def _fractal_swing_as_of(bars: list[Bar], as_of, pivot_width: int) -> tuple[float | None, float | None]:
    """Same fractal rule as swing_points.py's SwingPointTracker, just with
    a configurable width and replayed independently over bars up to and
    including as_of (no lookahead -- a candidate's confirmation requires
    pivot_width bars after it, which are themselves <= as_of)."""
    relevant = [b for b in bars if b.timestamp <= as_of]
    w = pivot_width
    high = low = None
    for i in range(w, len(relevant) - w):
        window = relevant[i - w : i + w + 1]
        candidate = window[w]
        others = window[:w] + window[w + 1 :]
        if all(candidate.high > o.high for o in others):
            high = candidate.high
        if all(candidate.low < o.low for o in others):
            low = candidate.low
    return high, low


def _recent_extreme_as_of(bars: list[Bar], as_of, lookback: int) -> tuple[float | None, float | None]:
    relevant = [b for b in bars if b.timestamp <= as_of]
    window = relevant[-lookback:]
    if not window:
        return None, None
    return max(b.high for b in window), min(b.low for b in window)


def _resolve_outcome(direction: Direction, stop_price: float, target_price: float, bars_after: list[Bar]) -> str:
    for bar in bars_after:
        if direction is Direction.LONG:
            hit_stop = bar.low <= stop_price
            hit_target = bar.high >= target_price
        else:
            hit_stop = bar.high >= stop_price
            hit_target = bar.low <= target_price
        if hit_stop or hit_target:
            return "LOSS" if hit_stop else "WIN"
    return "UNRESOLVED"


def _evaluate_method(
    label: str,
    anchor: AnchorRecord,
    entry: float,
    swing_high: float | None,
    swing_low: float | None,
    cfg: BotConfig,
    bars: list[Bar],
) -> str:
    direction = anchor.direction
    if direction is Direction.LONG:
        stop_price = swing_low if swing_low is not None and swing_low < entry else None
    else:
        stop_price = swing_high if swing_high is not None and swing_high > entry else None

    bracket = compute_stop_target(
        direction=direction,
        entry_price=entry,
        stop_price=stop_price,
        max_stop_dollars=cfg.strategy.max_stop_dollars,
        min_stop_dollars=cfg.strategy.min_stop_dollars,
        point_value=cfg.instrument.point_value,
        contracts=cfg.position_sizing.contract_size,
        reward_risk_ratio=cfg.strategy.reward_risk_ratio,
        tick_size=cfg.instrument.tick_size,
    )
    if bracket is None:
        return label + ": still no valid stop"

    bars_after = [b for b in bars if b.timestamp > anchor.ended_at]
    outcome = _resolve_outcome(direction, bracket.stop_price, bracket.target_price, bars_after)
    if outcome == "UNRESOLVED":
        return label + ": stop=" + str(round(bracket.stop_price, 2)) + " UNRESOLVED (ran off the end of fetched history)"

    point_value = cfg.instrument.point_value
    contracts = cfg.position_sizing.contract_size
    if direction is Direction.LONG:
        pnl_points = (bracket.target_price - entry) if outcome == "WIN" else (bracket.stop_price - entry)
    else:
        pnl_points = (entry - bracket.target_price) if outcome == "WIN" else (entry - bracket.stop_price)
    pnl_dollars = pnl_points * point_value * contracts
    return (
        label + ": stop=" + str(round(bracket.stop_price, 2))
        + " " + outcome + " " + ("+$" if pnl_dollars >= 0 else "-$") + str(round(abs(pnl_dollars), 2))
    ), pnl_dollars, outcome


def main() -> None:
    cfg = load_config("config.yaml")
    tz = ZoneInfo(cfg.session.timezone)
    broker = ProjectXGatewayBroker(
        base_url=cfg.broker.base_url,
        realtime_base_url=cfg.broker.realtime_base_url,
        dry_run=True,
    )
    broker.connect()
    print("Fetching " + str(DAYS) + " days of history...")
    bars = fetch_recent_bars(broker, cfg.instrument.symbol, tz, DAYS)
    print("Fetched " + str(len(bars)) + " bars.")

    for label, run_fn in (("DAY", run_backtest), ("OVERNIGHT", run_overnight_backtest)):
        print("")
        print("=" * 70)
        print(label + " strategy")
        print("=" * 70)

        anchors: list = []
        run_fn(cfg, bars, anchor_history_out=anchors)
        no_stop = [a for a in anchors if a.outcome == "no_valid_stop"]
        print("")
        print("no_valid_stop anchors: " + str(len(no_stop)))

        totals = {"A (3-bar pivot)": [0, 0.0], "B (10-bar extreme)": [0, 0.0]}

        for a in no_stop:
            entry = _entry_price(cfg, a.direction, a.gap_low, a.gap_high)
            prefix = (
                a.started_at.strftime("%Y-%m-%d %H:%M")
                + "  " + a.direction.value.upper().ljust(6)
                + " gap=" + str(round(a.gap_low, 2)) + "-" + str(round(a.gap_high, 2))
                + "  entry=" + str(round(entry, 2))
            )
            print(prefix)

            high_a, low_a = _fractal_swing_as_of(bars, a.ended_at, pivot_width=1)
            result_a = _evaluate_method("A (3-bar pivot)", a, entry, high_a, low_a, cfg, bars)
            if isinstance(result_a, tuple):
                line, pnl, outcome = result_a
                print("    " + line)
                if outcome in ("WIN", "LOSS"):
                    totals["A (3-bar pivot)"][0] += 1
                    totals["A (3-bar pivot)"][1] += pnl
            else:
                print("    " + result_a)

            high_b, low_b = _recent_extreme_as_of(bars, a.ended_at, LOOKBACK_BARS)
            result_b = _evaluate_method("B (10-bar extreme)", a, entry, high_b, low_b, cfg, bars)
            if isinstance(result_b, tuple):
                line, pnl, outcome = result_b
                print("    " + line)
                if outcome in ("WIN", "LOSS"):
                    totals["B (10-bar extreme)"][0] += 1
                    totals["B (10-bar extreme)"][1] += pnl
            else:
                print("    " + result_b)

        print("")
        print("Summary for " + label + ":")
        for method, (count, pnl) in totals.items():
            print("  " + method + ": " + str(count) + " resolved trade(s), net $" + str(round(pnl, 2)))


if __name__ == "__main__":
    main()
