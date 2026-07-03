from __future__ import annotations

"""Backtests the opening-range strategy against recent real history from
TopstepX. Requires .env credentials (same as the live bot) and network
access to the ProjectX Gateway API -- run this on the VPS, not locally.

    python -m src.backtest --days 7

Reuses the exact same strategy state machine and stop/target calculator the
live bot uses, so results reflect the real STRATEGY.md rules and
assumptions (fix those first if they don't match how you actually trade --
this backtest can't tell you the assumptions are wrong, only how the
current rules would have performed).

Caveats:
  - Fills are idealized: no slippage, no commissions, and if a single bar's
    range touches both the stop and target, the stop is conservatively
    assumed to have been hit first.
  - Bars come from TopstepX's official 1-minute history, which may differ
    slightly from the tick-aggregated bars the live bot builds in real time.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.broker.projectx_gateway import ProjectXGatewayBroker
from src.config import BotConfig, load_config
from src.models import Bar, Direction
from src.risk import compute_stop_target
from src.strategy import OpeningRangeStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=7, help="Calendar days of history to report on (default: 7, ~last week)."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the levels/box/FVG behind each trade, to verify the mechanics rather than just outcomes.",
    )
    return parser.parse_args()


def fetch_recent_bars(broker: ProjectXGatewayBroker, symbol: str, tz: ZoneInfo, days: int) -> list[Bar]:
    now = datetime.now(tz)
    # 2 extra buffer days so the earliest reported day still has a real
    # previous-day high/low to use as a structural stop level.
    day = (now - timedelta(days=days + 2)).replace(hour=0, minute=0, second=0, microsecond=0)
    all_bars: list[Bar] = []
    while day.date() <= now.date():
        day_end = min(day + timedelta(days=1), now)
        all_bars.extend(broker.fetch_historical_bars(symbol, day, day_end))
        day += timedelta(days=1)
    all_bars.sort(key=lambda b: b.timestamp)
    return all_bars


def run_backtest(cfg: BotConfig, bars: list[Bar]) -> list[dict]:
    strategy = OpeningRangeStrategy(cfg)
    tz = ZoneInfo(cfg.session.timezone)
    open_trade: dict | None = None
    results: list[dict] = []

    for bar in bars:
        if open_trade is not None:
            direction = open_trade["direction"]
            if direction is Direction.LONG:
                hit_stop = bar.low <= open_trade["stop_price"]
                hit_target = bar.high >= open_trade["target_price"]
            else:
                hit_stop = bar.high >= open_trade["stop_price"]
                hit_target = bar.low <= open_trade["target_price"]

            if hit_stop or hit_target:
                won = hit_target and not hit_stop  # both hit same bar -> assume stop first
                results.append(
                    {
                        **open_trade,
                        "date": open_trade["entry_time"].astimezone(tz).date(),
                        "direction": direction.value,
                        "won": won,
                    }
                )
                strategy.notify_trade_closed(won=won)
                open_trade = None

        signal = strategy.on_bar(bar)
        if open_trade is None and signal is not None:
            bracket = compute_stop_target(
                direction=signal.direction,
                entry_price=signal.entry_price,
                structural_levels=signal.structural_levels,
                max_stop_points=cfg.strategy.max_stop_points,
                reward_risk_ratio=cfg.strategy.reward_risk_ratio,
            )
            levels = strategy.current_session_levels
            open_trade = {
                "direction": signal.direction,
                "entry_time": signal.timestamp,
                "entry_price": signal.entry_price,
                "stop_price": bracket.stop_price,
                "target_price": bracket.target_price,
                "previous_day_high": levels.previous_day_high if levels else None,
                "previous_day_low": levels.previous_day_low if levels else None,
                "asia_high": levels.asia_high if levels else None,
                "asia_low": levels.asia_low if levels else None,
                "london_high": levels.london_high if levels else None,
                "london_low": levels.london_low if levels else None,
                "box_high": strategy.box.high,
                "box_low": strategy.box.low,
                "fvg_gap_low": signal.fvg.gap_low,
                "fvg_gap_high": signal.fvg.gap_high,
            }

    return results


def _pnl_points(trade: dict) -> float:
    """Signed point P&L: positive on a win, negative on a loss, for either direction."""
    exit_price = trade["target_price"] if trade["won"] else trade["stop_price"]
    if trade["direction"] == "long":
        return exit_price - trade["entry_price"]
    return trade["entry_price"] - exit_price


def print_report(cfg: BotConfig, results: list[dict]) -> None:
    if not results:
        print("No trades were triggered by the strategy in this window.")
        return

    by_day: dict = defaultdict(list)
    for r in results:
        by_day[r["date"]].append(r)

    contracts = cfg.position_sizing.contract_size
    point_value = cfg.instrument.point_value

    header = f"{'Date':<12}{'Trades':<8}{'Wins':<6}{'Win %':<8}{'Net $ (@' + str(contracts) + ' ctr)':<18}"
    print(header)
    print("-" * len(header))

    total_trades = total_wins = 0
    total_pnl = 0.0
    for day in sorted(by_day):
        trades = by_day[day]
        wins = sum(1 for t in trades if t["won"])
        n = len(trades)
        day_pnl = sum(_pnl_points(t) for t in trades) * point_value * contracts
        total_trades += n
        total_wins += wins
        total_pnl += day_pnl
        print(f"{str(day):<12}{n:<8}{wins:<6}{100 * wins / n:<8.0f}{day_pnl:<18.2f}")

    print("-" * len(header))
    overall_pct = 100 * total_wins / total_trades if total_trades else 0
    print(f"{'TOTAL':<12}{total_trades:<8}{total_wins:<6}{overall_pct:<8.0f}{total_pnl:<18.2f}")


def print_trade_detail(results: list[dict]) -> None:
    """Prints the levels/box/FVG behind each trade, so the mechanics can be
    checked (not just outcomes) -- previous day/Asia/London high-low, the
    9:30-9:45 box, the confirming FVG's gap, and the resulting bracket."""
    if not results:
        return

    print("\nTrade detail:")
    for i, t in enumerate(results, start=1):
        print(f"\n#{i}  {t['date']}  {t['direction'].upper()}  {'WIN' if t['won'] else 'LOSS'}")
        print(
            f"    Previous day: high={_fmt(t['previous_day_high'])}  low={_fmt(t['previous_day_low'])}"
        )
        print(f"    Asia session: high={_fmt(t['asia_high'])}  low={_fmt(t['asia_low'])}")
        print(f"    London session: high={_fmt(t['london_high'])}  low={_fmt(t['london_low'])}")
        print(f"    9:30-9:45 box: high={_fmt(t['box_high'])}  low={_fmt(t['box_low'])}")
        print(f"    Confirming 1m FVG: {_fmt(t['fvg_gap_low'])} - {_fmt(t['fvg_gap_high'])}")
        print(
            f"    Entry={_fmt(t['entry_price'])}  Stop={_fmt(t['stop_price'])}  Target={_fmt(t['target_price'])}"
        )


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg.session.timezone)

    broker = ProjectXGatewayBroker(
        base_url=cfg.broker.base_url,
        realtime_base_url=cfg.broker.realtime_base_url,
        dry_run=True,  # backtest never places orders, regardless of config.yaml
    )
    broker.connect()

    print(f"Fetching ~{args.days + 2} days of 1-minute history for {cfg.instrument.symbol}...")
    bars = fetch_recent_bars(broker, cfg.instrument.symbol, tz, args.days)
    print(f"Fetched {len(bars)} bars. Running backtest...\n")

    results = run_backtest(cfg, bars)
    print_report(cfg, results)
    if args.verbose:
        print_trade_detail(results)


if __name__ == "__main__":
    main()
