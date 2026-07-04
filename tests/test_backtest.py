import json
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.backtest import _pnl_points, export_chart_html, export_chart_json, print_near_miss_anchors, run_backtest
from src.config import load_config
from src.models import Bar

TZ = ZoneInfo("America/New_York")
PREV_DAY_BASE = datetime(2026, 7, 5, 6, 0, tzinfo=TZ)
DAY = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)

WICK = 0.2  # wide enough that a smooth 1-minute walk never forms its own 1-minute-scale gap


def bar_at(dt: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=dt, open=o, high=h, low=l, close=c)


def flat_bar(dt: datetime, price: float, spread: float = 0.5) -> Bar:
    """A single bar standing in for a whole quiet, unchanging candle --
    safe to use sparsely since a repeated, unchanging value can't form a
    gap, so it doesn't also register as a false FVG."""
    return bar_at(dt, price, price + spread / 2, price - spread / 2, price)


def smooth_walk_1m(start: datetime, minutes: int, start_price: float, end_price: float) -> list[Bar]:
    """`minutes` consecutive real 1-minute bars walking smoothly from
    start_price to end_price -- gentle enough relative to WICK that no 3
    consecutive bars form their own 1-minute gap, so this same price
    action can build a genuine 5m candle without also registering as a
    contaminating 1-minute-scale FVG."""
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
    # 13:30 ET cutoff -- push it out so the cutoff isn't what's under
    # test here.
    cfg.session.no_new_entries_after = dtime(23, 59)
    # See tests/test_strategy.py: these fixed box/previous-day levels
    # routinely land inside the real config's 20-point minimum stop
    # distance -- zero it out since that gate isn't what's under test here.
    cfg.strategy.min_stop_dollars = 0.0
    # See tests/test_strategy.py: this file's fixtures assume the exact
    # midpoint too, since the real config's default is now loosened.
    cfg.strategy.entry_retracement_pct = 0.5
    return cfg


def breakout_5m_anchor_bars() -> list[Bar]:
    """Previous-day high of 105 and low of 95, box 9:30-9:45 (high=101/
    low=99.5), breakout above the box, 8 quiet 5m baseline candles, then
    a real displacement move from 105.1 to 109.3 that forms a large 5m
    FVG anchor (gap 105.3-109.1) -- its own midpoint (107.2) is the entry
    trigger, filled by the final bar's retrace back down to it."""
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
        bars.append(flat_bar(baseline_start + timedelta(minutes=5 * i), 105.0))  # 5m baseline

    pattern_start = baseline_start + timedelta(minutes=5 * 8)
    bars += smooth_walk_1m(pattern_start, 5, 105.0, 105.1)  # c0
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=5), 5, 105.1, 109.3)  # c1: displacement
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=10), 5, 109.3, 109.5)  # c2: confirms gap
    bars.append(flat_bar(pattern_start + timedelta(minutes=15), 109.5))  # flush -- detects the 5m anchor
    bars.append(bar_at(pattern_start + timedelta(minutes=16), 109.5, 109.6, 105.3, 107.2))  # fills the 107.2 midpoint
    return bars


def test_backtest_records_a_win():
    cfg = load_test_config()
    bars = breakout_5m_anchor_bars()
    # Stop candidates are only the marked previous-day/box levels now (the
    # anchor's own edges are deliberately excluded -- see strategy.py: at
    # entry=midpoint, they're always exactly half the anchor's own width
    # away, not real structure). Nearest below entry (107.2) is the
    # previous-day high (105.0), so target is 107.2 + 2*(107.2-105.0) = 111.6.
    # Runs up to the target (111.6) without dipping to the stop (105.0) first.
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.5, 111.9, 107.1, 111.7))

    results = run_backtest(cfg, bars)

    assert len(results) == 1
    assert results[0]["won"] is True
    assert results[0]["date"] == DAY.date()
    assert results[0]["entry_price"] == pytest.approx(107.2)
    assert results[0]["stop_price"] == pytest.approx(105.0)
    assert results[0]["target_price"] == pytest.approx(111.6)


def test_backtest_records_a_loss():
    cfg = load_test_config()
    bars = breakout_5m_anchor_bars()
    # Drops to the stop (105.0, the previous-day high) without reaching
    # the target (111.6) first.
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.0, 107.2, 104.5, 105.0))

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
    bars = breakout_5m_anchor_bars()
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.5, 111.9, 107.1, 111.7))

    stats: dict = {}
    run_backtest(cfg, bars, stats_out=stats)

    assert stats == {
        "breakouts": 1,
        "breakouts_invalidated": 0,
        "large_fvgs": 1,
        "fills": 1,
    }


def breakout_5m_anchor_no_fill_bars() -> list[Bar]:
    """Same breakout + 5m anchor as breakout_5m_anchor_bars(), but price
    stays well above the anchor's midpoint (107.2) for the rest of the
    session instead of ever retracing down to it -- the anchor forms and
    then just sits there, unfilled, until whatever cutoff the caller's
    config sets kicks in."""
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
        bars.append(flat_bar(baseline_start + timedelta(minutes=5 * i), 105.0))

    pattern_start = baseline_start + timedelta(minutes=5 * 8)
    bars += smooth_walk_1m(pattern_start, 5, 105.0, 105.1)
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=5), 5, 105.1, 109.3)
    bars += smooth_walk_1m(pattern_start + timedelta(minutes=10), 5, 109.3, 109.5)
    bars.append(flat_bar(pattern_start + timedelta(minutes=15), 109.5))  # flush -- detects the 5m anchor

    flat_after = pattern_start + timedelta(minutes=16)
    for i in range(20):
        bars.append(flat_bar(flat_after + timedelta(minutes=i), 109.5, spread=0.3))
    return bars


def test_funnel_stats_show_anchor_but_no_fill_afterward():
    """A breakout that anchors on a large 5m FVG but where price never
    retraces back to the anchor's own midpoint should show up as a
    near-miss: breakout + anchor counted, zero fills."""
    cfg = load_test_config()
    bars = breakout_5m_anchor_no_fill_bars()

    stats: dict = {}
    results = run_backtest(cfg, bars, stats_out=stats)

    assert results == []
    assert stats["breakouts"] == 1
    assert stats["large_fvgs"] == 1
    assert stats["fills"] == 0


def test_anchor_history_records_a_fill():
    cfg = load_test_config()
    bars = breakout_5m_anchor_bars()

    history: list = []
    run_backtest(cfg, bars, anchor_history_out=history)

    assert len(history) == 1
    assert history[0].outcome == "filled"
    assert history[0].gap_low == pytest.approx(105.3)
    assert history[0].gap_high == pytest.approx(109.1)


def test_anchor_history_records_session_ended_when_cutoff_hits_before_a_fill():
    cfg = load_test_config()
    cfg.session.no_new_entries_after = dtime(11, 20)  # inside the no-fill fixture's flat tail
    bars = breakout_5m_anchor_no_fill_bars()

    history: list = []
    results = run_backtest(cfg, bars, anchor_history_out=history)

    assert results == []
    assert len(history) == 1
    assert history[0].outcome == "session_ended"


def test_print_near_miss_anchors_reports_unfilled_anchors(capsys):
    cfg = load_test_config()
    cfg.session.no_new_entries_after = dtime(11, 20)
    bars = breakout_5m_anchor_no_fill_bars()

    history: list = []
    run_backtest(cfg, bars, anchor_history_out=history)
    print_near_miss_anchors(history)

    out = capsys.readouterr().out
    assert "Near-miss anchors (1 formed but never filled)" in out
    assert "session_ended" in out
    assert "Breakdown: session_ended=1" in out


def test_print_near_miss_anchors_reports_nothing_when_every_anchor_filled(capsys):
    cfg = load_test_config()
    bars = breakout_5m_anchor_bars()

    history: list = []
    run_backtest(cfg, bars, anchor_history_out=history)
    print_near_miss_anchors(history)

    out = capsys.readouterr().out
    assert "No near-miss anchors" in out


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
    bars = breakout_5m_anchor_bars()
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.5, 111.9, 107.1, 111.7))

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
    assert trade["anchor_timeframe_minutes"] == 5
    # entry_time should exactly match one of the candle labels, so the
    # chart can find that candle and place a marker there.
    assert any(c["t"] == trade["entry_time"] for c in trade["candles"])
    # Candles should span from 9:30 through the exit bar.
    assert trade["candles"][0]["t"] == "09:30"
    assert len(trade["candles"]) > 0


def test_export_chart_html_embeds_trade_data(tmp_path):
    cfg = load_test_config()
    bars = breakout_5m_anchor_bars()
    bars.append(bar_at(bars[-1].timestamp + timedelta(minutes=1), 107.5, 111.9, 107.1, 111.7))

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
