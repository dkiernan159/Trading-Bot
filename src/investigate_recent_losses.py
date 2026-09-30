from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading. David asked "why are
we taking poor trades recently? Win rate is horrible" after 7 trades
since the 2026-09-23 account switch showed only a 28.6% win rate
(below the ~33.3% breakeven line at the current 2.0 reward:risk ratio),
concentrated in overnight trades using an FVG-based stop (1 win / 4
losses there, net -$675). He asked to investigate the individual
losing trades first, same approach used for the very first lost trade
(2026-09-23) and the overnight breakeven trade (2026-09-25) -- both
already understood in depth from that earlier work; this covers the
three new ones since (2026-09-29 07:41, 2026-09-30 00:11, 2026-09-30
05:21, all overnight, all FVG-stop losses).

For every real "stop" exit in trades.csv from the last DAYS days, prints
the actual 1-minute bars from just before entry through the stop-out,
then keeps walking forward past the exit to see whether price then
reversed back in the trade's original favorable direction (the stop
got tagged by short-term noise before the real move happened -- bad
luck) or kept going against it (the stop-out was warranted -- the
thesis was genuinely wrong).

Run on the VPS (needs .env credentials and network access to the
ProjectX Gateway), same as src/backtest.py:

    python -m src.investigate_recent_losses
"""

import csv
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.backtest import fetch_recent_bars
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import load_config
from src.models import Bar

DAYS = 8
POST_EXIT_WINDOW_MINUTES = 60
TRADES_CSV = Path("trades/trades.csv")


def _load_recent_stop_losses(days: int) -> list[dict]:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    rows = []
    with open(TRADES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if row["exit_reason"] != "stop":
                continue
            entry_time = datetime.fromisoformat(row["entry_time"])
            if entry_time < cutoff:
                continue
            rows.append(row)
    return rows


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

    losses = _load_recent_stop_losses(DAYS)
    print("")
    print(str(len(losses)) + " real stop-loss exit(s) in the last " + str(DAYS) + " days.")

    for row in losses:
        entry_time = datetime.fromisoformat(row["entry_time"])
        exit_time = datetime.fromisoformat(row["exit_time"])
        direction = row["direction"]
        entry_price = float(row["entry_price"])
        stop_price = float(row["stop_price"])

        print("")
        print("=" * 70)
        print(
            row["strategy"].upper() + " " + direction.upper()
            + "  entry=" + row["entry_price"] + " @ " + entry_time.strftime("%Y-%m-%d %H:%M")
            + "  stop=" + row["stop_price"] + " (" + row["stop_source"] + ")"
            + "  exit=" + row["exit_price"] + " @ " + exit_time.strftime("%H:%M")
            + "  pnl=$" + row["pnl_dollars"]
        )
        print("=" * 70)

        window_start = entry_time - timedelta(minutes=10)
        window_end = exit_time + timedelta(minutes=POST_EXIT_WINDOW_MINUTES)
        window_bars = [b for b in bars if window_start <= b.timestamp <= window_end]

        for b in window_bars:
            marker = ""
            if b.timestamp == entry_time:
                marker = "  <- ENTRY"
            elif entry_time < b.timestamp <= exit_time:
                marker = ""
            elif b.timestamp > exit_time:
                marker = "  (post-exit)"
            local_t = b.timestamp.astimezone(tz).strftime("%H:%M")
            print(
                "  " + local_t
                + "  o=" + str(b.open) + " h=" + str(b.high) + " l=" + str(b.low) + " c=" + str(b.close)
                + marker
            )

        post_exit_bars = [b for b in bars if b.timestamp > exit_time]
        cutoff = exit_time + timedelta(minutes=POST_EXIT_WINDOW_MINUTES)
        max_favorable_after_stop = 0.0
        for b in post_exit_bars:
            if b.timestamp > cutoff:
                break
            if direction == "long":
                favorable = b.high - entry_price
            else:
                favorable = entry_price - b.low
            max_favorable_after_stop = max(max_favorable_after_stop, favorable)

        stop_distance = abs(entry_price - stop_price)
        verdict = (
            "price ran BACK PAST the original entry in the favorable direction after "
            "stopping out -- looks like the stop got tagged by noise, thesis may have been right"
            if max_favorable_after_stop > stop_distance
            else "price never got back past entry in the favorable direction -- the stop-out looks warranted"
        )
        print("")
        print(
            "  Next " + str(POST_EXIT_WINDOW_MINUTES) + "min after stop-out, best it got back in "
            + direction + "'s favor: +" + str(round(max_favorable_after_stop, 2))
            + "pts (original stop distance was " + str(round(stop_distance, 2)) + "pts) -> " + verdict
        )


if __name__ == "__main__":
    main()
