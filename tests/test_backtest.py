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

WICK = 0.2  # wide enough that a smooth 1-minute walk never forms its own 1-minute-scale gap


def bar_at(dt: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=dt, open=o, high=h, low=l, close=c)


def flat_bar(dt: datetime, price: float, spread: float = 0.5) -> Bar:
    """A single bar standing in for a whole quiet, unchanging 15-minute
    candle -- safe to use sparsely since a repeated, unchanging value
    can't form a gap, so it doesn't also register as a false FVG."""
    return bar_at(dt, price, price + spread / 2, price - spread / 2, price)


def smooth_walk_1m(start: datetime, minutes: int, start_price: float, end_price: float) -> list[Bar]:
    """`minutes` consecutive real 1-minute bars walking smoothly from
    start_price to end_price -- gentle enough relative to WICK that no 3
    consecutive bars form their own 1-minute gap, so this same price
    action can build a genuine 15m (or 5m) candle without also
    registering as a contaminating 1-minute-scale FVG."""
    bars = []
    for i in range(minutes):
        o = start_price + (end_price - start_price) * i / minutes
        c = start_price + (end_price - start_price) * (i + 1) / minutes
        h, l = (o, c) if o >= c else (c, o)
        bars.append(bar_at(start + timedelta(minutes=i), o, h + WICK, l - WICK, c))
    return bars


def load_test_config():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    # See tests/test_strategy.py: these fixtures span well past the real
    # 12:30 ET cutoff -- push it out so the cutoff isn't what's under
    # test here.
    cfg.session.no_new_entries_after = dtime(23, 59)
    cfg.strategy.entry_fvg.lookback_bars = 5
    return cfg


def breakout_15m_anchor_nested_5m_bars() -> list[Bar]:
    """Previous-day high of 105 and low of 95, box 9:30-9:45 (high=101/
    low=99.5), breakout above the box, 8 quiet 15m baseline candles, a
    real displacement move from 105.1 to 109.3 that forms a large 15m FVG
    anchor (gap 105.3-109.1), then a small 5m FVG nested inside it (gap
    106.3-107.9) whose midpoint (107.1) is where the resulting limit
    order fills."""
    bars = [
        bar_at(PREV_DAY_BASE, 100.0, 101.0, 99.0, 100.0),
        bar_at(PREV_DAY_BASE + timedelta(minutes=15), 100.0, 105.0, 100.0, 104.0),
        bar_at(PREV_DAY_BASE + timedelta(minutes=30), 100.0, 101.0, 95.0, 98.0),
    ]
    bars.append(bar_at(DAY, 100.0, 101.0, 99.5, 100.5))  # box
    bars.append(bar_at(DAY + timedelta(minutes=15), 100.5, 101.2, 100.0, 100.8))  # box formed
    bars.append(bar_at(DAY + timedelta(minutes=30), 100.8, 103.0, 100.7, 102.5))  # breakout

    baseline_start = DAY + timedelta(minutes=45)
    for i in range(8):
        bars.append(flat_bar(baseline_start + timedelta(minutes=15 * i), 105.0))  # 15m baseline

    pattern_start = baseline_start + timedelta(minutes=15 * 8)
    bars += smooth_walk_1m(pattern_start, 15, 105.0, 105.1)  # c0
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=15), 15, 105.1, 109.3)  # c1: displacement
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=30), 15, 109.3, 109.5)  # c2: confirms gap
    bars.append(flat_bar(pattern_start + timedelta(minutes=45), 109.5))  # flush -- detects the 15m anchor

    # nested_start lands on a 5-minute-aligned boundary (flush + 5) so the
    # dense c0/c1/c2 bars below bucket cleanly into three 5-minute candles.
    nested_start = pattern_start + timedelta(minutes=50)
    bars += smooth_walk_1m(nested_start, 5, 106.0, 106.1)  # c0
    bars += smooth_walk_1m(nested_start + timedelta(minutes=5), 5, 106.1, 108.1)  # c1: displacement
    bars += smooth_walk_1m(nested_start + timedelta(minutes=10), 5, 108.1, 108.3)  # c2: confirms gap
    bars.append(flat_bar(nested_start + timedelta(minutes=15), 108.3))  # flush -- detects it
    bars.append(bar_at(nested_start + timedelta(minutes=16), 108.0, 108.1, 106.3, 106.8))  # fills the 107.1 limit
    return bars


def test_backtest_records_a_win():
    cfg = load_test_config()
    bars = breakout_15m_anchor_nested_5m_bars()
    # Stop is the 15m anchor's own bottom (105.3), the nearest structural
    # level below entry (107.1) -- nearer than the previous-day high (105.0)
    # -- so target is 107.1 + 2*(107.1-105.3) = 110.7.
    # Runs up to the target (110.7) without dipping to the stop (105.3) first.
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.4, 111.0, 107.0, 110.8))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["date"] == DAY.date()
    assert results[0]["entry_price"] == pytest.approx(107.1)
    assert results[0]["stop_price"] == pytest.approx(105.3)
    assert results[0]["target_price"] == pytest.approx(110.7)


def test_backtest_records_a_loss():
    cfg = load_test_config()
    bars = breakout_15m_anchor_nested_5m_bars()
    # Drops to the stop (105.3, the 15m anchor's bottom) without reaching
    # the target (110.7) first.
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 106.8, 107.0, 104.5, 105.0))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is False


def test_backtest_reports_no_trades_when_nothing_triggers():
    cfg = load_test_config()
    flat_bars = [flat_bar(DAY + timedelta(minutes=15 * k), 100.0, spread=0.4) for k in range(30)]

    results = run_backtest(cfg, flat_bars)

    assert results == []


def test_funnel_stats_track_each_gate():
    cfg = load_test_config()
    bars = breakout_15m_anchor_nested_5m_bars()
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 106.8, 110.0, 106.7, 109.9))

    stats: dict = {}
    run_backtest(cfg, bars, stats_out=stats)

    assert stats == {
        "breakouts": 1,
        "breakouts_invalidated": 0,
        "large_15m_fvgs": 1,
        "nested_5m_fvgs": 1,
        "fills": 1,
    }


def test_funnel_stats_show_anchor_but_no_nested_fvg_afterward():
    """A breakout that anchors on a large 15m FVG but never gets a
    qualifying nested 5m FVG inside it should show up as a near-miss:
    breakout + anchor counted, zero nested_5m_fvgs, zero fills."""
    cfg = load_test_config()
    bars = [
        bar_at(PREV_DAY_BASE, 100.0, 101.0, 99.0, 100.0),
        bar_at(PREV_DAY_BASE + timedelta(minutes=15), 100.0, 105.0, 100.0, 104.0),
        bar_at(PREV_DAY_BASE + timedelta(minutes=30), 100.0, 101.0, 95.0, 98.0),
    ]
    bars.append(bar_at(DAY, 100.0, 101.0, 99.5, 100.5))
    bars.append(bar_at(DAY + timedelta(minutes=15), 100.5, 101.2, 100.0, 100.8))
    bars.append(bar_at(DAY + timedelta(minutes=30), 100.8, 103.0, 100.7, 102.5))  # breakout

    baseline_start = DAY + timedelta(minutes=45)
    for i in range(8):
        bars.append(flat_bar(baseline_start + timedelta(minutes=15 * i), 105.0))

    pattern_start = baseline_start + timedelta(minutes=15 * 8)
    bars += smooth_walk_1m(pattern_start, 15, 105.0, 105.1)
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=15), 15, 105.1, 109.3)
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=30), 15, 109.3, 109.5)
    bars.append(flat_bar(pattern_start + timedelta(minutes=45), 109.5))  # flush -- detects the 15m anchor

    flat_after = pattern_start + timedelta(minutes=46)
    for i in range(20):
        bars.append(flat_bar(flat_after + timedelta(minutes=i), 109.5, spread=0.3))  # flat, no 5m FVG ever forms

    stats: dict = {}
    results = run_backtest(cfg, bars, stats_out=stats)

    assert results == []
    assert stats["breakouts"] == 1
    assert stats["large_15m_fvgs"] == 1
    assert stats["nested_5m_fvgs"] == 0
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
    bars = breakout_15m_anchor_nested_5m_bars()
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.4, 111.0, 107.0, 110.8))

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
    assert trade["anchor_gap_low"] == pytest.approx(105.3)
    assert trade["anchor_gap_high"] == pytest.approx(109.1)
    assert trade["fvg_gap_low"] == pytest.approx(106.3)
    assert trade["fvg_gap_high"] == pytest.approx(107.9)
    # entry_time should exactly match one of the candle labels, so the
    # chart can find that candle and place a marker there.
    assert any(c["t"] == trade["entry_time"] for c in trade["candles"])
    # Candles should span from 9:30 through the exit bar.
    assert trade["candles"][0]["t"] == "09:30"
    assert len(trade["candles"]) > 0


def test_export_chart_html_embeds_trade_data(tmp_path):
    cfg = load_test_config()
    bars = breakout_15m_anchor_nested_5m_bars()
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.4, 111.0, 107.0, 110.8))

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
    flat_bars = [flat_bar(DAY + timedelta(minutes=15 * k), 100.0, spread=0.4) for k in range(30)]

    results = run_backtest(cfg, flat_bars)
    out_path = tmp_path / "chart.html"
    export_chart_html(cfg, results, flat_bars, str(out_path))

    html = out_path.read_text()
    assert "__TRADE_DATA__" not in html
    assert "const TRADES = [];" in html
