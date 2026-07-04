import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.backtest import _pnl_points, export_chart_html, export_chart_json, run_backtest
from src.config import load_config
from src.models import Bar

TZ = ZoneInfo("America/New_York")
PREV_DAY_BASE = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bar(minutes_from_open: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=DAY + timedelta(minutes=minutes_from_open), open=o, high=h, low=l, close=c)


def load_test_config():
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def breakout_key_level_fvg_bars() -> list[Bar]:
    """Same setup used in test_strategy.py: previous-day high of 105 (set by
    a 15m candle spanning 100-105) and low of 95 (spanning 95-101), box
    9:30-9:45 (high=101/low=99.5), breakout above the box, a strong bullish
    FVG (gap 101.5-104.0) that overlaps the previous-day-high zone without
    containing the exact tick (105), and a retrace bar that fills the
    resulting limit order at the FVG midpoint (102.75)."""
    bars = [
        Bar(timestamp=PREV_DAY_BASE, open=100.0, high=101.0, low=99.0, close=100.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=15), open=100.0, high=105.0, low=100.0, close=104.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=30), open=100.0, high=101.0, low=95.0, close=98.0),
    ]
    for i in range(15):
        bars.append(bar(i, 100.0, 101.0, 99.5, 100.5))
    bars.append(bar(15, 100.5, 101.2, 100.0, 100.8))
    bars.append(bar(16, 100.8, 103.0, 100.7, 102.5))
    for i in range(17, 37):
        bars.append(bar(i, 103.0, 103.5, 102.5, 103.0))
    bars.append(bar(37, 101.2, 101.5, 101.0, 101.4))
    bars.append(bar(38, 101.4, 105.2, 101.3, 105.0))
    bars.append(bar(39, 105.0, 105.3, 104.0, 105.1))
    bars.append(bar(40, 105.1, 105.5, 102.5, 103.0))  # fills the 102.75 limit
    return bars


def test_backtest_records_a_win():
    cfg = load_test_config()
    bars = breakout_key_level_fvg_bars()
    # Runs up to the target (106.25) without dipping to the stop (101.0) first.
    bars.append(bar(41, 103.0, 107.0, 102.8, 106.5))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["date"] == DAY.date()
    assert results[0]["entry_price"] == 102.75
    assert results[0]["stop_price"] == 101.0
    assert results[0]["target_price"] == pytest.approx(106.25)


def test_backtest_records_a_loss():
    cfg = load_test_config()
    bars = breakout_key_level_fvg_bars()
    # Drops to the stop (101.0) without reaching the target (106.25) first.
    bars.append(bar(41, 103.0, 103.2, 100.5, 101.0))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is False


def test_backtest_reports_no_trades_when_nothing_triggers():
    cfg = load_test_config()
    flat_bars = [bar(i, 100.0, 100.2, 99.8, 100.0) for i in range(30)]

    results = run_backtest(cfg, flat_bars)

    assert results == []


def test_funnel_stats_track_each_gate():
    cfg = load_test_config()
    bars = breakout_key_level_fvg_bars()
    bars.append(bar(41, 103.0, 107.0, 102.8, 106.5))

    stats: dict = {}
    run_backtest(cfg, bars, stats_out=stats)

    assert stats == {
        "breakouts": 1,
        "strong_fvgs_in_direction": 1,
        "fvgs_at_key_level": 1,
        "fills": 1,
    }


def test_funnel_stats_show_fvg_found_but_no_key_level_match():
    """A breakout with a strong, correctly-directed FVG that never overlaps
    a key level should show up as a near-miss: breakout + FVG counted, but
    zero at fvgs_at_key_level and zero fills."""
    cfg = load_test_config()
    bars = [
        Bar(timestamp=PREV_DAY_BASE, open=100.0, high=101.0, low=99.0, close=100.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=15), open=100.0, high=105.0, low=100.0, close=104.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=30), open=100.0, high=101.0, low=95.0, close=98.0),
    ]
    for i in range(15):
        bars.append(bar(i, 100.0, 101.0, 99.5, 100.5))
    bars.append(bar(15, 100.5, 101.2, 100.0, 100.8))
    bars.append(bar(16, 100.8, 103.0, 100.7, 102.5))
    for i in range(17, 37):
        bars.append(bar(i, 103.0, 103.5, 102.5, 103.0))
    bars.append(bar(37, 109.5, 110.0, 109.3, 109.8))
    bars.append(bar(38, 109.8, 114.2, 109.7, 114.0))
    bars.append(bar(39, 114.0, 114.5, 113.0, 114.2))

    stats: dict = {}
    results = run_backtest(cfg, bars, stats_out=stats)

    assert results == []
    assert stats["breakouts"] == 1
    assert stats["strong_fvgs_in_direction"] == 1
    assert stats["fvgs_at_key_level"] == 0
    assert stats["fills"] == 0


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
    bars.append(bar(41, 103.0, 107.0, 102.8, 106.5))

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


def test_export_chart_html_embeds_trade_data(tmp_path):
    cfg = load_test_config()
    bars = breakout_key_level_fvg_bars()
    bars.append(bar(41, 103.0, 107.0, 102.8, 106.5))

    results = run_backtest(cfg, bars)
    out_path = tmp_path / "chart.html"
    export_chart_html(cfg, results, bars, str(out_path))

    html = out_path.read_text()

    assert "__TRADE_DATA__" not in html  # placeholder was substituted
    assert '"date": "2026-07-06"' in html
    assert "<svg" not in html  # charts are built client-side by the embedded script, not pre-rendered
    assert "function buildChart" in html


def test_export_chart_html_handles_no_trades(tmp_path):
    cfg = load_test_config()
    flat_bars = [bar(i, 100.0, 100.2, 99.8, 100.0) for i in range(30)]

    results = run_backtest(cfg, flat_bars)
    out_path = tmp_path / "chart.html"
    export_chart_html(cfg, results, flat_bars, str(out_path))

    html = out_path.read_text()
    assert "__TRADE_DATA__" not in html
    assert "const TRADES = [];" in html
