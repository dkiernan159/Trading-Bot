import csv
from datetime import datetime
from zoneinfo import ZoneInfo

from src.logger import TradeLogger
from src.models import Direction, Trade

TZ = ZoneInfo("America/New_York")


def make_trade(pnl_positive: bool = True, stop_source: str = "swing", stop_fvg_size: float | None = None) -> Trade:
    trade = Trade(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=90.0,
        target_price=110.0,
        contracts=1,
        entry_time=datetime(2026, 7, 7, 9, 45, tzinfo=TZ),
        stop_source=stop_source,
        stop_fvg_size=stop_fvg_size,
    )
    trade.exit_price = 110.0 if pnl_positive else 90.0
    trade.exit_time = datetime(2026, 7, 7, 10, 0, tzinfo=TZ)
    trade.exit_reason = "target" if pnl_positive else "stop"
    return trade


def test_log_trade_writes_the_strategy_column(tmp_path):
    path = tmp_path / "trades.csv"
    logger = TradeLogger(path=str(path))

    logger.log_trade(make_trade(), point_value=2.0, strategy="overnight")

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["strategy"] == "overnight"


def test_log_trade_writes_the_stop_source_and_fvg_size_columns(tmp_path):
    """Added 2026-07-14 so a live trade's losses can actually be analyzed
    for a pattern (fvg/swing/cap) the same way backtest's --verbose
    already could -- the user asked to analyze real losing trades and
    trades.csv had no way to tell which stop rule produced each one."""
    path = tmp_path / "trades.csv"
    logger = TradeLogger(path=str(path))

    logger.log_trade(make_trade(stop_source="cap", stop_fvg_size=None), point_value=2.0, strategy="overnight")
    logger.log_trade(make_trade(stop_source="fvg", stop_fvg_size=14.5), point_value=2.0, strategy="day")

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["stop_source"] == "cap"
    assert rows[0]["stop_fvg_size"] == ""
    assert rows[1]["stop_source"] == "fvg"
    assert rows[1]["stop_fvg_size"] == "14.5"


def test_existing_file_without_strategy_column_is_migrated(tmp_path):
    """A trades.csv written before per-strategy tagging existed (e.g. the
    live one already deployed) has an old-format header with no "strategy"
    column and possibly old data rows -- must gain the new column without
    corrupting those rows."""
    path = tmp_path / "trades.csv"
    old_header = "entry_time,direction,contracts,entry_price,stop_price,target_price,exit_price,exit_time,exit_reason,pnl_points,pnl_dollars\n"
    old_row = "2026-07-06T09:45:00-04:00,long,1,100.0,90.0,110.0,110.0,2026-07-06T10:00:00-04:00,target,10.0,20.0\n"
    path.write_text(old_header + old_row)

    logger = TradeLogger(path=str(path))
    logger.log_trade(make_trade(), point_value=2.0, strategy="day")

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    # Old row has no strategy/stop_source/stop_fvg_size values written --
    # tolerated as None, not a crash.
    assert rows[0]["strategy"] is None
    assert rows[0]["stop_source"] is None
    assert rows[0]["pnl_dollars"] == "20.0"
    # New row logged after migration has the real values.
    assert rows[1]["strategy"] == "day"
    assert rows[1]["stop_source"] == "swing"


def test_existing_file_with_strategy_but_no_stop_source_is_migrated(tmp_path):
    """A trades.csv written after per-strategy tagging but before
    stop_source/stop_fvg_size existed (i.e. the live one deployed between
    2026-07-07 and 2026-07-14) -- must also gain the new trailing columns
    without corrupting existing rows."""
    path = tmp_path / "trades.csv"
    old_header = (
        "entry_time,direction,contracts,entry_price,stop_price,target_price,"
        "exit_price,exit_time,exit_reason,pnl_points,pnl_dollars,strategy\n"
    )
    old_row = "2026-07-13T00:55:00+00:00,short,1,29842.0,29905.25,29715.5,29715.5,2026-07-13T02:59:00+00:00,target,126.5,253.0,overnight\n"
    path.write_text(old_header + old_row)

    logger = TradeLogger(path=str(path))
    logger.log_trade(make_trade(stop_source="cap"), point_value=2.0, strategy="day")

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["strategy"] == "overnight"
    assert rows[0]["stop_source"] is None
    assert rows[1]["stop_source"] == "cap"


def test_migration_is_a_no_op_when_file_already_has_the_current_header(tmp_path):
    path = tmp_path / "trades.csv"
    logger = TradeLogger(path=str(path))
    logger.log_trade(make_trade(), point_value=2.0, strategy="day")

    # Re-instantiating (as happens on every bot restart) must not touch existing rows.
    TradeLogger(path=str(path))

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["strategy"] == "day"
