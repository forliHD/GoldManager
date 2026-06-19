"""Tests for the SpreadMonitor — rolling percentiles, spike/outlier detection."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.connectors.schemas import Bar, Tick
from xauusd_bot.data.spread_monitor import SpreadMonitor, SpreadSnapshot


# ---------------------------------------------------------------- percentiles


def test_percentiles_on_synthetic_uniform_series() -> None:
    """Rolling percentiles P50 / P95 on a known uniform series.

    10 evenly-spaced samples [10, 20, ..., 100]. With linear-interpolated
    percentiles (the documented semantics):
        P50 rank = 0.5 * 9 = 4.5 → 50 + 0.5*(60-50) = 55.0
        P95 rank = 0.95 * 9 = 8.55 → 90 + 0.55*(100-90) = 95.5
    """

    m = SpreadMonitor(
        symbol="XAUUSD",
        point=Decimal("0.01"),
        window=100,
        warn_points=200,  # absolute: high enough that nothing is outlier
        block_points=400,
    )
    for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        m.update_from_points(v)
    s = m.snapshot()
    assert s.p50 == pytest.approx(55.0)
    assert s.p95 == pytest.approx(95.5, abs=1e-9)
    assert s.n == 10


def test_percentiles_on_realistic_skewed_series() -> None:
    """A log-normal-looking spread series must produce sane percentiles."""

    rng = random.Random(42)
    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"), window=2000)
    samples = []
    for _ in range(1000):
        # Base spread 30 points, with a 10-point jitter.
        v = 30.0 + rng.gauss(0, 5)
        samples.append(v)
        m.update_from_points(v)
    s = m.snapshot()
    # Median should be close to 30.
    assert 25.0 < s.p50 < 35.0, f"p50 {s.p50} out of expected range"
    # p95 should be noticeably higher than p50.
    assert s.p95 > s.p50
    assert s.n == 1000


def test_snapshot_returns_zero_state_when_empty() -> None:
    """An empty monitor must not crash on snapshot() — all percentiles 0."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    s = m.snapshot()
    assert s.p50 == 0.0
    assert s.p95 == 0.0
    assert s.p99 == 0.0
    assert s.current == 0.0
    assert s.n == 0
    assert s.is_outlier is False
    assert s.is_block is False


def test_snapshot_with_single_sample() -> None:
    """A monitor with exactly one sample reports that sample at every percentile."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"), window=10)
    m.update_from_points(42.0)
    s = m.snapshot()
    assert s.p50 == 42.0
    assert s.p95 == 42.0
    assert s.p99 == 42.0
    assert s.current == 42.0
    assert s.n == 1


# ---------------------------------------------------------------- spike detection


def test_spike_detection_one_outlier_in_thousand() -> None:
    """One massive outlier among 1000 normal samples must be flagged."""

    m = SpreadMonitor(
        symbol="XAUUSD",
        point=Decimal("0.01"),
        window=2000,
        warn_points=80.0,  # absolute warn
        block_points=200.0,  # absolute block
    )
    # 999 normal samples around 30 points.
    for _ in range(999):
        m.update_from_points(30.0)
    s_normal = m.snapshot()
    assert s_normal.is_outlier is False
    assert s_normal.is_block is False

    # One spike: 500 points — 16x the median.
    m.update_from_points(500.0)
    s_spike = m.snapshot()
    assert s_spike.is_outlier is True
    assert s_spike.is_block is True
    assert s_spike.current == 500.0


def test_outlier_via_percentile_threshold() -> None:
    """Outlier flag is set when the current value is at or above the
    warn-percentile threshold (no absolute warn_points configured)."""

    m = SpreadMonitor(
        symbol="XAUUSD",
        point=Decimal("0.01"),
        window=100,
        warn_percentile=0.95,  # top 5% is outlier
        block_percentile=0.99,  # top 1% is block
    )
    # 100 samples in [10, 100]; p95 ≈ 95, p99 ≈ 99.
    for v in range(1, 101):
        m.update_from_points(float(v))
    # A value above p95 (e.g. 97) is an outlier but not a block.
    m.update_from_points(97.0)
    s = m.snapshot()
    assert s.is_outlier is True
    assert s.is_block is False


def test_block_via_percentile_threshold() -> None:
    """Block flag is set when the current value is at or above the
    block-percentile threshold."""

    m = SpreadMonitor(
        symbol="XAUUSD",
        point=Decimal("0.01"),
        window=100,
        warn_percentile=0.95,
        block_percentile=0.99,
    )
    for v in range(1, 101):
        m.update_from_points(float(v))
    # A value above p99 (e.g. 100) is a block.
    m.update_from_points(100.0)
    s = m.snapshot()
    assert s.is_outlier is True
    assert s.is_block is True


# ---------------------------------------------------------------- input methods


def test_update_from_tick_computes_points_correctly() -> None:
    """Spread from a tick (bid, ask) is (ask - bid) / point in points."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    tick = Tick(
        symbol="XAUUSD",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        bid=Decimal("2000.00"),
        ask=Decimal("2000.50"),
    )
    m.update_from_tick(tick)
    assert m.last == 50.0  # 0.50 / 0.01 = 50 points


def test_update_from_bar_uses_bar_spread_field() -> None:
    """update_from_bar converts the bar's spread (price units) to points."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    bar = Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000.5"),
        tick_volume=10,
        spread=Decimal("0.30"),  # 30 points at 0.01
    )
    m.update_from_bar(bar)
    assert m.last == 30.0


def test_update_from_bar_with_no_spread_is_no_op() -> None:
    """A bar with spread=None must not affect the monitor."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    bar = Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000.5"),
        tick_volume=10,
        spread=None,
    )
    m.update_from_bar(bar)
    assert m.last == 0.0
    assert m.snapshot().n == 0


# ---------------------------------------------------------------- windowing


def test_window_drops_old_samples() -> None:
    """A rolling window drops the oldest sample when it overflows."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"), window=3)
    m.update_from_points(10.0)
    m.update_from_points(20.0)
    m.update_from_points(30.0)
    s = m.snapshot()
    assert s.n == 3
    assert s.p50 == 20.0  # median of [10, 20, 30]

    # Overflow: drop 10, add 40 → window = [20, 30, 40]
    m.update_from_points(40.0)
    s = m.snapshot()
    assert s.n == 3
    assert s.p50 == 30.0
    assert s.current == 40.0


def test_reset_clears_state() -> None:
    """reset() drops the window and the last value."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    m.update_from_points(10.0)
    m.update_from_points(20.0)
    assert m.snapshot().n == 2
    m.reset()
    assert m.snapshot().n == 0
    assert m.last == 0.0


# ---------------------------------------------------------------- update_from_points


def test_update_from_points_clamps_negative_to_zero() -> None:
    """Negative spreads are not physically possible — clamp to 0."""

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    m.update_from_points(-5.0)
    assert m.last == 0.0
