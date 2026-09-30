from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading. David: "how many
trades that would have hit full take profit are we missing because
we're setting a break even take profit?"

Reads every real "breakeven" exit across trades.csv, the archived
pre-account-switch trades-combine-archive.csv, and the older
trades.csv.bak-before-aug5-7-backfill (deduplicated by entry_time/
entry_price/direction, since the same August trades appear in both the
archive and the older backup), then fetches real bars forward from each
breakeven exit to check whether price actually went on to reach the
original, full target_price -- and if so, how long that took and how
much extra $ was left on the table by exiting at breakeven instead of
riding to target.

Run on the VPS (needs .env credentials and network access to the
ProjectX Gateway), same as src/backtest.py:

    python -m src.analyze_breakeven_giveback
"""

import csv
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path

from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import load_config

LOOKAHEAD_HOURS = 8
TRADE_FILES = [
    "trades/trades.csv",
    "trades/trades-combine-archive.csv",
    "trades/trades.csv.bak-before-aug5-7-backfill",
]


def _load_breakeven_trades() -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    rows: list[dict] = []
    for path in TRADE_FILES:
        p = Path(path)
        if not p.exists():
            continue
        with open(p, newline="") as f:
            for row in csv.DictReader(f):
                if row["exit_reason"] != "breakeven":
                    continue
                key = (row["entry_time"], row["entry_price"], row["direction"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    rows.sort(key=lambda r: r["entry_time"])
    return rows


def main() -> None:
    cfg = load_config("config.yaml")
    broker = ProjectXGatewayBroker(
        base_url=cfg.broker.base_url,
        realtime_base_url=cfg.broker.realtime_base_url,
        dry_run=True,
    )
    broker.connect()

    trades = _load_breakeven_trades()
    print(str(len(trades)) + " real breakeven exit(s) found across all trade files.")

    would_have_hit_target = 0
    total_missed_dollars = 0.0

    for i, row in enumerate(trades):
        if i > 0:
            time_module.sleep(0.25)

        exit_time = datetime.fromisoformat(row["exit_time"])
        direction = row["direction"]
        exit_price = float(row["exit_price"])
        target_price = float(row["target_price"])
        contracts = int(row["contracts"])

        window_end = exit_time + timedelta(hours=LOOKAHEAD_HOURS)
        bars = broker.fetch_historical_bars(cfg.instrument.symbol, exit_time, window_end)

        hit_bar = None
        for b in bars:
            if b.timestamp <= exit_time:
                continue
            touched = b.high >= target_price if direction == "long" else b.low <= target_price
            if touched:
                hit_bar = b
                break

        prefix = (
            row["strategy"].upper() + " " + direction.upper()
            + "  entry=" + row["entry_price"] + " @ " + row["entry_time"]
            + "  breakeven exit=" + row["exit_price"] + " @ " + row["exit_time"]
            + "  target=" + row["target_price"]
        )

        if hit_bar is not None:
            would_have_hit_target += 1
            missed_points = (target_price - exit_price) if direction == "long" else (exit_price - target_price)
            missed_dollars = missed_points * cfg.instrument.point_value * contracts
            total_missed_dollars += missed_dollars
            wait = hit_bar.timestamp - exit_time
            print(
                prefix
                + "  -> WOULD HAVE HIT TARGET " + str(wait) + " later, missed +$" + str(round(missed_dollars, 2))
            )
        else:
            print(prefix + "  -> never reached target within " + str(LOOKAHEAD_HOURS) + "h -- breakeven exit looks like the right call")

    print("")
    print(
        str(would_have_hit_target) + " of " + str(len(trades))
        + " breakeven exits would have gone on to hit full target within " + str(LOOKAHEAD_HOURS) + "h."
    )
    print("Total upside missed by exiting at breakeven instead of riding to target: $" + str(round(total_missed_dollars, 2)))


if __name__ == "__main__":
    main()
