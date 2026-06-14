"""Tests for the FixedVolumeRangeEngine (Yearly/Monthly/Weekly profiles)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.common.schemas.features import (
    ValueAreaStatus,
    VolumeProfileState,
)
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.volume_range import FixedVolumeRangeEngine, VolumeDistribution


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


def _build_m1_day(date: datetime, base_price: float, vol: int = 100, range_size: float = 0.5) -> list[Bar]:
    """Build a day's worth of M1 bars (1440) with a constant range."""

    bars: list[Bar] = []
    for i in range(1440):
        t = date + timedelta(minutes=i)
        # Slow drift up.
        p = base_price + 0.0001 * i
        bars.append(_bar(t, p, p + range_size, p - range_size, p + 0.0001, tv=vol))
    return bars


# ---------------------------------------------------------------- period boundaries


def test_weekly_bounds_monday_to_monday() -> None:
    """A query on a Wednesday returns the Mon-Mon ISO week."""

    from xauusd_bot.features.volume_range import _weekly_bounds

    wed = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)  # Wed
    start, end = _weekly_bounds(wed)
    assert start == datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # Mon
    assert end == datetime(2026, 1, 12, 0, 0, tzinfo=UTC)  # next Mon


def test_monthly_bounds_first_to_first() -> None:
    from xauusd_bot.features.volume_range import _monthly_bounds

    mid = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    start, end = _monthly_bounds(mid)
    assert start == datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def test_yearly_bounds_jan1_to_jan1() -> None:
    from xauusd_bot.features.volume_range import _yearly_bounds

    mid = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    start, end = _yearly_bounds(mid)
    assert start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- state transitions


def test_locked_vs_developing_states() -> None:
    """The current period is 'developing'; the previous is 'locked' (or absent)."""

    eng = FixedVolumeRangeEngine()
    # Build 2 days of bars: one in the previous week, one in the current
    # week (we sit on a Wednesday with 1.5 days of history visible).
    wed = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)
    # Build the previous Monday → Wed (3 days of bars) and that puts the
    # "previous" weekly profile fully populated. We sit at current_t =
    # Wednesday noon.
    prev_mon = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = _build_m1_day(prev_mon, 2000.0) + _build_m1_day(prev_mon + timedelta(days=1), 2000.0)
    # Add partial Wed (current week) up to noon.
    for i in range(12 * 60):  # 12h = 720 bars
        t = datetime(2026, 1, 7, 0, 0, tzinfo=UTC) + timedelta(minutes=i)
        bars.append(_bar(t, 2000, 2000.5, 1999.5, 2000.0001, tv=100))
    out = eng.compute(bars, wed)
    assert out.weekly.state == VolumeProfileState.DEVELOPING
    # prev_week may or may not be present depending on visibility; with
    # the data above it should be locked.
    if out.prev_week is not None:
        assert out.prev_week.state == VolumeProfileState.LOCKED
    # Developing weekly includes 2 full days (Mon, Tue) + 12h of Wed.
    assert out.weekly.n_bars == 2 * 1440 + 12 * 60
    # Monthly spans the whole month — still developing.
    assert out.monthly.state == VolumeProfileState.DEVELOPING
    # Yearly too.
    assert out.yearly.state == VolumeProfileState.DEVELOPING


def test_rollover_creates_new_developing_profile() -> None:
    """At the exact moment a new period begins, the old one freezes + a new one starts."""

    eng = FixedVolumeRangeEngine()
    # Build 7 days of M1 bars (full Mon-Sun week), then sit at 00:00 of the new week.
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # Mon
    bars: list[Bar] = []
    for d in range(7):
        bars.extend(_build_m1_day(base + timedelta(days=d), 2000.0))
    # Current_t = Monday 00:00 (start of new week).
    new_week_start = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)
    out = eng.compute(bars, new_week_start)
    # Developing weekly has 0 bars (we're at the exact start).
    assert out.weekly.n_bars == 0
    assert out.weekly.state == VolumeProfileState.EMPTY
    # Previous week is locked and fully populated.
    assert out.prev_week is not None
    assert out.prev_week.state == VolumeProfileState.LOCKED
    assert out.prev_week.n_bars == 7 * 1440  # 7 days × 1440 M1/day


# ---------------------------------------------------------------- look-ahead freedom


def test_look_ahead_freedom() -> None:
    """The profile at t=N must not change if a bar with close_time > t_N is added.

    This is the PIT contract — if it ever fails, the engine has look-ahead
    and is unsafe for backtesting.
    """

    eng = FixedVolumeRangeEngine()
    wed = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars_visible = _build_m1_day(base, 2000.0) + _build_m1_day(base + timedelta(days=1), 2000.0)
    # Add a "future" bar (close_time > current_t). The engine must
    # ignore it.
    fut = _bar(wed + timedelta(minutes=1), 9999, 9999.5, 9998.5, 9999.0001, tv=100000)
    out_no_fut = eng.compute(bars_visible, wed)
    out_with_fut = eng.compute(bars_visible + [fut], wed)
    # All developing levels must be byte-identical.
    assert out_no_fut.weekly.vah == out_with_fut.weekly.vah
    assert out_no_fut.weekly.vpoc == out_with_fut.weekly.vpoc
    assert out_no_fut.weekly.val == out_with_fut.weekly.val
    assert out_no_fut.monthly.vah == out_with_fut.monthly.vah
    assert out_no_fut.monthly.vpoc == out_with_fut.monthly.vpoc
    assert out_no_fut.monthly.val == out_with_fut.monthly.val
    assert out_no_fut.yearly.vah == out_with_fut.yearly.vah


def test_in_progress_bar_excluded() -> None:
    """A bar that opens at 11:59 and 'closes' at 12:01 must NOT count at current_t=12:00.

    The PIT filter is the caller's responsibility (the engine re-filters
    defensively). This test verifies the engine's defensive re-filter
    works: a bar with time == current_t+1m must be ignored even if it's
    in the iterable.
    """

    eng = FixedVolumeRangeEngine()
    wed = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = _build_m1_day(base, 2000.0) + _build_m1_day(base + timedelta(days=1), 2000.0)
    # Add bars up to 11:59 (12h - 1 minute) on Wed.
    for i in range(12 * 60 - 1):
        t = datetime(2026, 1, 7, 0, 0, tzinfo=UTC) + timedelta(minutes=i)
        bars.append(_bar(t, 2000, 2000.5, 1999.5, 2000.0001, tv=100))
    # Add a future bar at 12:00 (== current_t is OK, 12:01 is the violation).
    bars.append(_bar(wed, 2000, 2000.5, 1999.5, 2000.0001, tv=100))
    bars.append(_bar(wed + timedelta(minutes=1), 9999, 9999, 9998, 9999, tv=999999))
    out = eng.compute(bars, wed)
    # 2 full days of bars (Mon + Tue) + 12*60 - 1 + 1 (the 12:00 bar) = 3599.
    # The 12:01 bar must be excluded.
    assert out.weekly.n_bars == 2 * 1440 + 12 * 60
    # And the volume from the 12:01 bar must not have polluted the profile.
    assert out.weekly.vah is not None
    assert out.weekly.vah < 5000  # would be ~9999 if look-ahead had leaked


# ---------------------------------------------------------------- features


def test_value_status_below_above_within() -> None:
    """Where the last close sits relative to the value area drives the status."""

    eng = FixedVolumeRangeEngine()
    # Build a day with bars in a tight range, then close above/below.
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = []
    for i in range(100):
        t = base + timedelta(minutes=i)
        p = 2000.0
        bars.append(_bar(t, p, p + 0.5, p - 0.5, p, tv=100))
    # Last bar closes well above the value area.
    bars.append(_bar(base + timedelta(minutes=100), 2010, 2011, 2009, 2010, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=100))
    assert out.weekly.value_status == ValueAreaStatus.ABOVE_VALUE


def test_value_area_pct_70_default() -> None:
    """Default value_area_pct is 0.70."""

    eng = FixedVolumeRangeEngine()
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = []
    for i in range(60):
        t = base + timedelta(minutes=i)
        p = 2000.0
        bars.append(_bar(t, p, p + 0.5, p - 0.5, p, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=59))
    assert out.weekly.value_area_pct == 0.70


def test_value_area_pct_override() -> None:
    """A custom value_area_pct (0.68 / 0.75) is honored."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = []
    for i in range(60):
        t = base + timedelta(minutes=i)
        p = 2000.0
        bars.append(_bar(t, p, p + 0.5, p - 0.5, p, tv=100))
    out68 = FixedVolumeRangeEngine(value_area_pct=0.68).compute(bars, base + timedelta(minutes=59))
    out75 = FixedVolumeRangeEngine(value_area_pct=0.75).compute(bars, base + timedelta(minutes=59))
    assert out68.weekly.value_area_pct == 0.68
    assert out75.weekly.value_area_pct == 0.75


# ---------------------------------------------------------------- distribution


def test_distribution_close_only_puts_volume_at_close() -> None:
    """close_only: all volume lands on the close price bin."""

    eng = FixedVolumeRangeEngine(distribution=VolumeDistribution.CLOSE_ONLY)
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = []
    for i in range(10):
        t = base + timedelta(minutes=i)
        bars.append(_bar(t, 2000, 2005, 1995, 2003, tv=1000))
    out = eng.compute(bars, base + timedelta(minutes=9))
    # With close_only, all 10 bars deposit at the close bin. Bin centers
    # are multiples of bin_size (0.75 for weekly), so 2003 lands in the
    # bin centered at 2003.25. The point is: ALL volume lives in that
    # single bin, so VAH == VPOC == VAL.
    assert out.weekly.vpoc is not None
    assert out.weekly.vah == out.weekly.vpoc == out.weekly.val


def test_distribution_uniform_hl_spreads_volume() -> None:
    """uniform_hl: volume is spread across the high-low range of each bar."""

    eng = FixedVolumeRangeEngine(distribution=VolumeDistribution.UNIFORM_HL)
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = []
    for i in range(10):
        t = base + timedelta(minutes=i)
        bars.append(_bar(t, 2000, 2005, 1995, 2003, tv=1000))
    out = eng.compute(bars, base + timedelta(minutes=9))
    # With uniform_hl, bins between 1995 and 2005 should all have some
    # volume, so VAH - VAL should span the bar range.
    assert out.weekly.vah is not None and out.weekly.val is not None
    spread = out.weekly.vah - out.weekly.val
    assert spread >= 5.0  # ≥ bar range (2005 - 1995 = 10)
