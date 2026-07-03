import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.backtest import _pnl_points, export_chart_json, run_backtest
from src.config import load_config
from src.models import Bar

TZ = ZoneInfo("America/New_York")
PREV_DAY = datetime(2026, 7, 5, 7, 0, tzinfo=TZ)
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bar(minutes_from_open: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=DAY + timedelta(minutes=minutes_from_open), open=o, high=h, low=l, close=c)


def load_test_config():
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def breakout_key_level_fvg_bars() -> list[Bar]:
    """Same setup used in test_strategy.py: previous-day high/low of 105/95,
    box 9:30-9:45 (high=101/low=99.5), breakout above the box, a strong
    bullish FVG (gap 103.6-106.0) containing the previous-day high (105),
    and a retrace bar that fills the resulting limit order at the FVG
    midpoint (104.8)."""
    bars = [Bar(timestamp=PREV_DAY, open=100.0, high=105.0, low=95.0, close=100.0)]
    for i in range(15):
        bars.append(bar(i, 100.0, 101.0, 99.5, 100.5))
    bars.append(bar(15, 100.5, 101.2, 100.0, 100.8))
    bars.append(bar(16, 100.8, 103.0, 100.7, 102.5))
    for i in range(17, 37):
        bars.append(bar(i, 103.0, 103.5, 102.5, 103.0))
    bars.append(bar(37, 103.0, 103.6, 102.7, 103.3))
    bars.append(bar(38, 103.3, 107.2, 103.2, 107.0))
    bars.append(bar(39, 107.0, 107.5, 106.0, 107.3))
    bars.append(bar(40, 107.3, 107.5, 104.5, 105.0))  # fills the 104.8 limit
    return bars


def test_backtest_records_a_win():
    cfg = load_test_config()
    bars = breakout_key_level_fvg_bars()
    # Runs up to the target (112.4) without dipping to the stop (101.0) first.
    bars.append(bar(41, 105.0, 113.0, 104.8, 112.5))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["date"] == DAY.date()
    assert results[0]["entry_price"] == 104.8
    assert results[0]["stop_price"] == 101.0
    assert results[0]["target_price"] == pytest.approx(112.4)


def test_backtest_records_a_loss():
    cfg = load_test_config()
    bars = breakout_key_level_fvg_bars()
    # Drops to the stop (101.0) without reaching the target (112.4) first.
    bars.append(bar(41, 105.0, 105.5, 100.5, 101.0))

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
    bars = breakout_key_level_fvg_bars()
    bars.append(bar(41, 105.0, 113.0, 104.8, 112.5))

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
    assert trade["previous_day_high"] == 105.0
    assert trade["previous_day_low"] == 95.0
    # Candles should span from 9:30 through the exit bar.
    assert trade["candles"][0]["t"] == "09:30"
    assert len(trade["candles"]) > 0
