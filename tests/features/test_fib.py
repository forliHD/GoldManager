"""Tests for the FibRetracementEngine (last H1 impulse leg)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.fib import FibRetracementEngine

_BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def _m1_from_h1(specs: list[tuple[float, float, float]]) -> list[Bar]:
    """Expand (high, low, close) H1 specs into 60 identical M1 bars per hour.

    round_bars_by_time aggregates each 60-bar bucket back to those H1 OHLC.
    """
    bars: list[Bar] = []
    t = _BASE
    for (h, low, c) in specs:
        for _ in range(60):
            bars.append(
                Bar(
                    symbol="XAUUSD",
                    timeframe="M1",
                    time=t,
                    open=Decimal(str(c)),
                    high=Decimal(str(h)),
                    low=Decimal(str(low)),
                    close=Decimal(str(c)),
                    tick_volume=100,
                )
            )
            t += timedelta(minutes=1)
    return bars


# An up-impulse: swing low at H1 idx2 (90) → swing high at idx5 (120); leg size 30.
# The final (idx8) bar carries the "current price" via its close.
def _up_leg(last_high: float, last_low: float, last_close: float) -> list[Bar]:
    return _m1_from_h1(
        [
            (101, 100, 100),
            (99, 98, 98),
            (92, 90, 91),    # swing low = 90
            (96, 94, 95),
            (100, 98, 99),
            (120, 110, 118),  # swing high = 120
            (115, 108, 110),
            (112, 106, 108),
            (last_high, last_low, last_close),  # current bar (not a swing)
        ]
    )


def _now(bars: list[Bar]) -> datetime:
    return bars[-1].time


def test_insufficient_history_returns_defaults():
    bars = _m1_from_h1([(101, 100, 100)] * 3)  # only 3 H1 bars
    out = FibRetracementEngine().compute(bars, _now(bars))
    assert out.direction == "none"
    assert out.leg_low is None and out.price_zone == "none"


def test_up_impulse_golden_pocket():
    # close 103 → r = (120-103)/30 = 0.567 → golden pocket
    bars = _up_leg(104, 102, 103)
    out = FibRetracementEngine().compute(bars, _now(bars))
    assert out.direction == "up"
    assert out.leg_low == 90 and out.leg_high == 120
    # levels measured down from the high
    assert round(out.fib_500, 1) == 105.0
    assert round(out.fib_618, 1) == 101.5
    assert out.price_zone == "golden_pocket"
    assert out.in_golden_pocket is True
    # leg size 30 >> typical H1 bar range here → a strong impulse
    assert out.trend_strength == "strong"


def test_up_impulse_shallow_retrace():
    bars = _up_leg(116, 114, 115)  # r = (120-115)/30 = 0.167 → shallow
    out = FibRetracementEngine().compute(bars, _now(bars))
    assert out.price_zone == "shallow"
    assert out.in_golden_pocket is False


def test_up_impulse_deep_retrace():
    bars = _up_leg(96, 94, 95)  # r = (120-95)/30 = 0.833 → deep
    out = FibRetracementEngine().compute(bars, _now(bars))
    assert out.price_zone == "deep"
    assert out.in_golden_pocket is False


def test_levels_ordered_for_up_leg():
    bars = _up_leg(104, 102, 103)
    out = FibRetracementEngine().compute(bars, _now(bars))
    # For an up leg, shallower fib (0.236) sits HIGHER than deeper (0.618).
    assert out.fib_236 > out.fib_382 > out.fib_500 > out.fib_618


def test_point_in_time_excludes_future():
    bars = _up_leg(104, 102, 103)
    # Cut off before the impulse high forms (within the first 3 H1 buckets).
    cutoff = _BASE + timedelta(minutes=150)
    out = FibRetracementEngine().compute(bars, cutoff)
    # Not enough confirmed swings yet → defaults (no leg).
    assert out.direction == "none"
