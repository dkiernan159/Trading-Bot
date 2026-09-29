from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading. Two parts:

1. For every anchor the zone-confirmation layer blocked (zone_rejected /
   zone_timeout), replays real bars forward from the exact bar the pause
   began to see whether the entry/stop/target it would have used
   (captured at that moment -- see strategy.py's and
   overnight_strategy.py's WAIT_FILL pause block, and AnchorRecord's
   would_be_* fields) hit its stop or target first. Where no valid
   structural stop existed at pause time (would_be_stop_price is None),
   falls back to a directional-only read: max favorable vs. max adverse
   excursion over a fixed window, since there's no real stop/target to
   size a trade against -- not a formal backtested outcome, just
   "would this have gone the right way."

2. Tallies every anchor's outcome (not just zone-blocked ones) to show
   how often no_valid_stop happens across the whole window, separate
   from the zone question entirely.

live config.yaml disabled strategy.zones.enabled 2026-09-24 real trades
showed it net costly -- this script forces it back on for its own
replay only (never touches the live config file) so it can still
reproduce and learn from the historical pauses that happened while it
was live.

An earlier version of this script ran two full, independent backtests
(zones on vs. zones off) and tried to match trades between them by gap
bounds -- that's unsound: the moment the zones-off run takes its first
trade, the strategy sits IN_TRADE and stops hunting, so its whole
timeline diverges from the zones-on run from that point on, and most
later anchors never even get evaluated in the same way. Only 2 of 33
blocked anchors matched with that approach. This version avoids the
problem entirely: it runs the real, single zones-on backtest exactly
once, and for each blocked anchor just checks the *actual* subsequent
bars against a bracket already computed at the real pause moment --
no second diverging timeline needed.

Run on the VPS (needs .env credentials and network access to the
ProjectX Gateway), same as src/backtest.py:

    python -m src.compare_zones
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from src.backtest import fetch_recent_bars, run_backtest, run_overnight_backtest
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import load_config
from src.models import Bar, Direction

DAYS = 7
DIRECTIONAL_WINDOW_MINUTES = 60


def _resolve_outcome(direction: Direction, stop_price: float, target_price: float, bars_after: list[Bar]) -> str:
    """Walks the real bars forward from the pause point and reports which
    of stop/target is hit first, mirroring run_backtest's own
    hit_stop/hit_target convention (both hit the same bar -> stop first).
    Returns "WIN", "LOSS", or "UNRESOLVED" if neither is hit before the
    fetched history runs out."""
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


def _directional_excursion(direction: Direction, entry_price: float, bars_after: list[Bar]) -> tuple[float, float]:
    """No formal stop/target exists for these, so there's nothing to
    "win" or "lose" in dollar terms -- this just reports how far price
    moved in the anchor's favor vs. against it over a fixed window, as a
    plain directional signal (would the thesis have looked right)."""
    if not bars_after:
        return 0.0, 0.0
    cutoff = bars_after[0].timestamp + timedelta(minutes=DIRECTIONAL_WINDOW_MINUTES)
    max_favorable = 0.0
    max_adverse = 0.0
    for bar in bars_after:
        if bar.timestamp > cutoff:
            break
        if direction is Direction.LONG:
            favorable = bar.high - entry_price
            adverse = entry_price - bar.low
        else:
            favorable = entry_price - bar.low
            adverse = bar.high - entry_price
        max_favorable = max(max_favorable, favorable)
        max_adverse = max(max_adverse, adverse)
    return max_favorable, max_adverse


def main() -> None:
    cfg = load_config("config.yaml")
    # The live config disabled zones 2026-09-24 (real trades showed it
    # net costly) -- force it back on for this replay only so the
    # historical pauses that happened while it was live can still be
    # reproduced and learned from. Never writes back to config.yaml.
    cfg.strategy.zones.enabled = True

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

    point_value = cfg.instrument.point_value
    contracts = cfg.position_sizing.contract_size

    for label, run_fn in (("DAY", run_backtest), ("OVERNIGHT", run_overnight_backtest)):
        print("")
        print("=" * 70)
        print(label + " strategy")
        print("=" * 70)

        anchors: list = []
        run_fn(cfg, bars, anchor_history_out=anchors)

        print("")
        print("Every anchor's outcome (" + str(len(anchors)) + " total, zones or not):")
        counts: dict[str, int] = {}
        for a in anchors:
            counts[a.outcome] = counts.get(a.outcome, 0) + 1
        for outcome, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            pct = 100 * count / len(anchors) if anchors else 0
            print("  " + outcome.ljust(16) + str(count).rjust(4) + "  (" + str(round(pct)) + "%)")

        blocked = [a for a in anchors if a.outcome in ("zone_rejected", "zone_timeout")]
        print("")
        print("Zone-blocked anchors: " + str(len(blocked)))

        resolved = 0
        total_whatif_pnl = 0.0

        for a in blocked:
            prefix = (
                a.started_at.strftime("%Y-%m-%d %H:%M")
                + "  " + a.direction.value.upper().ljust(6)
                + " gap=" + str(round(a.gap_low, 2)) + "-" + str(round(a.gap_high, 2))
                + "  blocked_as=" + a.outcome.ljust(14)
            )

            if a.would_be_computed_at is None:
                print(prefix + "  no entry price captured at pause time -- can't evaluate at all")
                continue

            bars_after = [b for b in bars if b.timestamp > a.would_be_computed_at]

            if a.would_be_stop_price is None:
                mfe, mae = _directional_excursion(a.direction, a.would_be_entry_price, bars_after)
                verdict = "favorable" if mfe > mae else ("unfavorable" if mae > mfe else "flat")
                line = (
                    prefix
                    + "  entry=" + str(round(a.would_be_entry_price, 2))
                    + "  no valid stop -- directional only, next " + str(DIRECTIONAL_WINDOW_MINUTES) + "min: "
                    + "MFE=+" + str(round(mfe, 2)) + "pts  MAE=-" + str(round(mae, 2)) + "pts  -> " + verdict
                )
                print(line)
                continue

            outcome = _resolve_outcome(a.direction, a.would_be_stop_price, a.would_be_target_price, bars_after)
            outcome_str = outcome
            if outcome in ("WIN", "LOSS"):
                resolved += 1
                if a.direction is Direction.LONG:
                    pnl_points = (
                        (a.would_be_target_price - a.would_be_entry_price)
                        if outcome == "WIN"
                        else (a.would_be_stop_price - a.would_be_entry_price)
                    )
                else:
                    pnl_points = (
                        (a.would_be_entry_price - a.would_be_target_price)
                        if outcome == "WIN"
                        else (a.would_be_entry_price - a.would_be_stop_price)
                    )
                pnl_dollars = pnl_points * point_value * contracts
                total_whatif_pnl += pnl_dollars
                outcome_str = outcome + " " + ("+$" if pnl_dollars >= 0 else "-$") + str(round(abs(pnl_dollars), 2))

            line = (
                prefix
                + "  entry=" + str(round(a.would_be_entry_price, 2))
                + "  without_zone_gate: " + outcome_str
            )
            print(line)

        print("")
        print("Resolved " + str(resolved) + " of " + str(len(blocked)) + " blocked anchors with a real stop/target.")
        print("Total what-if P&L from those resolved trades: $" + str(round(total_whatif_pnl, 2)))


if __name__ == "__main__":
    main()
