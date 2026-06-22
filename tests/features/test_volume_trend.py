"""Tests for the VolumeTrendEngine (tick-volume trend + spike on M1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.volume_trend import VolumeTrendEngine

_BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def _bars(vols: list[int]) -> list[Bar]:
    out: list[Bar] = []
    for i, tv in enumerate(vols):
        out.append(
            Bar(
                symbol="XAUUSD",
                timeframe="M1",
                time=_BASE + timedelta(minutes=i),
                open=Decimal("2000"),
                high=Decimal("2001"),
                low=Decimal("1999"),
                close=Decimal("2000"),
                tick_volume=tv,
            )
        )
    return out


def _now(bars: list[Bar]) -> datetime:
    return bars[-1].time


def test_insufficient_history_returns_defaults():
    out = VolumeTrendEngine().compute(_bars([100] * 10), _now(_bars([100] * 10)))
    assert out.ma_fast is None and out.ma_slow is None
    assert out.is_spike is False and out.trend == "flat"


def test_flat_volume_is_flat():
    bars = _bars([400] * 40)
    out = VolumeTrendEngine().compute(bars, _now(bars))
    assert out.trend == "flat"
    assert out.ma_fast == 400 and out.ma_slow == 400
    assert out.is_spike is False


def test_falling_volume_is_weakening():
    # Volume ramps DOWN over the window → fast MA below its earlier value.
    bars = _bars(list(range(800, 300, -10)))  # 50 bars, strictly declining
    out = VolumeTrendEngine().compute(bars, _now(bars))
    assert out.trend == "falling"
    assert out.slope_pct is not None and out.slope_pct < 0


def test_rising_volume_is_rising():
    bars = _bars(list(range(300, 800, 10)))
    out = VolumeTrendEngine().compute(bars, _now(bars))
    assert out.trend == "rising"
    assert out.slope_pct is not None and out.slope_pct > 0


def test_spike_detected_on_large_last_bar():
    # 30 flat bars at 400, then a 1000-volume spike (1000/400 = 2.5 > 2.0).
    bars = _bars([400] * 30 + [1000])
    out = VolumeTrendEngine().compute(bars, _now(bars))
    assert out.is_spike is True
    assert out.spike_ratio is not None and out.spike_ratio > 2.0
    assert out.last_volume == 1000


def test_no_spike_on_normal_bar():
    bars = _bars([400] * 30 + [500])  # 500/400 = 1.25 < 2.0
    out = VolumeTrendEngine().compute(bars, _now(bars))
    assert out.is_spike is False


def test_point_in_time_ignores_future_bars():
    bars = _bars([400] * 30 + [9999] * 5)  # future spikes
    cutoff = bars[29].time  # only the first 30 flat bars are visible
    out = VolumeTrendEngine().compute(bars, cutoff)
    assert out.is_spike is False
    assert out.last_volume == 400


def test_spike_mult_configurable():
    bars = _bars([400] * 30 + [700])  # 1.75×
    assert VolumeTrendEngine(spike_mult=1.5).compute(bars, _now(bars)).is_spike is True
    assert VolumeTrendEngine(spike_mult=2.0).compute(bars, _now(bars)).is_spike is False
