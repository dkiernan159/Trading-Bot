import csv
from datetime import datetime
from zoneinfo import ZoneInfo

from src.logger import TradeLogger
from src.models import Direction, Trade

TZ = ZoneInfo("America/New_York")


def make_trade(pnl_positive: bool = True) -> Trade:
    trade = Trade(
        direction=Direction.LONG,
        entry_price=100.0,
        stop_price=90.0,
        target_price=110.0,
        contracts=1,
        entry_time=datetime(2026, 7, 7, 9, 45, tzinfo=TZ),
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
    # Old row has no strategy value written -- tolerated as None, not a crash.
    assert rows[0]["strategy"] is None
    assert rows[0]["pnl_dollars"] == "20.0"
    # New row logged after migration has the real value.
    assert rows[1]["strategy"] == "day"


def test_migration_is_a_no_op_when_file_already_has_the_strategy_column(tmp_path):
    path = tmp_path / "trades.csv"
    logger = TradeLogger(path=str(path))
    logger.log_trade(make_trade(), point_value=2.0, strategy="day")

    # Re-instantiating (as happens on every bot restart) must not touch existing rows.
    TradeLogger(path=str(path))

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["strategy"] == "day"
