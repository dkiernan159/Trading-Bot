import json
from pathlib import Path

from src.dashboard import read_live_trades, read_status

_HEADER = "entry_time,direction,contracts,entry_price,stop_price,target_price,exit_price,exit_time,exit_reason,pnl_points,pnl_dollars\n"


def test_read_live_trades_returns_empty_list_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.csv"
    assert read_live_trades(str(missing_path)) == []


def test_read_live_trades_parses_rows_and_sorts_newest_first(tmp_path):
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        _HEADER
        + "2026-06-24T09:50:00-04:00,short,1,29713.0,29728.0,29683.0,29728.0,2026-06-24T10:10:00-04:00,stop,-15.0,-30.0\n"
        + "2026-07-01T09:51:00-04:00,long,1,30202.5,30187.5,30232.5,30232.5,2026-07-01T10:15:00-04:00,target,30.0,60.0\n"
    )

    rows = read_live_trades(str(csv_path))

    assert len(rows) == 2
    # Most recent entry_time first.
    assert rows[0]["entry_time"] == "2026-07-01T09:51:00-04:00"
    assert rows[0]["direction"] == "long"
    assert rows[0]["contracts"] == 1
    assert rows[0]["exit_reason"] == "target"
    assert rows[0]["pnl_dollars"] == 60.0
    assert rows[1]["entry_time"] == "2026-06-24T09:50:00-04:00"
    assert rows[1]["pnl_dollars"] == -30.0


def test_read_status_returns_none_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    assert read_status(str(missing_path)) is None


def test_read_status_parses_the_written_status(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"last_bar_time": "2026-07-07T02:00:00+00:00", "day": {"state": "WAIT_FILL"}}))

    status = read_status(str(status_path))

    assert status["last_bar_time"] == "2026-07-07T02:00:00+00:00"
    assert status["day"]["state"] == "WAIT_FILL"


def test_read_status_tolerates_a_torn_write(tmp_path):
    """Runner writes the file non-atomically -- a read that lands mid-write
    could see a truncated/invalid JSON file. Must not crash the dashboard;
    the next poll gets a clean copy once the write finishes."""
    status_path = tmp_path / "status.json"
    status_path.write_text('{"last_bar_time": "2026-07-07T02:00', encoding="utf-8")

    assert read_status(str(status_path)) is None
