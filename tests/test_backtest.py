import json
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.backtest import _pnl_points, export_chart_html, export_chart_json, run_backtest
from src.config import load_config
from src.models import Bar

TZ = ZoneInfo("America/New_York")
PREV_DAY_BASE = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bar15(k: int, o: float, h: float, l: float, c: float) -> Bar:
    """Bar `k` stands in for one whole 15-minute candle -- DAY + 15*k
    minutes -- since the FVG detector runs on 15m candles, not 1m."""
    return Bar(timestamp=DAY + timedelta(minutes=15 * k), open=o, high=h, low=l, close=c)


def load_test_config():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    # See tests/test_strategy.py: these fixtures span several 15-minute
    # candles, past the real 11:30 ET cutoff -- push it out so the cutoff
    # isn't what's under test here.
    cfg.session.no_new_entries_after = dtime(23, 59)
    return cfg


def breakout_retest_fvg_bars() -> list[Bar]:
    """Previous-day high of 105 and low of 95, box 9:30-9:45 (high=101/
    low=99.5), breakout above the box, a retest of the previous-day high
    (105) at bar 3, 8 quiet 15m candles to establish the average-range
    baseline, then a strong 15m FVG (gap 105.4-109.0) and a retrace bar
    that fills the resulting limit order at the FVG midpoint (107.2)."""
    bars = [
        Bar(timestamp=PREV_DAY_BASE, open=100.0, high=101.0, low=99.0, close=100.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=15), open=100.0, high=105.0, low=100.0, close=104.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=30), open=100.0, high=101.0, low=95.0, close=98.0),
    ]
    bars.append(bar15(0, 100.0, 101.0, 99.5, 100.5))  # box
    bars.append(bar15(1, 100.5, 101.2, 100.0, 100.8))  # box formed
    bars.append(bar15(2, 100.8, 103.0, 100.7, 102.5))  # breakout
    bars.append(bar15(3, 102.5, 105.5, 102.3, 105.0))  # retest of previous-day high (105)
    for k in range(4, 12):
        bars.append(bar15(k, 105.0, 105.5, 104.5, 105.0))  # baseline
    bars.append(bar15(12, 105.0, 105.4, 104.7, 105.1))  # c0
    bars.append(bar15(13, 105.1, 109.5, 105.0, 109.3))  # c1: displacement
    bars.append(bar15(14, 109.3, 109.8, 109.0, 109.5))  # c2: confirms gap 105.4-109.0
    bars.append(bar15(15, 109.5, 109.6, 109.4, 109.5))  # flush -- detects the FVG
    bars.append(bar15(16, 109.5, 109.8, 106.0, 107.5))  # fills the 107.2 limit
    return bars


def test_backtest_records_a_win():
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    # Runs up to the target (111.6) without dipping to the stop (105.0) first.
    bars.append(bar15(17, 107.5, 112.0, 107.3, 111.8))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["date"] == DAY.date()
    assert results[0]["entry_price"] == 107.2
    assert results[0]["stop_price"] == 105.0
    assert results[0]["target_price"] == pytest.approx(111.6)


def test_backtest_records_a_loss():
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    # Drops to the stop (105.0) without reaching the target (111.6) first.
    bars.append(bar15(17, 107.5, 107.7, 104.5, 105.0))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is False


def test_backtest_reports_no_trades_when_nothing_triggers():
    cfg = load_test_config()
    flat_bars = [bar15(k, 100.0, 100.2, 99.8, 100.0) for k in range(30)]

    results = run_backtest(cfg, flat_bars)

    assert results == []


def test_funnel_stats_track_each_gate():
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    bars.append(bar15(17, 107.5, 112.0, 107.3, 111.8))

    stats: dict = {}
    run_backtest(cfg, bars, stats_out=stats)

    assert stats == {
        "breakouts": 1,
        "key_level_retests": 1,
        "strong_fvgs_after_retest": 1,
        "fills": 1,
        "fvgs_mitigated_before_fill": 0,
    }


def test_funnel_stats_show_retest_but_no_fvg_afterward():
    """A breakout that retests a key level but never gets a qualifying FVG
    afterward should show up as a near-miss: breakout + retest counted,
    zero strong_fvgs_after_retest, zero fills."""
    cfg = load_test_config()
    bars = [
        Bar(timestamp=PREV_DAY_BASE, open=100.0, high=101.0, low=99.0, close=100.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=15), open=100.0, high=105.0, low=100.0, close=104.0),
        Bar(timestamp=PREV_DAY_BASE + timedelta(minutes=30), open=100.0, high=101.0, low=95.0, close=98.0),
    ]
    bars.append(bar15(0, 100.0, 101.0, 99.5, 100.5))
    bars.append(bar15(1, 100.5, 101.2, 100.0, 100.8))
    bars.append(bar15(2, 100.8, 103.0, 100.7, 102.5))
    bars.append(bar15(3, 102.5, 105.5, 102.3, 105.0))  # retest
    for k in range(4, 20):
        bars.append(bar15(k, 105.0, 105.5, 104.5, 105.0))  # flat, no FVG ever forms

    stats: dict = {}
    results = run_backtest(cfg, bars, stats_out=stats)

    assert results == []
    assert stats["breakouts"] == 1
    assert stats["key_level_retests"] == 1
    assert stats["strong_fvgs_after_retest"] == 0
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
    bars = breakout_retest_fvg_bars()
    bars.append(bar15(17, 107.5, 112.0, 107.3, 111.8))

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
    # entry_time should exactly match one of the candle labels, so the
    # chart can find that candle and place a marker there.
    assert trade["entry_time"] == "13:30"
    assert any(c["t"] == trade["entry_time"] for c in trade["candles"])
    # Candles should span from 9:30 through the exit bar.
    assert trade["candles"][0]["t"] == "09:30"
    assert len(trade["candles"]) > 0


def test_export_chart_html_embeds_trade_data(tmp_path):
    cfg = load_test_config()
    bars = breakout_retest_fvg_bars()
    bars.append(bar15(17, 107.5, 112.0, 107.3, 111.8))

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
    flat_bars = [bar15(k, 100.0, 100.2, 99.8, 100.0) for k in range(30)]

    results = run_backtest(cfg, flat_bars)
    out_path = tmp_path / "chart.html"
    export_chart_html(cfg, results, flat_bars, str(out_path))

    html = out_path.read_text()
    assert "__TRADE_DATA__" not in html
    assert "const TRADES = [];" in html
