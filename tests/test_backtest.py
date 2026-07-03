import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.backtest import _pnl_points, export_chart_json, run_backtest
from src.config import load_config
from src.models import Bar

TZ = ZoneInfo("America/New_York")
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bar(minutes_from_open: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=DAY + timedelta(minutes=minutes_from_open), open=o, high=h, low=l, close=c)


def load_test_config():
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def breakout_retest_fvg_bars() -> list[Bar]:
    """Same setup used in test_strategy.py: box 9:30-9:44, breakout at 9:46,
    retest at 9:48, strong bullish FVG confirmed at 9:51 (entry @ 104.6,
    structural stop lands on the box high at 101.0, so target is 111.8)."""
    bars = []
    for i in range(15):
        bars.append(bar(i, 100.0, 101.0, 99.5, 100.5))
    bars.append(bar(15, 100.5, 101.2, 100.0, 100.8))
    bars.append(bar(16, 100.8, 102.5, 100.7, 102.3))
    bars.append(bar(17, 102.3, 103.0, 101.5, 102.8))
    bars.append(bar(18, 102.8, 103.0, 100.8, 101.5))
    bars.append(bar(19, 101.5, 101.8, 101.0, 101.6))
    bars.append(bar(20, 101.6, 104.6, 101.5, 104.5))
    bars.append(bar(21, 104.5, 104.8, 104.0, 104.6))
    return bars


def test_backtest_records_a_win():
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    # Runs up to the target (111.8) without dipping to the stop (101.0) first.
    bars.append(bar(22, 104.6, 112.0, 104.0, 111.9))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["date"] == DAY.date()
    assert results[0]["stop_price"] == 101.0
    assert results[0]["target_price"] == pytest.approx(111.8)
    assert results[0]["box_high"] == 101.0
    assert results[0]["box_low"] == 99.5
    assert results[0]["fvg_gap_low"] == pytest.approx(101.8)
    assert results[0]["fvg_gap_high"] == pytest.approx(104.0)
    # No prior-day/Asia/London bars were fed in this synthetic scenario.
    assert results[0]["previous_day_high"] is None
    assert results[0]["asia_high"] is None
    assert results[0]["london_high"] is None


def test_backtest_records_a_loss():
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    # Drops to the stop (101.0) without reaching the target (111.8) first.
    bars.append(bar(22, 104.6, 105.0, 100.5, 100.8))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is False


def test_backtest_reports_no_trades_when_nothing_triggers():
    cfg = load_test_config()
    flat_bars = [bar(i, 100.0, 100.2, 99.8, 100.0) for i in range(30)]

    results = run_backtest(cfg, flat_bars)

    assert results == []


def test_pnl_points_is_negative_for_a_long_loss():
    trade = {"direction": "long", "entry_price": 104.6, "stop_price": 101.0, "target_price": 111.8, "won": False}
    assert _pnl_points(trade) == pytest.approx(-3.6)


def test_pnl_points_is_positive_for_a_long_win():
    trade = {"direction": "long", "entry_price": 104.6, "stop_price": 101.0, "target_price": 111.8, "won": True}
    assert _pnl_points(trade) == pytest.approx(7.2)


def test_pnl_points_is_negative_for_a_short_loss():
    trade = {"direction": "short", "entry_price": 100.0, "stop_price": 103.0, "target_price": 94.0, "won": False}
    assert _pnl_points(trade) == pytest.approx(-3.0)


def test_pnl_points_is_positive_for_a_short_win():
    trade = {"direction": "short", "entry_price": 100.0, "stop_price": 103.0, "target_price": 94.0, "won": True}
    assert _pnl_points(trade) == pytest.approx(6.0)


def test_export_chart_json_writes_candles_and_levels(tmp_path):
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    bars.append(bar(22, 104.6, 112.0, 104.0, 111.9))

    results = run_backtest(cfg, bars)
    out_path = tmp_path / "chart.json"
    export_chart_json(cfg, results, bars, str(out_path))

    payload = json.loads(out_path.read_text())

    assert len(payload) == 1
    trade = payload[0]
    assert trade["date"] == str(DAY.date())
    assert trade["direction"] == "long"
    assert trade["won"] is True
    assert trade["box_high"] == 101.0
    assert trade["box_low"] == 99.5
    # Candles should span from 9:30 through the exit bar (9:52, the last
    # bar available -- window_end reaches 9:57 but there are no more bars).
    assert trade["candles"][0]["t"] == "09:30"
    assert trade["candles"][-1]["t"] == "09:52"
    assert len(trade["candles"]) == 23
