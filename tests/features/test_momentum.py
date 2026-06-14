"""Tests for the CandleMomentumEngine (no pattern names)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.momentum import CandleMomentumEngine


def _bar(time: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=time,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


def _bullish_run(n: int, body: float = 1.0) -> list[Bar]:
    """n consecutive bullish bars (close > open)."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    price = 2000.0
    for i in range(n):
        t = base + timedelta(minutes=i)
        o = price
        c = price + body
        bars.append(_bar(t, o, c + 0.5, o - 0.5, c))
        price = c
    return bars


# ------------------------------------------------------------------- per-bar


def test_body_size_atr_positive_for_strong_bar() -> None:
    """A bar with a 2x-ATR body has body_size_atr ≈ 2.0."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    # 20 small bars to establish ATR, then one big bar.
    bars = _bullish_run(20, body=0.5)
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2010, 2020, 2010 - 0.5, 2020, tv=1000))
    out = eng.compute(bars, bars[-1].time)
    per = out.by_tf["M1"]
    assert per.body_size_atr > 1.0  # at least 1 ATR body


def test_displacement_flag_on_2x_atr_body() -> None:
    """body > 2*ATR → displacement = True."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2010, 2020, 2010 - 0.5, 2020, tv=1000))
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].displacement is True


def test_displacement_flag_on_1_5x_median_body() -> None:
    """body > 1.5× median body → displacement = True."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    # All bars have body=0.5; the next bar has body=1.0 (2x median).
    bars = _bullish_run(20, body=0.5)
    # New bar: o=2010, h=2011.3, l=2009.3, c=2011.0. body=1.0, range=2.0.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2010, 2011.3, 2009.3, 2011, tv=100))
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].displacement is True


def test_no_displacement_for_normal_body() -> None:
    """A bar with body ≈ median → no displacement."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)  # all same body
    out = eng.compute(bars, bars[-1].time)
    # The last bar is a normal body (matches the median).
    assert out.by_tf["M1"].displacement is False


def test_impulsive_follow_through_counts_consecutive_bars() -> None:
    """5 consecutive bullish bars → follow_through = 5."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(5, body=0.5)
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].impulsive_follow_through == 5


def test_follow_through_resets_on_direction_change() -> None:
    """A bearish bar after bullish bars resets the count to 1 (the bearish bar)."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(3, body=0.5)
    # A bearish bar.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2001, 2001.5, 2000, 2000.5, tv=100))
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].impulsive_follow_through == 1


def test_wick_body_ratio_high_for_pin_bar() -> None:
    """A pin bar (small body, big wicks) has a high wick_body_ratio."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    # 20 normal bars.
    bars = _bullish_run(20, body=0.5)
    # A pin bar: open=close=2000, high=2010, low=1990 → body=0, range=20.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2000, 2010, 1990, 2000.05, tv=100))
    out = eng.compute(bars, bars[-1].time)
    per = out.by_tf["M1"]
    assert per.wick_body_ratio > 5.0


def test_close_position_1_for_close_at_high() -> None:
    """A bar where close == high → close_position = 1.0."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    # close=high bar: o=2000, h=2010, l=1999, c=2010.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2000, 2010, 1999, 2010, tv=100))
    out = eng.compute(bars, bars[-1].time)
    per = out.by_tf["M1"]
    assert per.close_position == 1.0


# ------------------------------------------------------------------- aggregate


def test_aggregate_score_0_to_100() -> None:
    """The aggregate score is clamped to [0, 100]."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    out = eng.compute(bars, bars[-1].time)
    assert 0.0 <= out.score <= 100.0


def test_no_bars_returns_zero_score() -> None:
    eng = CandleMomentumEngine()
    out = eng.compute([], datetime(2026, 1, 5, 0, 0, tzinfo=UTC))
    assert out.score == 0.0
    assert out.by_tf == {}


# ------------------------------------------------------------------- PIT


def test_pit_excludes_bars_after_current_t() -> None:
    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    cutoff = bars[10].time
    out_pre = eng.compute(bars, cutoff)
    fut = _bar(bars[10].time + timedelta(minutes=1), 9999, 9999.5, 9998.5, 9999, tv=999999)
    out_with_fut = eng.compute(bars + [fut], cutoff)
    # Last visible bar is at index 10; features should match.
    assert out_pre.by_tf["M1"].body_size_atr == out_with_fut.by_tf["M1"].body_size_atr
