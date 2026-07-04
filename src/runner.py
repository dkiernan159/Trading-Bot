from __future__ import annotations

import time
from zoneinfo import ZoneInfo

from src.broker.base import Broker
from src.config import BotConfig, load_config
from src.logger import TradeLogger
from src.models import Bar, Trade
from src.risk import DailyRiskState, compute_stop_target
from src.strategy import EntrySignal, OpeningRangeStrategy


class Runner:
    """Wires broker -> strategy -> risk -> broker (orders) -> logger into a
    single bar-driven loop. Works against any Broker implementation (mock or
    real), so the same runner is used for testing and for live trading."""

    def __init__(self, cfg: BotConfig, broker: Broker, logger: TradeLogger | None = None):
        self.cfg = cfg
        self.broker = broker
        self.tz = ZoneInfo(cfg.session.timezone)
        self.strategy = OpeningRangeStrategy(cfg)
        self.risk_state = DailyRiskState(cfg.risk_limits)
        self.logger = logger or TradeLogger()
        self.current_order_id: str | None = None
        self.current_trade: Trade | None = None

    def start(self) -> None:
        self.broker.connect()
        self.broker.subscribe_bars(self.cfg.instrument.symbol, 1, self.on_bar)

    def on_bar(self, bar: Bar) -> None:
        local = bar.timestamp.astimezone(self.tz)
        self.risk_state.reset_if_new_day(local.date())

        if self.current_trade is not None:
            if local.time() >= self.cfg.session.flatten_by:
                self._flatten_current_trade(bar)
            else:
                self._check_open_trade(bar)
            return

        if not self.risk_state.can_take_new_trade():
            return

        signal = self.strategy.on_bar(bar)
        if signal is not None:
            self._enter_trade(signal)

    def _enter_trade(self, signal: EntrySignal) -> None:
        bracket = compute_stop_target(
            direction=signal.direction,
            entry_price=signal.entry_price,
            structural_levels=signal.structural_levels,
            max_stop_dollars=self.cfg.strategy.max_stop_dollars,
            min_stop_dollars=self.cfg.strategy.min_stop_dollars,
            point_value=self.cfg.instrument.point_value,
            contracts=self.cfg.position_sizing.contract_size,
            reward_risk_ratio=self.cfg.strategy.reward_risk_ratio,
        )
        contracts = self.cfg.position_sizing.contract_size

        order_id = self.broker.place_bracket_order(
            symbol=self.cfg.instrument.symbol,
            direction=signal.direction,
            contracts=contracts,
            entry_price=signal.entry_price,
            stop_price=bracket.stop_price,
            target_price=bracket.target_price,
        )
        self.current_order_id = order_id
        self.current_trade = Trade(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_price=bracket.stop_price,
            target_price=bracket.target_price,
            contracts=contracts,
            entry_time=signal.timestamp,
        )

    def _check_open_trade(self, bar: Bar) -> None:
        assert self.current_trade is not None and self.current_order_id is not None
        status = self.broker.poll_order_status(self.current_order_id)
        if status == "open":
            return

        won = status == "filled_target"
        self._close_current_trade(
            exit_price=self.current_trade.target_price if won else self.current_trade.stop_price,
            exit_time=bar.timestamp,
            exit_reason="target" if won else "stop",
        )

    def _flatten_current_trade(self, bar: Bar) -> None:
        self.broker.flatten_all(self.cfg.instrument.symbol)
        self._close_current_trade(exit_price=bar.close, exit_time=bar.timestamp, exit_reason="flatten")

    def _close_current_trade(self, exit_price: float, exit_time, exit_reason: str) -> None:
        trade = self.current_trade
        assert trade is not None
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_reason = exit_reason

        pnl = trade.pnl_dollars(self.cfg.instrument.point_value) or 0.0
        self.risk_state.record_trade_result(pnl)
        self.logger.log_trade(trade, self.cfg.instrument.point_value)

        self.strategy.notify_trade_closed(won=(exit_reason == "target"))

        self.current_trade = None
        self.current_order_id = None


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

    # subscribe_bars() only registers the callback and starts the SignalR
    # connection; keep the process alive to receive bars.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
