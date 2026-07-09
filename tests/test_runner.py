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

    def __init__(self, order_id_to_return: str | None, raise_on_place: Exception | None = None):
        self.order_id_to_return = order_id_to_return
        self.raise_on_place = raise_on_place
        self.placed_orders: list[dict] = []
        self.flatten_calls: list[str] = []

    def connect(self) -> None:
        pass

    def subscribe_bars(self, symbol: str, timeframe_minutes: int, on_bar: Callable[[Bar], None]) -> None:
        pass

    def place_bracket_order(self, **kwargs) -> str | None:
        self.placed_orders.append(kwargs)
        if self.raise_on_place is not None:
            raise self.raise_on_place
        return self.order_id_to_return

    def poll_order_status(self, order_id: str) -> str:
        return "open"

    def flatten_all(self, symbol: str) -> None:
        self.flatten_calls.append(symbol)


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
        stop_price=entry_price - 30.0,  # 30 points below entry -- clears the real config's min/max stop band
        timestamp=DAY,
    )


def test_enter_trade_records_a_trade_when_the_broker_fills_it(tmp_path):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.strategy.state = State.IN_TRADE  # as if on_bar just fired this signal

    runner.day_slot._enter_trade(make_signal())

    assert runner.day_slot.current_order_id == "1"
    assert runner.day_slot.current_trade is not None
    assert runner.day_slot.current_trade.entry_price == 100.0
    # Untouched -- a real fill doesn't need the not-filled reset.
    assert runner.day_slot.strategy.state is State.IN_TRADE


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
    runner.day_slot.strategy._breakout_direction = Direction.LONG
    runner.day_slot.strategy.state = State.IN_TRADE  # as if on_bar just fired this signal

    runner.day_slot._enter_trade(make_signal())

    assert runner.day_slot.current_order_id is None
    assert runner.day_slot.current_trade is None
    # Back to hunting within the same breakout, not stuck in IN_TRADE and
    # not reset all the way back to WAIT_BREAKOUT (no real loss occurred).
    assert runner.day_slot.strategy.state is State.WAIT_5M_FVG
    assert runner.day_slot.strategy._breakout_direction is Direction.LONG
    # The broker was actually asked to place the order -- this isn't
    # skipping the attempt, just handling its failure to fill.
    assert len(broker.placed_orders) == 1


def test_enter_trade_resets_strategy_when_placing_the_order_raises(tmp_path, capsys):
    """Confirmed live 2026-07-08: a real /Order/place 400 Bad Request
    raised uncaught out of place_bracket_order, leaving the strategy stuck
    at IN_TRADE forever (current_trade never set, nothing ever resets the
    state). Any failure placing the order -- not just a clean None return
    -- must be treated like a not-filled entry."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1", raise_on_place=RuntimeError("400 Bad Request"))
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.strategy._breakout_direction = Direction.LONG
    runner.day_slot.strategy.state = State.IN_TRADE  # as if on_bar just fired this signal

    runner.day_slot._enter_trade(make_signal())  # must not raise

    assert runner.day_slot.current_order_id is None
    assert runner.day_slot.current_trade is None
    assert runner.day_slot.strategy.state is State.WAIT_5M_FVG
    assert "400 Bad Request" in capsys.readouterr().out


def test_overnight_slot_is_created_when_enabled():
    cfg = load_test_config()
    assert cfg.strategy.overnight.enabled is True
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker)

    assert runner.overnight_slot is not None
    assert runner.overnight_slot.flatten_by is None  # never force-flattened, unlike the day slot


def test_overnight_slot_is_absent_when_disabled():
    cfg = load_test_config()
    cfg.strategy.overnight.enabled = False
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker)

    assert runner.overnight_slot is None


def test_day_and_overnight_trades_are_tracked_independently(tmp_path):
    """The two strategies must not share trade bookkeeping -- entering a
    trade on one slot must not touch the other's current_trade/
    current_order_id."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="day-order")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    assert runner.overnight_slot is not None

    runner.day_slot._enter_trade(make_signal(entry_price=100.0))

    assert runner.day_slot.current_order_id == "day-order"
    assert runner.overnight_slot.current_order_id is None
    assert runner.overnight_slot.current_trade is None


def test_risk_state_is_shared_across_both_slots(tmp_path):
    """A single account-wide daily trade-count/loss cap applies across both
    strategies combined, since they trade the same account and budget --
    not a separate cap per strategy."""
    cfg = load_test_config()
    cfg.risk_limits.max_trades_per_day = 1
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    assert runner.overnight_slot is not None

    runner.risk_state.reset_if_new_day(DAY.date())
    runner.risk_state.record_trade_result(50.0)  # counts as this account's 1st trade today

    assert runner.risk_state.can_take_new_trade() is False


def test_on_bar_writes_a_status_file_for_the_dashboard(tmp_path):
    """src/dashboard.py's "Bot activity" section reads this file to show
    whether the bot is actively scanning -- must reflect both slots'
    current state after every bar, and use a path Runner can be told to use
    so tests don't touch the real trades/ directory."""
    import json

    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    status_path = tmp_path / "status.json"
    runner = Runner(
        cfg,
        broker,
        logger=TradeLogger(path=str(tmp_path / "trades.csv")),
        status_path=str(status_path),
    )

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    assert status_path.exists()
    status = json.loads(status_path.read_text())
    assert status["last_bar_time"] == DAY.isoformat()
    assert status["process_started_at"] == runner.process_started_at.isoformat()
    assert status["day"]["state"] == runner.day_slot.strategy.state.name
    assert status["day"]["in_trade"] is False
    if runner.overnight_slot is not None:
        assert status["overnight"]["state"] == runner.overnight_slot.strategy.state.name
    else:
        assert status["overnight"] is None


def test_status_file_includes_last_price_and_recent_candles(tmp_path):
    """The dashboard's live chart snapshot reads recent_candles -- must
    reflect every bar seen so far, in the shared OHLC shape."""
    import json

    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    status_path = tmp_path / "status.json"
    runner = Runner(
        cfg,
        broker,
        logger=TradeLogger(path=str(tmp_path / "trades.csv")),
        status_path=str(status_path),
    )

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    status = json.loads(status_path.read_text())
    assert status["last_price"] == 100.0
    assert len(status["recent_candles"]) == 1
    assert status["recent_candles"][0] == {
        "t": DAY.isoformat(),
        "o": 100.0,
        "h": 100.5,
        "l": 99.5,
        "c": 100.0,
    }


def test_status_file_includes_unrealized_pnl_for_an_open_trade(tmp_path):
    """The dashboard shows live P&L on an open trade, not just closed-trade
    P&L -- must match Trade.unrealized_pnl_dollars against the latest bar's
    close, and stay None while no trade is open."""
    import json

    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    status_path = tmp_path / "status.json"
    runner = Runner(
        cfg,
        broker,
        logger=TradeLogger(path=str(tmp_path / "trades.csv")),
        status_path=str(status_path),
    )
    runner.day_slot.strategy.state = State.IN_TRADE
    runner.day_slot._enter_trade(make_signal(entry_price=100.0))  # long

    runner.on_bar(Bar(timestamp=DAY, open=104.0, high=105.0, low=103.5, close=105.0))

    status = json.loads(status_path.read_text())
    assert status["day"]["in_trade"] is True
    expected = runner.day_slot.current_trade.unrealized_pnl_dollars(105.0, cfg.instrument.point_value)
    assert status["day"]["unrealized_pnl_dollars"] == expected
    assert expected > 0  # price moved in the long's favor
    assert status["day"]["entry_price"] == 100.0
    assert status["day"]["stop_price"] == runner.day_slot.current_trade.stop_price
    assert status["day"]["target_price"] == runner.day_slot.current_trade.target_price
    if runner.overnight_slot is not None:
        assert status["overnight"]["in_trade"] is False
        assert status["overnight"]["unrealized_pnl_dollars"] is None
        assert status["overnight"]["entry_price"] is None


def test_status_file_write_failure_does_not_crash_on_bar(tmp_path):
    """Writing the dashboard status file is a nice-to-have -- a failure
    here (e.g. a bad path) must never take down live trading."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(
        cfg,
        broker,
        logger=TradeLogger(path=str(tmp_path / "trades.csv")),
        status_path=str(tmp_path / "status.json"),
    )
    # Point status_path at something that can't be written (a directory).
    bad_dir = tmp_path / "not_a_file"
    bad_dir.mkdir()
    runner.status_path = bad_dir

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))  # must not raise


def test_on_bar_exception_anywhere_inside_does_not_propagate(tmp_path, capsys):
    """Confirmed live 2026-07-08 (via signalrcore's own source): an
    exception escaping on_bar propagates into the realtime hub's socket
    receive loop, which silently kills that connection's thread with no
    reconnect ever triggered -- the bot goes deaf to real-time data until
    the next scheduled hourly refresh. A bug anywhere in the strategy
    pipeline must never be able to take down the live data feed like
    that."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))

    def _boom(bar, local_time):
        raise RuntimeError("simulated bug in the strategy pipeline")

    runner.day_slot.on_bar = _boom

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))  # must not raise

    assert "simulated bug in the strategy pipeline" in capsys.readouterr().out


def test_recent_bars_survive_concurrent_append_and_read(tmp_path):
    """Confirmed live 2026-07-08: a "deque mutated during iteration"
    RuntimeError surfaced, meaning on_bar (which appends to _recent_bars)
    and _write_status (which iterates it) were genuinely running from more
    than one thread at once. _recent_bars_lock must make concurrent
    append + read safe regardless of how many threads call in -- tested
    directly against those two operations rather than the full on_bar
    pipeline, since the strategy state machines themselves were never
    designed for concurrent access and aren't what this lock protects.
    Forces a very short GIL switch interval so the two threads actually
    interleave within the loop instead of each just happening to finish
    before the other gets scheduled -- without this, the race is real but
    doesn't reliably reproduce in a test this short."""
    import sys
    import threading

    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    bar = Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0)

    errors = []

    def appender() -> None:
        try:
            for _ in range(800):
                with runner._recent_bars_lock:
                    runner._recent_bars.append(bar)
        except Exception as e:  # pragma: no cover -- the whole point is that this must not happen
            errors.append(e)

    def writer() -> None:
        try:
            for _ in range(800):
                runner._write_status(bar)
        except Exception as e:  # pragma: no cover -- the whole point is that this must not happen
            errors.append(e)

    original_switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=appender) for _ in range(4)] + [
            threading.Thread(target=writer) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(original_switch_interval)

    assert errors == []
