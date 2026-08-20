from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pytest

from src.broker.base import Broker
from src.config import load_config
from src.fvg import FairValueGap
from src.logger import TradeLogger
from src.models import Bar, Direction, Trade
from src.runner import Runner
from src.strategy import AnchorRecord, EntrySignal, State

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
        self.net_position = 0
        self.raise_on_fetch_net_position: Exception | None = None
        self.fetch_net_position_calls = 0
        self.orphaned_orders_to_cancel = 0
        self.raise_on_cancel_orphaned_orders: Exception | None = None
        self.cancel_orphaned_orders_calls = 0
        self.modify_stop_price_calls: list[tuple[str, float]] = []
        self.raise_on_modify_stop_price: Exception | None = None

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

    def fetch_net_position(self, symbol: str) -> int:
        self.fetch_net_position_calls += 1
        if self.raise_on_fetch_net_position is not None:
            raise self.raise_on_fetch_net_position
        return self.net_position

    def cancel_orphaned_orders(self, symbol: str) -> int:
        self.cancel_orphaned_orders_calls += 1
        if self.raise_on_cancel_orphaned_orders is not None:
            raise self.raise_on_cancel_orphaned_orders
        return self.orphaned_orders_to_cancel

    def modify_stop_price(self, order_id: str, new_stop_price: float) -> None:
        self.modify_stop_price_calls.append((order_id, new_stop_price))
        if self.raise_on_modify_stop_price is not None:
            raise self.raise_on_modify_stop_price


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


def test_on_bar_prints_anchor_outcomes_recorded_in_anchor_history(tmp_path, capsys):
    """Confirmed live 2026-07-09: WAIT_FILL was observed reverting several
    times in one evening with no way to tell why -- anchor_history already
    records every anchor's outcome (filled/superseded/invalidated/
    no_valid_stop/session_ended) for backtest's own near-miss reporting,
    but nothing surfaced that live. _StrategySlot.on_bar diffs
    anchor_history's length around the strategy's own on_bar call and
    prints whatever's new, so bot.log shows the reason without the
    strategy classes needing to know they're running live vs backtest."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))

    record = AnchorRecord(
        direction=Direction.LONG,
        gap_low=95.0,
        gap_high=105.0,
        started_at=DAY,
        ended_at=DAY,
        outcome="no_valid_stop",
    )
    runner.day_slot.strategy.on_bar = lambda bar: runner.day_slot.strategy.anchor_history.append(record) or None

    runner.day_slot.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0), DAY.time())

    out = capsys.readouterr().out
    assert "no_valid_stop" in out
    assert "95.00-105.00" in out
    # Confirmed live 2026-07-10: no timestamp at all meant a plain grep of
    # bot.log couldn't tell "tonight" apart from any earlier night since
    # the file was last rotated -- ended_at was already tracked, just
    # wasn't printed.
    assert DAY.isoformat() in out


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


# ---------- _reconcile_open_positions ----------
# (added 2026-08-07: a real order filled for real on the exchange during a
# connection-instability episode, then vanished from the bot's own tracking
# entirely -- no success/timeout/exception message ever printed, never
# logged, never protected. TopstepX's own account statement confirmed a
# real, unaccounted-for loss the bot had no way to have known about. See
# Runner._reconcile_open_positions's own docstring for the full story.)


def make_open_trade() -> Trade:
    return Trade(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        contracts=1,
        entry_time=DAY,
    )


def test_reconcile_flattens_an_orphaned_position_when_every_slot_is_flat(tmp_path, capsys):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    broker.net_position = 2  # a real position the bot has no record of
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    assert broker.flatten_calls == [cfg.instrument.symbol]
    assert "orphaned position detected" in capsys.readouterr().out


def test_reconcile_does_nothing_when_the_broker_reports_flat(tmp_path):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")  # net_position defaults to 0
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    assert broker.flatten_calls == []


def test_reconcile_is_skipped_while_the_day_slot_believes_it_is_in_a_trade(tmp_path):
    """Deliberately doesn't reconcile *positions* while any slot believes
    it's in a trade -- distinguishing "this IS the tracked trade" from
    "there's ALSO an orphan on top of it" needs per-position detail this
    account's API hasn't confirmed it exposes; flattening everything here
    could kill a real, correctly-tracked trade instead of just cleaning up
    a stray one. The orphaned-*order* sweep is unrelated to this and still
    runs regardless -- a stale working order not belonging to any tracked
    bracket is safe to cancel no matter what a strategy slot believes."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    broker.net_position = 2
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "existing"
    runner.day_slot.current_trade = make_open_trade()

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    assert broker.flatten_calls == []
    assert broker.fetch_net_position_calls == 0
    assert broker.cancel_orphaned_orders_calls == 1


# ---------- _cancel_orphaned_orders ----------
# (added 2026-08-07: a resting order can outlive the process life that
# placed it if its own timeout-cancel silently fails -- see
# ProjectXGatewayBroker._cancel_order's history -- and sit unfilled and
# untracked for hours or days before finally executing on its own.)


def test_cancel_orphaned_orders_runs_on_every_reconciliation_cycle(tmp_path, capsys):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    broker.orphaned_orders_to_cancel = 2
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    assert broker.cancel_orphaned_orders_calls == 1
    assert "cancelled 2 stale orphaned working order" in capsys.readouterr().out


def test_cancel_orphaned_orders_prints_nothing_when_none_are_cancelled(tmp_path, capsys):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")  # orphaned_orders_to_cancel defaults to 0

    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))

    assert "orphaned working order" not in capsys.readouterr().out


def test_cancel_orphaned_orders_tolerates_raising(tmp_path, capsys):
    """Must never take down the rest of bar processing, same principle as
    every other reconciliation failure mode in this file."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    broker.raise_on_cancel_orphaned_orders = RuntimeError("network blip")
    broker.net_position = 2  # position reconciliation must still run afterward
    status_path = tmp_path / "status.json"
    runner = Runner(
        cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")), status_path=str(status_path)
    )

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))  # must not raise

    assert "orphaned-order cancellation sweep raised" in capsys.readouterr().out
    assert status_path.exists()
    assert broker.flatten_calls == [cfg.instrument.symbol]  # position check still ran despite this failing


def test_reconcile_is_throttled_to_the_configured_interval(tmp_path):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))
    assert broker.fetch_net_position_calls == 1

    soon_after = DAY + timedelta(minutes=1)
    runner.on_bar(Bar(timestamp=soon_after, open=100.0, high=100.5, low=99.5, close=100.0))
    assert broker.fetch_net_position_calls == 1  # still within the interval -- not checked again

    well_after = DAY + timedelta(minutes=10)
    runner.on_bar(Bar(timestamp=well_after, open=100.0, high=100.5, low=99.5, close=100.0))
    assert broker.fetch_net_position_calls == 2


def test_reconcile_tolerates_fetch_net_position_raising(tmp_path, capsys):
    """A failure checking the broker must never take down the rest of bar
    processing (same principle as on_bar's own outer try/except) -- the
    status file must still get written even if this specific check fails."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    broker.raise_on_fetch_net_position = RuntimeError("network blip")
    status_path = tmp_path / "status.json"
    runner = Runner(
        cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")), status_path=str(status_path)
    )

    runner.on_bar(Bar(timestamp=DAY, open=100.0, high=100.5, low=99.5, close=100.0))  # must not raise

    assert broker.flatten_calls == []
    assert status_path.exists()
    assert "position reconciliation check raised" in capsys.readouterr().out


# ---------- _maybe_move_stop_to_breakeven ----------
# (added 2026-08-20 at the user's explicit request: "when we're in a trade
# and it looks like take profit will be hit, move stop loss above breakeven
# so that the trade doesn't swing down and hit stop loss." Real config:
# breakeven.trigger_pct=0.5, breakeven.buffer_dollars=20, point_value=2.0.)


def make_short_open_trade() -> Trade:
    return Trade(
        direction=Direction.SHORT,
        entry_price=100.0,
        stop_price=110.0,
        target_price=80.0,
        contracts=1,
        entry_time=DAY,
    )


def test_moves_stop_to_breakeven_once_halfway_to_target_long(tmp_path):
    """entry=100, target=120 -- halfway is 110. buffer_dollars=20 /
    (point_value=2.0 * 1 contract) = 10 points -- new stop = 100+10 = 110."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_open_trade()
    runner.day_slot.current_trade = trade

    runner.day_slot._check_open_trade(Bar(timestamp=DAY, open=105.0, high=110.0, low=104.5, close=109.5))

    assert broker.modify_stop_price_calls == [("bracket-1", 110.0)]
    assert trade.stop_price == 110.0
    assert trade.breakeven_moved is True


def test_moves_stop_to_breakeven_once_halfway_to_target_short(tmp_path):
    """entry=100, target=80 -- halfway is 90. buffer_dollars=20 /
    (point_value=2.0 * 1 contract) = 10 points -- new stop = 100-10 = 90."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_short_open_trade()
    runner.day_slot.current_trade = trade

    runner.day_slot._check_open_trade(Bar(timestamp=DAY, open=95.0, high=95.5, low=90.0, close=90.5))

    assert broker.modify_stop_price_calls == [("bracket-1", 90.0)]
    assert trade.stop_price == 90.0
    assert trade.breakeven_moved is True


def test_does_not_move_stop_before_the_trigger_threshold(tmp_path):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_open_trade()
    runner.day_slot.current_trade = trade

    # High of 109.0 is short of the 110.0 halfway point.
    runner.day_slot._check_open_trade(Bar(timestamp=DAY, open=105.0, high=109.0, low=104.5, close=108.5))

    assert broker.modify_stop_price_calls == []
    assert trade.breakeven_moved is False
    assert trade.stop_price == 90.0  # untouched


def test_only_moves_the_stop_once(tmp_path):
    """Fires once per trade -- doesn't keep tightening further as price
    keeps running (that would be an actual trailing stop, a different,
    larger feature this wasn't asked for)."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_open_trade()
    runner.day_slot.current_trade = trade

    runner.day_slot._check_open_trade(Bar(timestamp=DAY, open=105.0, high=110.0, low=104.5, close=109.5))
    runner.day_slot._check_open_trade(
        Bar(timestamp=DAY + timedelta(minutes=1), open=115.0, high=118.0, low=114.5, close=117.5)
    )

    assert broker.modify_stop_price_calls == [("bracket-1", 110.0)]  # only the first call


def test_breakeven_disabled_in_config_never_moves_the_stop(tmp_path):
    cfg = load_test_config()
    cfg.strategy.breakeven.enabled = False
    broker = FakeBroker(order_id_to_return="1")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_open_trade()
    runner.day_slot.current_trade = trade

    runner.day_slot._check_open_trade(Bar(timestamp=DAY, open=115.0, high=120.0, low=114.5, close=119.5))

    assert broker.modify_stop_price_calls == []
    assert trade.breakeven_moved is False


def test_tolerates_the_broker_raising_and_leaves_the_original_stop_tracked(tmp_path, capsys):
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    broker.raise_on_modify_stop_price = RuntimeError("modify failed")
    runner = Runner(cfg, broker, logger=TradeLogger(path=str(tmp_path / "trades.csv")))
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_open_trade()
    runner.day_slot.current_trade = trade

    runner.day_slot._check_open_trade(
        Bar(timestamp=DAY, open=105.0, high=110.0, low=104.5, close=109.5)
    )  # must not raise

    assert trade.breakeven_moved is False
    assert trade.stop_price == 90.0  # unchanged -- still the real, original stop
    assert "failed to move stop to breakeven" in capsys.readouterr().out


def test_a_stop_hit_after_breakeven_was_moved_logs_as_breakeven_not_stop(tmp_path):
    """Confirmed this matters for real: _close_current_trade's stop-side
    exit_price comes from trade.stop_price, which is the *new* breakeven
    level once moved -- logging it as a plain "stop" would misleadingly
    suggest the original, larger loss instead of the small real win it
    actually is."""
    cfg = load_test_config()
    broker = FakeBroker(order_id_to_return="1")
    logger = TradeLogger(path=str(tmp_path / "trades.csv"))
    runner = Runner(cfg, broker, logger=logger)
    runner.day_slot.current_order_id = "bracket-1"
    trade = make_open_trade()
    runner.day_slot.current_trade = trade

    # Move to breakeven first.
    runner.day_slot._check_open_trade(Bar(timestamp=DAY, open=105.0, high=110.0, low=104.5, close=109.5))
    assert trade.breakeven_moved is True

    # Now the (moved) stop gets hit.
    broker.poll_order_status = lambda order_id: "filled_stop"
    runner.day_slot._check_open_trade(Bar(timestamp=DAY + timedelta(minutes=1), open=109.0, high=109.5, low=109.0, close=109.0))

    assert runner.day_slot.current_trade is None  # closed
    logged = logger.path.read_text()
    assert ",breakeven," in logged
    assert trade.exit_price == 110.0  # the moved stop, not the original 90.0
    assert trade.pnl_dollars(cfg.instrument.point_value) == pytest.approx(20.0)  # a small real win, not a loss
