from __future__ import annotations

import json
import threading
import time
import traceback
from collections import deque
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.broker.base import Broker
from src.config import BotConfig, load_config
from src.logger import TradeLogger
from src.models import Bar, Trade
from src.overnight_strategy import EntrySignal as OvernightEntrySignal, OvernightMomentumStrategy
from src.risk import DailyRiskState, compute_stop_target
from src.strategy import EntrySignal, OpeningRangeStrategy

# How many recent 1-minute bars Runner keeps in memory for the dashboard's
# live chart snapshot (see _write_status) -- 3 hours, enough to see an
# entire overnight hunting window without the status file growing without
# bound. Purely a display window; has no effect on trading decisions.
RECENT_CANDLES_MAXLEN = 180


class _StrategySlot:
    """Wraps one strategy instance with its own independent in-flight-trade
    bookkeeping, so multiple strategies (the 9:30 ORB day strategy and the
    Asia/London overnight strategy) can run in parallel against the same
    broker/account without one's open trade interfering with the other's.
    risk_state (trade count / daily P&L cap) is shared across both slots --
    they trade the same account and the same daily risk budget, so a single
    account-wide cap is what actually protects a funded/eval account,
    regardless of which strategy is placing the trade.

    flatten_by is a hard end-of-window flatten time (the day strategy's
    13:45 ET), or None to never force-flatten (the overnight strategy: an
    open trade that outlives its own hunting window is left alone rather
    than flattened, matching that strategy's own documented design)."""

    def __init__(self, name: str, strategy, flatten_by: dtime | None, runner: "Runner"):
        self.name = name
        self.strategy = strategy
        self.flatten_by = flatten_by
        self.runner = runner
        self.current_order_id: str | None = None
        self.current_trade: Trade | None = None

    def on_bar(self, bar: Bar, local_time: dtime) -> None:
        if self.current_trade is not None:
            if self.flatten_by is not None and local_time >= self.flatten_by:
                self._flatten_current_trade(bar)
            else:
                self._check_open_trade(bar)
            return

        if not self.runner.risk_state.can_take_new_trade():
            return

        # Confirmed live 2026-07-09: WAIT_FILL was observed reverting
        # several times in one evening with no way to tell why -- the
        # strategy classes already record every anchor's outcome
        # (filled/superseded/invalidated/no_valid_stop/session_ended) in
        # anchor_history for backtest's own near-miss reporting, but
        # nothing surfaced that live. Diffing anchor_history's length
        # around the on_bar call and printing whatever's new gives that
        # same visibility in bot.log without the strategy classes
        # themselves needing to know they're running live vs backtest.
        anchors_before = len(self.strategy.anchor_history)
        signal = self.strategy.on_bar(bar)
        for record in self.strategy.anchor_history[anchors_before:]:
            live_for = record.ended_at - record.started_at
            # Confirmed live 2026-07-10: these lines had no timestamp at
            # all, so a plain grep of bot.log couldn't tell "tonight" apart
            # from any earlier night since the file was last rotated --
            # AnchorRecord already carries ended_at, just wasn't printed.
            print(
                f"[LIVE] {record.ended_at.isoformat()} {self.name}: anchor ended ({record.outcome}) -- "
                f"{record.direction.value.upper()} gap={record.gap_low:.2f}-{record.gap_high:.2f}, "
                f"live for {live_for}"
            )
        if signal is not None:
            self._enter_trade(signal)

    def _enter_trade(self, signal: EntrySignal | OvernightEntrySignal) -> None:
        cfg = self.runner.cfg
        bracket = compute_stop_target(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            max_stop_dollars=cfg.strategy.max_stop_dollars,
            min_stop_dollars=cfg.strategy.min_stop_dollars,
            point_value=cfg.instrument.point_value,
            contracts=cfg.position_sizing.contract_size,
            reward_risk_ratio=cfg.strategy.reward_risk_ratio,
        )
        contracts = cfg.position_sizing.contract_size

        try:
            order_id = self.runner.broker.place_bracket_order(
                symbol=cfg.instrument.symbol,
                direction=signal.direction,
                contracts=contracts,
                entry_price=signal.entry_price,
                stop_price=bracket.stop_price,
                target_price=bracket.target_price,
            )
        except Exception:
            # Confirmed live 2026-07-08: a real /Order/place 400 (Bad
            # Request) raised here, uncaught, leaving the strategy stuck
            # believing it was IN_TRADE forever (nothing else ever resets
            # that state) while current_order_id/current_trade were never
            # set -- the strategy silently never hunted again for the rest
            # of the process's life. Any failure placing the order --
            # rejected request, network error, whatever -- must be treated
            # exactly like "didn't fill": nothing was actually taken, so go
            # back to hunting instead of getting permanently stuck.
            print(f"[LIVE] WARNING: order placement raised, treating as not filled:\n{traceback.format_exc()}")
            self.strategy.notify_entry_not_filled()
            return
        if order_id is None:
            # The signal fired (backtest-equivalent: the bar-level check
            # says price touched entry_price), but the live broker's
            # resting entry order never actually got filled -- e.g. price
            # had already moved on by the time the order reached the
            # exchange, given the ~60s lag between a bar closing and the
            # order being placed. The strategy already moved to IN_TRADE
            # internally the moment it returned this signal; tell it
            # nothing was actually taken so it goes back to hunting
            # instead of sitting stuck in a phantom trade.
            self.strategy.notify_entry_not_filled()
            return
        self.current_order_id = order_id
        self.current_trade = Trade(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_price=bracket.stop_price,
            target_price=bracket.target_price,
            contracts=contracts,
            entry_time=signal.timestamp,
            stop_source=signal.stop_source,
            stop_fvg_size=signal.stop_fvg_size,
        )

    def _check_open_trade(self, bar: Bar) -> None:
        assert self.current_trade is not None and self.current_order_id is not None
        status = self.runner.broker.poll_order_status(self.current_order_id)
        if status == "open":
            return

        won = status == "filled_target"
        self._close_current_trade(
            exit_price=self.current_trade.target_price if won else self.current_trade.stop_price,
            exit_time=bar.timestamp,
            exit_reason="target" if won else "stop",
        )

    def _flatten_current_trade(self, bar: Bar) -> None:
        self.runner.broker.flatten_all(self.runner.cfg.instrument.symbol)
        self._close_current_trade(exit_price=bar.close, exit_time=bar.timestamp, exit_reason="flatten")

    def _close_current_trade(self, exit_price: float, exit_time: datetime, exit_reason: str) -> None:
        trade = self.current_trade
        assert trade is not None
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = exit_reason

        pnl = trade.pnl_dollars(self.runner.cfg.instrument.point_value) or 0.0
        self.runner.risk_state.record_trade_result(pnl)
        self.runner.logger.log_trade(trade, self.runner.cfg.instrument.point_value, self.name)

        self.strategy.notify_trade_closed(won=(exit_reason == "target"))

        self.current_trade = None
        self.current_order_id = None

    def status_for_dashboard(self, current_price: float) -> dict:
        """Dashboard-only view of this slot's current state plus, if a
        trade is open, its entry/stop/target and live unrealized P&L
        against current_price -- never read by the trading logic itself."""
        status = {
            **self.strategy.status_snapshot(),
            "in_trade": self.current_trade is not None,
            "unrealized_pnl_dollars": None,
            "entry_price": None,
            "stop_price": None,
            "target_price": None,
        }
        if self.current_trade is not None:
            status["unrealized_pnl_dollars"] = self.current_trade.unrealized_pnl_dollars(
                current_price, self.runner.cfg.instrument.point_value
            )
            status["entry_price"] = self.current_trade.entry_price
            status["stop_price"] = self.current_trade.stop_price
            status["target_price"] = self.current_trade.target_price
        return status


class Runner:
    """Wires broker -> strategy -> risk -> broker (orders) -> logger into a
    single bar-driven loop. Works against any Broker implementation (mock or
    real), so the same runner is used for testing and for live trading.

    Runs the 9:30 ORB day strategy and the Asia/London overnight strategy in
    parallel, each in its own _StrategySlot -- both see every bar, neither
    knows the other exists, and each manages its own trade independently.
    The overnight slot is only created when cfg.strategy.overnight.enabled
    is true."""

    def __init__(
        self,
        cfg: BotConfig,
        broker: Broker,
        logger: TradeLogger | None = None,
        status_path: str = "trades/status.json",
    ):
        self.cfg = cfg
        self.broker = broker
        self.tz = ZoneInfo(cfg.session.timezone)
        self.risk_state = DailyRiskState(cfg.risk_limits)
        self.logger = logger or TradeLogger()
        self.status_path = Path(status_path)
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        # Confirmed live 2026-07-09: the bot was found to be getting
        # restarted (via bot-pull/systemctl restart) far more often than
        # intended, silently wiping the box/FVG-pool/swing-tracker state
        # built up so far each time -- with no restart visible anywhere
        # in the dashboard, it looked like "the strategy just isn't
        # finding trades" when the real cause was the process never
        # staying up long enough to. Recorded once at process start so the
        # dashboard can surface it (see dashboard_template.html's
        # process-uptime badge).
        self.process_started_at = datetime.now(timezone.utc)
        self._recent_bars: deque[Bar] = deque(maxlen=RECENT_CANDLES_MAXLEN)
        # Confirmed live 2026-07-08: a "deque mutated during iteration"
        # RuntimeError surfaced here, meaning on_bar was genuinely being
        # invoked concurrently from more than one thread (see
        # ProjectXGatewayBroker's hub-generation guard for the likely
        # cause). Guards every read/mutation of _recent_bars so that can
        # never corrupt the dashboard snapshot again, regardless of why
        # more than one thread ends up calling on_bar.
        self._recent_bars_lock = threading.Lock()

        self.day_slot = _StrategySlot("day", OpeningRangeStrategy(cfg), cfg.session.flatten_by, self)
        self.overnight_slot: _StrategySlot | None = None
        if cfg.strategy.overnight.enabled:
            self.overnight_slot = _StrategySlot("overnight", OvernightMomentumStrategy(cfg), None, self)

    def start(self) -> None:
        self.broker.connect()
        self.broker.subscribe_bars(self.cfg.instrument.symbol, 1, self.on_bar)

    def on_bar(self, bar: Bar) -> None:
        # Confirmed live 2026-07-08: an uncaught exception anywhere in
        # here (e.g. a rejected /Order/place call) propagates back into
        # signalrcore's own socket receive loop, which silently kills that
        # connection's receive thread -- no reconnect is triggered, so the
        # bot goes deaf to real-time data until the next scheduled hourly
        # refresh (or forever, if the same bug fires again right after).
        # Trading logic bugs must never be able to take down the data feed
        # like that, so nothing from here is allowed to escape.
        try:
            self._on_bar(bar)
        except Exception:
            print(f"[LIVE] WARNING: on_bar raised, this bar's processing was aborted:\n{traceback.format_exc()}")

    def _on_bar(self, bar: Bar) -> None:
        local = bar.timestamp.astimezone(self.tz)
        self.risk_state.reset_if_new_day(local.date())
        with self._recent_bars_lock:
            self._recent_bars.append(bar)

        self.day_slot.on_bar(bar, local.time())
        if self.overnight_slot is not None:
            self.overnight_slot.on_bar(bar, local.time())
        self._write_status(bar)

    def _write_status(self, bar: Bar) -> None:
        """Dashboard-only visibility into what each strategy is currently
        doing (see src/dashboard.py's "Bot activity" section) -- never read
        by the trading logic itself, so a failure to write it must never
        take down live trading."""
        with self._recent_bars_lock:
            recent_candles = [
                {"t": b.timestamp.isoformat(), "o": b.open, "h": b.high, "l": b.low, "c": b.close}
                for b in self._recent_bars
            ]
        status = {
            "last_bar_time": bar.timestamp.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "process_started_at": self.process_started_at.isoformat(),
            "last_price": bar.close,
            "recent_candles": recent_candles,
            "day": self.day_slot.status_for_dashboard(bar.close),
            "overnight": (
                self.overnight_slot.status_for_dashboard(bar.close) if self.overnight_slot is not None else None
            ),
        }
        try:
            with open(self.status_path, "w") as f:
                json.dump(status, f)
        except OSError:
            pass


def main() -> None:
    cfg = load_config("config.yaml")

    if cfg.broker.provider != "projectx_gateway":
        raise RuntimeError(f"Unsupported broker provider: {cfg.broker.provider}")

    from src.broker.projectx_gateway import ProjectXGatewayBroker

    broker = ProjectXGatewayBroker(
        base_url=cfg.broker.base_url,
        realtime_base_url=cfg.broker.realtime_base_url,
        dry_run=cfg.broker.dry_run,
    )
    runner = Runner(cfg, broker)
    runner.start()

    if cfg.broker.dry_run:
        print(
            "Running in DRY RUN mode (config.yaml: broker.dry_run) -- no real "
            "orders will be sent. Flip to false only after verifying the "
            "unverified items in src/broker/projectx_gateway.py."
        )
    if cfg.strategy.overnight.enabled:
        print(
            "Overnight (Asia/London) strategy is LIVE alongside the day strategy "
            "(config.yaml: strategy.overnight.enabled)."
        )

    # subscribe_bars() only registers the callback and starts the SignalR
    # connection; keep the process alive to receive bars.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
