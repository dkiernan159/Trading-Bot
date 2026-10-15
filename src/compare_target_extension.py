from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading. David: "if the
movement is strong and theres a clear high or low to be retested on the
15 minute chart then we should set TP there I dont want a bunch of
small wins from this change" -- refined to: only ever EXTEND the normal
2:1 reward:risk target further out when a qualifying 15m zone (same
ZoneTracker already built for the now-disabled zone-confirmation
feature, see zones.py) sits beyond it in the trade's favorable
direction; never pull target in closer. Chosen specifically because it
can never manufacture a small win.

But extending a target doesn't touch the stop -- so for any trade that
actually WON at the original target, pushing the exit farther out risks
giving price room to reverse and run all the way back to the (unchanged)
original stop, turning a real win into a real loss. That is the same
shape of risk breakeven already demonstrated costs real money
(see analyze_breakeven_giveback.py -- $2,299.50 given up, 70% giveback
rate), just pointed in the opposite direction (over-extending instead
of cutting short). This script checks that real trade-off directly:
for every real historical trade that won at the current 2:1 target,
replays an independent ZoneTracker over the real bars up to entry time
(no lookahead -- zones are built only from confirmed pivots as of that
moment) to see whether a qualifying zone existed beyond the original
target, and if so, replays real subsequent price to see whether
extending to it would have produced a bigger win or flipped a real win
into a loss.

Run on the VPS (needs .env credentials and network access to the
ProjectX Gateway), same as src/backtest.py:

    python -m src.compare_target_extension
"""

from zoneinfo import ZoneInfo

from src.backtest import fetch_recent_bars, run_backtest, run_overnight_backtest
from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import BotConfig, load_config
from src.models import Bar, Direction
from src.zones import Zone, ZoneTracker

DAYS = 30
# ASSUMPTION: a small buffer so the extended target sits just below/above
# the zone rather than exactly at it (mirrors breakeven's buffer_dollars
# in spirit -- a real exit point, not a price that only a perfect wick
# would ever touch). Expressed in points, not $, since it's about where
# the order rests, not account risk.
TARGET_BUFFER_POINTS = 2.0


def _build_zone_tracker_as_of(cfg: BotConfig, tz: ZoneInfo, bars: list[Bar], as_of) -> ZoneTracker:
    """Independently reconstructs zone state from real bars up to (and
    including) as_of -- no lookahead, since ZoneTracker only ever confirms
    a pivot once bars after it exist, same guarantee as swing_points.py."""
    tracker = ZoneTracker(cfg.strategy.zones, tz)
    for b in bars:
        if b.timestamp > as_of:
            break
        tracker.add_bar(b)
    return tracker


def _nearest_qualifying_zone_beyond(tracker: ZoneTracker, direction: Direction, beyond_price: float) -> Zone | None:
    """Unlike ZoneTracker.opposing_zone (tight tolerance_points proximity
    gate, built for "is price testing a zone right now"), this searches
    for the nearest *qualifying* zone that sits beyond beyond_price in
    the trade's favorable direction -- i.e. a real target candidate
    farther out, not a nearby obstacle."""
    zones = tracker.resistance_zones if direction is Direction.LONG else tracker.support_zones
    candidates = [
        z
        for z in zones
        if z.touch_count >= tracker.cfg.min_touches
        and (z.price > beyond_price if direction is Direction.LONG else z.price < beyond_price)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda z: abs(z.price - beyond_price))


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

        results = run_fn(cfg, bars)
        wins = [t for t in results if t["won"]]
        print("")
        print("Real wins at the current 2:1 target: " + str(len(wins)) + " (of " + str(len(results)) + " total trades)")

        had_extension = 0
        bigger_win = 0
        flipped_to_loss = 0
        unresolved = 0
        net_delta = 0.0

        for t in wins:
            direction = Direction.LONG if t["direction"] == "long" else Direction.SHORT
            entry = t["entry_price"]
            original_target = t["target_price"]
            original_stop = t["stop_price"]

            tracker = _build_zone_tracker_as_of(cfg, tz, bars, t["entry_time"])
            zone = _nearest_qualifying_zone_beyond(tracker, direction, original_target)

            prefix = (
                t["entry_time"].strftime("%Y-%m-%d %H:%M")
                + "  " + t["direction"].upper().ljust(6)
                + " entry=" + str(round(entry, 2))
                + " orig_target=" + str(round(original_target, 2))
            )

            if zone is None:
                continue

            had_extension += 1
            extended_target = (
                round(zone.price - TARGET_BUFFER_POINTS, 2)
                if direction is Direction.LONG
                else round(zone.price + TARGET_BUFFER_POINTS, 2)
            )

            bars_after = [b for b in bars if b.timestamp > t["entry_time"]]
            outcome = None
            for b in bars_after:
                if direction is Direction.LONG:
                    hit_stop = b.low <= original_stop
                    hit_extended_target = b.high >= extended_target
                else:
                    hit_stop = b.high >= original_stop
                    hit_extended_target = b.low <= extended_target
                if hit_stop or hit_extended_target:
                    outcome = "LOSS" if hit_stop else "BIGGER_WIN"
                    break

            if outcome == "BIGGER_WIN":
                bigger_win += 1
                extra_points = (
                    (extended_target - original_target)
                    if direction is Direction.LONG
                    else (original_target - extended_target)
                )
                extra_dollars = extra_points * point_value * contracts
                net_delta += extra_dollars
                print(prefix + "  zone=" + str(round(zone.price, 2)) + " extended_target=" + str(extended_target) + "  -> BIGGER WIN +$" + str(round(extra_dollars, 2)) + " extra")
            elif outcome == "LOSS":
                flipped_to_loss += 1
                original_win_points = (
                    (original_target - entry) if direction is Direction.LONG else (entry - original_target)
                )
                loss_points = abs(entry - original_stop)
                swing_points = original_win_points + loss_points
                swing_dollars = swing_points * point_value * contracts
                net_delta -= swing_dollars
                print(prefix + "  zone=" + str(round(zone.price, 2)) + " extended_target=" + str(extended_target) + "  -> FLIPPED TO LOSS, swing -$" + str(round(swing_dollars, 2)))
            else:
                unresolved += 1
                print(prefix + "  zone=" + str(round(zone.price, 2)) + " extended_target=" + str(extended_target) + "  -> UNRESOLVED (ran off the end of fetched history)")

        print("")
        print("Of " + str(len(wins)) + " real wins, " + str(had_extension) + " had a qualifying zone beyond the original target.")
        print("  " + str(bigger_win) + " would have been BIGGER wins.")
        print("  " + str(flipped_to_loss) + " would have FLIPPED to a loss.")
        print("  " + str(unresolved) + " unresolved (ran off the end of fetched history).")
        print("Net $ change versus current behavior: $" + str(round(net_delta, 2)))


if __name__ == "__main__":
    main()
