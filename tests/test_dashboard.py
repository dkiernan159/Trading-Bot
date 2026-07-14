import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.dashboard import compute_trade_stats, read_live_trades, read_status

_HEADER = "entry_time,direction,contracts,entry_price,stop_price,target_price,exit_price,exit_time,exit_reason,pnl_points,pnl_dollars,strategy\n"


def test_read_live_trades_returns_empty_list_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.csv"
    assert read_live_trades(str(missing_path)) == []


def test_read_live_trades_parses_rows_and_sorts_newest_first(tmp_path):
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        _HEADER
        + "2026-06-24T09:50:00-04:00,short,1,29713.0,29728.0,29683.0,29728.0,2026-06-24T10:10:00-04:00,stop,-15.0,-30.0,day\n"
        + "2026-07-01T09:51:00-04:00,long,1,30202.5,30187.5,30232.5,30232.5,2026-07-01T10:15:00-04:00,target,30.0,60.0,overnight\n"
    )

    rows = read_live_trades(str(csv_path))

    assert len(rows) == 2
    # Most recent entry_time first.
    assert rows[0]["entry_time"] == "2026-07-01T09:51:00-04:00"
    assert rows[0]["direction"] == "long"
    assert rows[0]["contracts"] == 1
    assert rows[0]["exit_reason"] == "target"
    assert rows[0]["pnl_dollars"] == 60.0
    assert rows[0]["strategy"] == "overnight"
    assert rows[1]["entry_time"] == "2026-06-24T09:50:00-04:00"
    assert rows[1]["pnl_dollars"] == -30.0
    assert rows[1]["strategy"] == "day"


def test_read_live_trades_parses_stop_source_and_fvg_size(tmp_path):
    """Added 2026-07-14 alongside logger.py's new trailing columns, so a
    live trade's losses can actually be analyzed for a pattern."""
    header = (
        "entry_time,direction,contracts,entry_price,stop_price,target_price,"
        "exit_price,exit_time,exit_reason,pnl_points,pnl_dollars,strategy,"
        "stop_source,stop_fvg_size\n"
    )
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        header
        + "2026-07-13T19:20:00-04:00,short,1,29391.75,29491.75,29191.75,29491.75,2026-07-14T00:51:00-04:00,stop,-100.0,-200.0,overnight,cap,\n"
        + "2026-07-13T22:02:00-04:00,short,1,29464.5,29504.25,29385.0,29385.0,2026-07-13T22:21:00-04:00,target,79.5,159.0,overnight,fvg,14.5\n"
    )

    rows = read_live_trades(str(csv_path))

    assert len(rows) == 2
    cap_row = next(r for r in rows if r["stop_source"] == "cap")
    assert cap_row["stop_fvg_size"] is None
    fvg_row = next(r for r in rows if r["stop_source"] == "fvg")
    assert fvg_row["stop_fvg_size"] == 14.5


def test_read_live_trades_defaults_stop_source_to_unknown_for_old_rows(tmp_path):
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        _HEADER
        + "2026-06-24T09:50:00-04:00,short,1,29713.0,29728.0,29683.0,29728.0,2026-06-24T10:10:00-04:00,stop,-15.0,-30.0,day\n"
    )

    rows = read_live_trades(str(csv_path))

    assert rows[0]["stop_source"] == "unknown"
    assert rows[0]["stop_fvg_size"] is None


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


ET = ZoneInfo("America/New_York")


def _row(entry_time: str, pnl_dollars: float, strategy: str) -> dict:
    return {"entry_time": entry_time, "pnl_dollars": pnl_dollars, "strategy": strategy}


def test_compute_trade_stats_on_no_trades_returns_none_rate_fields_not_zero():
    stats = compute_trade_stats([], ET)

    assert stats["overall"]["trades"] == 0
    assert stats["overall"]["win_rate"] is None
    assert stats["overall"]["profit_factor"] is None
    assert stats["overall"]["total_pnl_dollars"] == 0.0


def test_compute_trade_stats_overall_and_per_strategy_breakdown():
    rows = [
        _row("2026-07-01T09:50:00-04:00", 60.0, "day"),
        _row("2026-07-01T10:00:00-04:00", -30.0, "day"),
        _row("2026-07-02T02:00:00-04:00", 45.0, "overnight"),
    ]

    stats = compute_trade_stats(rows, ET)

    assert stats["overall"]["trades"] == 3
    assert stats["overall"]["wins"] == 2
    assert stats["overall"]["losses"] == 1
    assert stats["overall"]["win_rate"] == 2 / 3
    assert stats["overall"]["total_pnl_dollars"] == 75.0
    assert stats["overall"]["avg_win_dollars"] == (60.0 + 45.0) / 2
    assert stats["overall"]["avg_loss_dollars"] == 30.0
    assert stats["overall"]["profit_factor"] == (60.0 + 45.0) / 30.0
    assert stats["overall"]["best_trade_dollars"] == 60.0
    assert stats["overall"]["worst_trade_dollars"] == -30.0

    assert stats["day"]["trades"] == 2
    assert stats["day"]["total_pnl_dollars"] == 30.0
    assert stats["overnight"]["trades"] == 1
    assert stats["overnight"]["total_pnl_dollars"] == 45.0


def test_compute_trade_stats_today_slice_uses_the_given_timezone(monkeypatch):
    """"Today" must follow the bot's own session timezone (ET), not UTC or
    the server's local clock -- otherwise a trade near midnight ET could be
    counted on the wrong day."""
    import src.dashboard as dashboard_module
    from datetime import datetime as real_datetime

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 7, 7, 12, 0, tzinfo=tz)

    monkeypatch.setattr(dashboard_module, "datetime", FrozenDatetime)

    rows = [
        _row("2026-07-07T09:50:00-04:00", 60.0, "day"),  # today, ET
        _row("2026-07-06T23:50:00-04:00", -30.0, "overnight"),  # yesterday, ET
    ]

    stats = compute_trade_stats(rows, ET)

    assert stats["today"]["trades"] == 1
    assert stats["today"]["total_pnl_dollars"] == 60.0
