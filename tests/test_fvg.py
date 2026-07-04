from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.config import FvgConfig
from src.fvg import FvgDetector
from src.models import Bar, Direction

TZ = ZoneInfo("America/New_York")
START = datetime(2026, 7, 6, 9, 30, tzinfo=TZ)


def bucket_bar(k: int, o: float, h: float, l: float, c: float) -> Bar:
    """A single bar standing in for one whole 15-minute candle (bucket k).
    The detector only needs *a* bar inside each bucket to compute that
    bucket's OHLC -- it doesn't require 15 individual 1-minute bars."""
    return Bar(timestamp=START + timedelta(minutes=15 * k), open=o, high=h, low=l, close=c)


def make_detector(min_gap_points=1.5, displacement_multiplier=1.5, lookback_bars=5) -> FvgDetector:
    cfg = FvgConfig(
        min_gap_points=min_gap_points,
        displacement_multiplier=displacement_multiplier,
        lookback_bars=lookback_bars,
        timeframe_minutes=15,
    )
    return FvgDetector(cfg, TZ)


def feed_baseline(detector: FvgDetector, count: int = 5) -> None:
    """Quiet 15m candles (range 1.5) that establish the average-range
    baseline. A candle is only finalized once the *next* bucket's bar
    arrives, so results here are always None regardless."""
    for k in range(count):
        result = detector.add_bar(bucket_bar(k, 100.0, 101.0, 99.5, 100.5))
        assert result is None


def test_detects_strong_bullish_fvg():
    detector = make_detector()
    feed_baseline(detector)

    # c0: quiet candle (bucket 5) -- finalizes the last baseline bucket.
    assert detector.add_bar(bucket_bar(5, 101.5, 101.8, 101.0, 101.6)) is None
    # c1: strong bullish displacement candle (bucket 6, body 2.9 vs ~1.5 avg range) -- finalizes c0.
    assert detector.add_bar(bucket_bar(6, 101.6, 104.6, 101.5, 104.5)) is None
    # c2: confirms the gap (bucket 7, c0.high=101.8 < c2.low=104.0) -- finalizes c1.
    assert detector.add_bar(bucket_bar(7, 104.5, 104.8, 104.0, 104.6)) is None

    # The next bar (bucket 8) finalizes c2 and runs the pattern check on c0/c1/c2.
    result = detector.add_bar(bucket_bar(8, 100.0, 100.2, 99.8, 100.0))

    assert result is not None
    assert result.direction is Direction.LONG
    assert result.gap_low == 101.8
    assert result.gap_high == 104.0
    assert result.size > 0
    assert detector.unmitigated_in_direction(Direction.LONG) == [result]


def test_ignores_weak_displacement():
    detector = make_detector()
    feed_baseline(detector)

    detector.add_bar(bucket_bar(5, 101.5, 101.8, 101.0, 101.6))  # c0
    # Displacement candle body is only 0.5 -- well below the 1.5x avg-range bar.
    detector.add_bar(bucket_bar(6, 101.6, 102.2, 101.5, 102.1))  # c1
    detector.add_bar(bucket_bar(7, 102.1, 104.8, 104.0, 104.6))  # c2
    result = detector.add_bar(bucket_bar(8, 100.0, 100.2, 99.8, 100.0))  # flush

    assert result is None


def test_ignores_small_gap():
    detector = make_detector()
    feed_baseline(detector)

    detector.add_bar(bucket_bar(5, 101.5, 101.8, 101.0, 101.6))  # c0
    detector.add_bar(bucket_bar(6, 101.6, 103.0, 101.5, 102.9))  # c1
    # Gap between c0.high (101.8) and c2.low (101.9) is only 0.1pt -- below min_gap_points.
    detector.add_bar(bucket_bar(7, 102.9, 103.2, 101.9, 103.0))  # c2
    result = detector.add_bar(bucket_bar(8, 100.0, 100.2, 99.8, 100.0))  # flush

    assert result is None


def test_active_pool_drops_gap_once_far_edge_is_broken():
    """Once price trades clean through the gap's far (low, for a bullish
    gap) edge, it's mitigated and must drop out of the pool immediately --
    checked against every incoming bar, not just at candle closes."""
    detector = make_detector()
    feed_baseline(detector)
    detector.add_bar(bucket_bar(5, 101.5, 101.8, 101.0, 101.6))
    detector.add_bar(bucket_bar(6, 101.6, 104.6, 101.5, 104.5))
    detector.add_bar(bucket_bar(7, 104.5, 104.8, 104.0, 104.6))
    gap = detector.add_bar(bucket_bar(8, 100.0, 100.2, 99.8, 100.0))
    assert gap is not None
    assert detector.unmitigated_in_direction(Direction.LONG) == [gap]

    # Price tears straight through the whole gap (101.8-104.0) and beyond.
    detector.add_bar(bucket_bar(9, 102.0, 102.5, 101.0, 101.2))

    assert detector.unmitigated_in_direction(Direction.LONG) == []
