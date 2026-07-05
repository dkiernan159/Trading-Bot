from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from src.broker.base import Broker
from src.config import load_config
from src.fvg import FairValueGap
from src.logger import TradeLogger
from src.models import Bar, Direction
from src.runner import Runner
from src.strategy import EntrySignal, State

TZ = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 6, 10, 15, tzinfo=TZ)


class FakeBroker(Broker):
    """Minimal Broker stub for exercising Runner in isolation, without any
    real network calls -- unlike MockBroker, place_bracket_order's return
    value is configurable so both the normal and "entry never filled"
    paths can be tested directly."""

    def __init__(self, order_id_to_return: str | None):
        self.order_id_to_return = order_id_to_return
        self.placed_orders: list[dict] = []

    def connect(self) -> None:
        pass

    def subscribe_bars(self, symbol: str, timeframe_minutes: int, on_bar: Callable[[Bar], None]) -> None:
        pass

    def place_bracket_order(self, **kwargs) -> str | None:
        self.placed_orders.append(kwargs)
        return self.order_id_to_return

    def poll_order_status(self, order_id: str) -> str:
        return "open"

    def flatten_all(self, symbol: str) -> None:
        pass


def load_test_config():
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def make_signal(entry_price: float = 100.0) -> EntrySignal:
    anchor = FairValueGap(
        direction=Direction.LONG,
        gap_low=95.0,
        gap_high=105.0,
        formed_at=DAY,
        timeframe_minutes=5,
    )
    return EntrySignal(
        direction=Direction.LONG,
        entry_price=entry_price,
        anchor_fvg=anchor,
        structural_levels=[70.0],  # 30 points below entry -- clears the real config's min/max stop band
        timestamp=DAY,
    )


def test_enter_trade_records_a_trade_when_the_broker_fills_it(tmp_path):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.strategy.state = State.IN_TRADE  # as if on_bar just fired this signal

    runner._enter_trade(make_signal())

    assert runner.current_order_id == "1"
    assert runner.current_trade is not None
    assert runner.current_trade.entry_price == 100.0
    # Untouched -- a real fill doesn't need the not-filled reset.
    assert runner.strategy.state is State.IN_TRADE


def test_enter_trade_resets_strategy_when_the_broker_never_fills_the_entry(tmp_path):
    """A live resting limit order can fail to actually fill (price moved on
    in the ~60s detection lag) even though the strategy's on_bar already
    committed to IN_TRADE, since in backtest a signal always means a real
    fill. place_bracket_order returns None in that case -- Runner must not
    record a trade, and must tell the strategy so it goes back to hunting
    instead of getting stuck believing it's in a trade that doesn't
    exist."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return=None)
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.strategy._breakout_direction = Direction.LONG
    runner.strategy.state = State.IN_TRADE  # as if on_bar just fired this signal

    runner._enter_trade(make_signal())

    assert runner.current_order_id is None
    assert runner.current_trade is None
    # Back to hunting within the same breakout, not stuck in IN_TRADE and
    # not reset all the way back to WAIT_BREAKOUT (no real loss occurred).
    assert runner.strategy.state is State.WAIT_5M_FVG
    assert runner.strategy._breakout_direction is Direction.LONG
    # The broker was actually asked to place the order -- this isn't
    # skipping the attempt, just handling its failure to fill.
    assert len(broker.placed_orders) == 1
