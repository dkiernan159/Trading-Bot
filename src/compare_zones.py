from __future__ import annotations

"""Ad-hoc analysis tool, not part of live trading. Three parts:

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
   from the zone question entirely. Real data 2026-09-29 showed this
   tally matters on its own: the day strategy's biggest blockers were
   superseded (34%) + invalidated (33%), zones were a smaller 21%; only
   1 of 87 day anchors ever filled. The overnight strategy's biggest
   single blocker was "stale" abandonment at 33%.

3. Overnight-only (day has no stale-abandon mechanism): for every
   "stale" anchor (overnight_strategy.py's WAIT_FILL abandons an anchor
   if price runs cfg.strategy.max_stop_dollars/2 points further away
   from the resting limit without ever retracing back to it), checks
   whether real price ever DID come back to that same entry level within
   a longer window after the abandonment, and if so how the setup did
   from there -- i.e., "was the stale threshold too tight, or was it
   right to give up." No would_be_* capture exists for "stale" (it's a
   different WAIT_FILL exit than the zone pause), so this recomputes the
   entry price directly from the anchor's own gap bounds using the same
   pure retracement-fraction formula overnight_strategy.py's own
   _entry_price uses -- no strategy state needed for that part.

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
from src.config import BotConfig, load_config
from src.models import Bar, Direction
from src.risk import round_to_tick
from src.strategy import AnchorRecord

DAYS = 7
DIRECTIONAL_WINDOW_MINUTES = 60
STALE_LOOKAHEAD_HOURS = 6


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


def _recompute_entry_price(cfg: BotConfig, direction: Direction, gap_low: float, gap_high: float) -> float:
    """Same pure formula as overnight_strategy.py's own _entry_price --
    depends only on the anchor's gap bounds and config, not any strategy
    state, so it can be reconstructed after the fact for an anchor that
    was abandoned as stale (which never captured a would_be_entry, unlike
    the zone-pause path)."""
    pct = cfg.strategy.entry_retracement_pct
    width = gap_high - gap_low
    price = gap_high - pct * width if direction is Direction.LONG else gap_low + pct * width
    return round_to_tick(price, cfg.instrument.tick_size)


def _analyze_stale_anchor(
    a: AnchorRecord, cfg: BotConfig, bars: list[Bar]
) -> tuple[float, float, Bar | None]:
    """Returns (recomputed entry price, how far price ran further away
    before ever coming back (0.0 if it came straight back), the bar that
    finally retraced back to touch entry -- or None if it never did
    within STALE_LOOKAHEAD_HOURS)."""
    entry = _recompute_entry_price(cfg, a.direction, a.gap_low, a.gap_high)
    cutoff = a.ended_at + timedelta(hours=STALE_LOOKAHEAD_HOURS)
    furthest_away = 0.0
    for bar in bars:
        if bar.timestamp <= a.ended_at:
            continue
        if bar.timestamp > cutoff:
            break
        if a.direction is Direction.LONG:
            away = bar.high - entry
            touched = bar.low <= entry
        else:
            away = entry - bar.low
            touched = bar.high >= entry
        furthest_away = max(furthest_away, away)
        if touched:
            return entry, furthest_away, bar
    return entry, furthest_away, None


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

        if label == "OVERNIGHT":
            stale = [a for a in anchors if a.outcome == "stale"]
            print("")
            print("Stale-abandoned anchors: " + str(len(stale)) + "  (threshold: " + str(round(cfg.strategy.max_stop_dollars / 2 / (point_value * contracts), 1)) + "pts)")
            came_back = 0
            for a in stale:
                entry, furthest_away, fill_bar = _analyze_stale_anchor(a, cfg, bars)
                prefix = (
                    a.started_at.strftime("%Y-%m-%d %H:%M")
                    + "  " + a.direction.value.upper().ljust(6)
                    + " gap=" + str(round(a.gap_low, 2)) + "-" + str(round(a.gap_high, 2))
                    + "  entry=" + str(round(entry, 2))
                    + "  abandoned=" + a.ended_at.strftime("%H:%M")
                )
                if fill_bar is None:
                    print(prefix + "  never retraced back within " + str(STALE_LOOKAHEAD_HOURS) + "h (ran up to " + str(round(furthest_away, 2)) + "pts further away) -- correctly abandoned")
                    continue
                came_back += 1
                wait = fill_bar.timestamp - a.ended_at
                bars_after_fill = [b for b in bars if b.timestamp > fill_bar.timestamp]
                mfe, mae = _directional_excursion(a.direction, entry, bars_after_fill)
                verdict = "favorable" if mfe > mae else ("unfavorable" if mae > mfe else "flat")
                print(
                    prefix
                    + "  ran " + str(round(furthest_away, 2)) + "pts further away, then DID retrace back "
                    + str(wait) + " later -- from there, next " + str(DIRECTIONAL_WINDOW_MINUTES) + "min: "
                    + "MFE=+" + str(round(mfe, 2)) + "pts  MAE=-" + str(round(mae, 2)) + "pts  -> " + verdict
                )
            print("")
            print(str(came_back) + " of " + str(len(stale)) + " stale-abandoned anchors eventually retraced back to entry within " + str(STALE_LOOKAHEAD_HOURS) + "h.")


if __name__ == "__main__":
    main()
