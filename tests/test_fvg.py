from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import FvgConfig
from src.fvg import FvgDetector
from src.models import Bar, Direction

TZ = ZoneInfo("America/New_York")
START = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bar(i: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=START + timedelta(minutes=i), open=o, high=h, low=l, close=c)


def make_detector() -> FvgDetector:
    return FvgDetector(FvgConfig(min_gap_points=1.5, displacement_multiplier=1.5, lookback_bars=20))


def test_detects_strong_bullish_fvg():
    detector = make_detector()
    result = None
    # 19 baseline bars with ~1.5pt ranges -- no gaps, establishes avg range.
    for i in range(19):
        result = detector.add_bar(bar(i, 100.0, 101.0, 99.5, 100.5))
        assert result is None

    # c0: quiet candle
    result = detector.add_bar(bar(19, 101.5, 101.8, 101.0, 101.6))
    assert result is None

    # c1: strong bullish displacement candle (body 2.9, avg range ~1.5 -> well above 1.5x threshold)
    result = detector.add_bar(bar(20, 101.6, 104.6, 101.5, 104.5))
    assert result is None

    # c2: confirms the gap (c0.high=101.8 < c2.low=104.0, gap size 2.2)
    result = detector.add_bar(bar(21, 104.5, 104.8, 104.0, 104.6))

    assert result is not None
    assert result.direction is Direction.LONG
    assert result.gap_low == 101.8
    assert result.gap_high == 104.0
    assert result.size > 0


def test_ignores_weak_displacement():
    detector = make_detector()
    for i in range(19):
        detector.add_bar(bar(i, 100.0, 101.0, 99.5, 100.5))

    detector.add_bar(bar(19, 101.5, 101.8, 101.0, 101.6))
    # Displacement candle body is only 0.5 -- well below the 1.5x avg-range bar.
    detector.add_bar(bar(20, 101.6, 102.2, 101.5, 102.1))
    result = detector.add_bar(bar(21, 102.1, 104.8, 104.0, 104.6))

    assert result is None


def test_ignores_small_gap():
    detector = make_detector()
    for i in range(19):
        detector.add_bar(bar(i, 100.0, 101.0, 99.5, 100.5))

    detector.add_bar(bar(19, 101.5, 101.8, 101.0, 101.6))
    detector.add_bar(bar(20, 101.6, 103.0, 101.5, 102.9))
    # Gap between c0.high (101.8) and c2.low (101.9) is only 0.1pt -- below min_gap_points.
    result = detector.add_bar(bar(21, 102.9, 103.2, 101.9, 103.0))

    assert result is None
