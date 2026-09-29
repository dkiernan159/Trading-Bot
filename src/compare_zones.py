from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading: for every anchor the
zone-confirmation layer blocked (zone_rejected / zone_timeout), replays
real bars forward from the exact bar the pause began to see whether the
entry/stop/target it would have used (captured at that moment -- see
strategy.py's and overnight_strategy.py's WAIT_FILL pause block, and
AnchorRecord's would_be_* fields) hit its stop or target first. Built
2026-09-29.

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

from zoneinfo import ZoneInfo

from src.backtest import fetch_recent_bars, run_backtest, run_overnight_backtest
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import load_config
from src.models import Direction

DAYS = 7


def _resolve_outcome(direction: Direction, stop_price: float, target_price: float, bars_after) -> str:
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

    point_value = cfg.instrument.point_value
    contracts = cfg.position_sizing.contract_size

    for label, run_fn in (("DAY", run_backtest), ("OVERNIGHT", run_overnight_backtest)):
        print("")
        print("=" * 70)
        print(label + " strategy")
        print("=" * 70)

        anchors: list = []
        run_fn(cfg, bars, anchor_history_out=anchors)

        blocked = [a for a in anchors if a.outcome in ("zone_rejected", "zone_timeout")]
        print("")
        print("Zone-blocked anchors: " + str(len(blocked)))

        resolved = 0
        total_whatif_pnl = 0.0

        for a in blocked:
            if a.would_be_stop_price is None or a.would_be_computed_at is None:
                line = (
                    a.started_at.strftime("%Y-%m-%d %H:%M")
                    + "  " + a.direction.value.upper().ljust(6)
                    + " gap=" + str(round(a.gap_low, 2)) + "-" + str(round(a.gap_high, 2))
                    + "  blocked_as=" + a.outcome.ljust(14)
                    + "  no valid stop existed at pause time -- can't evaluate"
                )
                print(line)
                continue

            bars_after = [b for b in bars if b.timestamp > a.would_be_computed_at]
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
                a.started_at.strftime("%Y-%m-%d %H:%M")
                + "  " + a.direction.value.upper().ljust(6)
                + " gap=" + str(round(a.gap_low, 2)) + "-" + str(round(a.gap_high, 2))
                + "  blocked_as=" + a.outcome.ljust(14)
                + "  entry=" + str(round(a.would_be_entry_price, 2))
                + "  without_zone_gate: " + outcome_str
            )
            print(line)

        print("")
        print("Resolved " + str(resolved) + " of " + str(len(blocked)) + " blocked anchors (rest had no valid stop, or ran off the end of fetched history).")
        print("Total what-if P&L from those resolved trades: $" + str(round(total_whatif_pnl, 2)))


if __name__ == "__main__":
    main()
