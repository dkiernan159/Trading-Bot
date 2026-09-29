from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading: replays real recent
history twice -- once with strategy.zones.enabled as configured, once
forced off -- to see what would have happened to every anchor the
zone-confirmation layer blocked (zone_rejected / zone_timeout) if it had
been allowed to fill instead. Built 2026-09-29 after real logs showed 16
anchors blocked by zones since the feature deployed on 09-24, with 0 of
them ever confirmed and let through -- this answers whether those 16
would have won or lost if zones had never paused them.

Run on the VPS (needs .env credentials and network access to the
ProjectX Gateway), same as src/backtest.py:

    python -m src.compare_zones
"""

from zoneinfo import ZoneInfo

from src.backtest import fetch_recent_bars, run_backtest, run_overnight_backtest
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import load_config

DAYS = 7


def main() -> None:
    cfg_on = load_config("config.yaml")
    cfg_off = load_config("config.yaml")
    cfg_off.strategy.zones.enabled = False

    tz = ZoneInfo(cfg_on.session.timezone)
    broker = ProjectXGatewayBroker(
        base_url=cfg_on.broker.base_url,
        realtime_base_url=cfg_on.broker.realtime_base_url,
        dry_run=True,
    )
    broker.connect()
    print("Fetching " + str(DAYS) + " days of history...")
    bars = fetch_recent_bars(broker, cfg_on.instrument.symbol, tz, DAYS)
    print("Fetched " + str(len(bars)) + " bars.")

    point_value = cfg_on.instrument.point_value
    contracts = cfg_on.position_sizing.contract_size

    for label, run_fn in (("DAY", run_backtest), ("OVERNIGHT", run_overnight_backtest)):
        print("")
        print("=" * 70)
        print(label + " strategy")
        print("=" * 70)

        anchors_on: list = []
        run_fn(cfg_on, bars, anchor_history_out=anchors_on)

        anchors_off: list = []
        results_off = run_fn(cfg_off, bars, anchor_history_out=anchors_off)

        blocked = [a for a in anchors_on if a.outcome in ("zone_rejected", "zone_timeout")]
        print("")
        print("Zone-blocked anchors (zones ON): " + str(len(blocked)))

        off_by_key = {}
        for t in results_off:
            key = (t["date"], t["direction"], round(t["anchor_gap_low"], 2), round(t["anchor_gap_high"], 2))
            off_by_key[key] = t

        matched = 0
        total_whatif_pnl = 0.0

        for a in blocked:
            day = a.started_at.astimezone(tz).date()
            key = (day, a.direction.value, round(a.gap_low, 2), round(a.gap_high, 2))
            t = off_by_key.get(key)
            outcome_str = "no matching zones-off trade (setup resolved differently without the pause)"
            if t is not None:
                matched += 1
                if t["direction"] == "long":
                    pnl_points = (t["target_price"] - t["entry_price"]) if t["won"] else (t["stop_price"] - t["entry_price"])
                else:
                    pnl_points = (t["entry_price"] - t["target_price"]) if t["won"] else (t["entry_price"] - t["stop_price"])
                pnl_dollars = pnl_points * point_value * contracts
                total_whatif_pnl += pnl_dollars
                outcome_str = ("WIN +$" if t["won"] else "LOSS -$") + str(round(abs(pnl_dollars), 2))
            line = (
                a.started_at.strftime("%Y-%m-%d %H:%M")
                + "  " + a.direction.value.upper().ljust(6)
                + " gap=" + str(round(a.gap_low, 2)) + "-" + str(round(a.gap_high, 2))
                + "  blocked_as=" + a.outcome.ljust(14)
                + "  without_zone_gate: " + outcome_str
            )
            print(line)

        print("")
        print("Matched " + str(matched) + " of " + str(len(blocked)) + " blocked anchors to a zones-off trade.")
        print("Total what-if P&L from those matched trades: $" + str(round(total_whatif_pnl, 2)))


if __name__ == "__main__":
    main()
