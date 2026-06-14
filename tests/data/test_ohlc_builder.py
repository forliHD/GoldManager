"""Tests for the OHLCBuilder — M1→M5/H1/D1 cascade and tick-based aggregation."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from xauusd_bot.connectors.schemas import Bar, Tick
from xauusd_bot.data.ohlc_builder import OHLCBuilder


# ---------------------------------------------------------------- helpers


def _m1_bar(t: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=t,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


def _tick(t: datetime, mid: float, volume: int = 0) -> Tick:
    """A round-number tick: bid = mid, ask = mid + 0.01."""

    return Tick(
        symbol="XAUUSD",
        time=t,
        bid=Decimal(str(mid)),
        ask=Decimal(str(mid + 0.01)),
        last=Decimal(str(mid)),
        volume=volume,
    )


# =============================================================== 1. 100 random ticks → M1 bars


def test_one_hundred_random_ticks_aggregate_into_m1_bars() -> None:
    """100 ticks spread across known minute buckets must produce the right
    M1 OHLC per bucket.

    Aggregation rule: an M1 closes only when the *next* tick (in a
    later minute) arrives. So 100 ticks in minutes 0..4 leave the
    minute-4 M1 open. We add one closing tick in minute 5 to flush
    it, then assert the 5 closed M1 bars have the right OHLC.
    """

    rng = random.Random(0xC0FFEE)
    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    # Distribute 100 ticks across 5 contiguous minutes: 20 each.
    expected_buckets: dict[datetime, list[Decimal]] = {}
    for i in range(100):
        minute = i // 20  # 0..4
        # Random timestamp within the minute.
        seconds = rng.randint(0, 59)
        t = base + timedelta(minutes=minute, seconds=seconds)
        # Random mid price in [2000, 2001), generated in Decimal to avoid
        # float-precision drift when comparing to the aggregator's mid.
        mid = Decimal("2000") + Decimal(str(rng.random()))
        # on_tick is a generator (it may yield closed higher-TF bars).
        # Always exhaust it so the side-effects run.
        for _ in builder.on_tick(_tick(t, float(mid))):
            pass
        # The aggregator computes (bid + ask) / 2 = mid + 0.005.
        expected_buckets.setdefault(base + timedelta(minutes=minute), []).append(mid + Decimal("0.005"))

    # Add a final closing tick in minute 5 to flush the last M1 bucket.
    for _ in builder.on_tick(_tick(base + timedelta(minutes=5), 2050.0)):
        pass

    # Should have 5 M1 closed bars.
    m1 = builder.closed_bars("M1")
    assert len(m1) == 5
    for bar in m1:
        bucket_samples = expected_buckets[bar.time]
        expected_open = bucket_samples[0]
        expected_high = max(bucket_samples)
        expected_low = min(bucket_samples)
        expected_close = bucket_samples[-1]
        # Compare with tolerance — float→Decimal→float round-trip can shift
        # the last digit. Use 1e-9 absolute tolerance on the float side.
        assert abs(float(bar.open) - float(expected_open)) < 1e-9, (
            f"open mismatch for {bar.time}: {bar.open} vs {expected_open}"
        )
        assert abs(float(bar.high) - float(expected_high)) < 1e-9, (
            f"high mismatch for {bar.time}: {bar.high} vs {expected_high}"
        )
        assert abs(float(bar.low) - float(expected_low)) < 1e-9, (
            f"low mismatch for {bar.time}: {bar.low} vs {expected_low}"
        )
        assert abs(float(bar.close) - float(expected_close)) < 1e-9, (
            f"close mismatch for {bar.time}: {bar.close} vs {expected_close}"
        )


# =============================================================== 2. edge: 0 ticks in a minute


def test_zero_ticks_in_a_minute_produces_no_bar() -> None:
    """A minute with no ticks should produce no M1 bar.

    Feed 5 ticks all in minute 0, then 5 ticks all in minute 5, then
    1 more tick in minute 6 (to flush the minute-5 M1). We expect
    exactly 2 M1 closed bars (one per non-empty minute), not 6.
    """

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(5):
        for _ in builder.on_tick(_tick(base + timedelta(seconds=i), 2000.0 + i * 0.1)):
            pass
    # 60-second gap: minute 1..4 have NO ticks.
    for i in range(5):
        for _ in builder.on_tick(_tick(base + timedelta(minutes=5, seconds=i), 2010.0 + i * 0.1)):
            pass
    # Closing tick in minute 6 flushes the minute-5 M1.
    for _ in builder.on_tick(_tick(base + timedelta(minutes=6), 2050.0)):
        pass
    m1 = builder.closed_bars("M1")
    assert len(m1) == 2
    assert m1[0].time == base
    assert m1[1].time == base + timedelta(minutes=5)


# =============================================================== 3. edge: 1 tick in a minute


def test_one_tick_in_a_minute_ohlc_equals_tick() -> None:
    """A minute with a single tick must produce an OHLC = that tick.

    The aggregator's mid is (bid + ask) / 2, so a tick with bid=2000.55
    and ask=2000.56 produces an M1 with open=high=low=close=2000.555.
    """

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    tick = _tick(base + timedelta(seconds=15), mid=2000.55)
    for _ in builder.on_tick(tick):
        pass
    # The single tick is still in the open M1 bar; close it by feeding
    # a tick in a later minute.
    for _ in builder.on_tick(_tick(base + timedelta(minutes=1, seconds=0), mid=2000.00)):
        pass
    m1 = builder.closed_bars("M1")
    assert len(m1) == 1
    bar = m1[0]
    # open/high/low/close all equal the aggregator's mid (2000.55 + 0.005 spread).
    assert bar.open == Decimal("2000.555")
    assert bar.high == Decimal("2000.555")
    assert bar.low == Decimal("2000.555")
    assert bar.close == Decimal("2000.555")
    assert bar.time == base


# =============================================================== 4. M5 cascade from M1


def test_m5_cascade_correct_ohlc() -> None:
    """5 M1 bars in the same M5 bucket → 1 M5 bar with correct OHLC."""

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # 5 bars in 00:00..00:04
    for i in range(5):
        builder.on_bar(_m1_bar(base + timedelta(minutes=i), 2000 + i, 2005 + i, 1998 + i, 2003 + i))
    # 6th bar (00:05) rolls the M5 bucket.
    closed = list(builder.on_bar(_m1_bar(base + timedelta(minutes=5), 2010, 2015, 2005, 2012)))
    m5 = [b for b in closed if b.timeframe == "M5"]
    assert len(m5) == 1
    m5bar = m5[0]
    # open = first bar's open = 2000
    assert m5bar.open == Decimal("2000")
    # high = max of bars 0..4 highs = max(2005..2009) = 2009
    assert m5bar.high == Decimal("2009")
    # low = min of bars 0..4 lows = 1998
    assert m5bar.low == Decimal("1998")
    # close = last bar in the bucket's close = 2007
    assert m5bar.close == Decimal("2007")
    # tick_volume = sum
    assert m5bar.tick_volume == 5 * 100  # default tv=100


def test_m5_cascade_emits_nothing_when_only_one_bar() -> None:
    """A single M1 bar in a fresh M5 bucket should not emit any M5 bar yet."""

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    closed = list(builder.on_bar(_m1_bar(base, 2000, 2001, 1999, 2000.5)))
    m5 = [b for b in closed if b.timeframe == "M5"]
    assert m5 == []
    # But the M1 is in the closed list (we treat the source as "closed" on arrival).
    assert len(builder.closed_bars("M1")) == 1


# =============================================================== 5. M1 → higher TFs


def test_h1_bucket_rolls_after_60_minutes() -> None:
    """61 M1 bars starting at 00:00: 60 in the 00:00 H1 bucket, 1 rolls it."""

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(60):
        builder.on_bar(_m1_bar(base + timedelta(minutes=i), 2000, 2001, 1999, 2000.5, tv=10))
    # 61st bar (01:00) rolls the H1 bucket.
    closed = list(builder.on_bar(_m1_bar(base + timedelta(hours=1), 2010, 2011, 2009, 2010.5)))
    h1 = [b for b in closed if b.timeframe == "H1"]
    assert len(h1) == 1
    assert h1[0].open == Decimal("2000")
    assert h1[0].tick_volume == 60 * 10


def test_d1_bucket_rolls_after_24_hours() -> None:
    """24*60 M1 bars starting at midnight; the next-day bar rolls the D1 bucket."""

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(24 * 60):
        builder.on_bar(_m1_bar(base + timedelta(minutes=i), 2000, 2000.5, 1999.5, 2000.2, tv=5))
    # Day-2 bar (00:00 next day) rolls the D1.
    closed = list(
        builder.on_bar(_m1_bar(base + timedelta(days=1), 2010, 2011, 2009, 2010.5))
    )
    d1 = [b for b in closed if b.timeframe == "D1"]
    assert len(d1) == 1
    assert d1[0].tick_volume == 24 * 60 * 5


# =============================================================== 6. symbol guard


def test_ohlc_builder_rejects_other_symbol() -> None:
    """A bar for a different symbol must be rejected with ValueError."""

    builder = OHLCBuilder(symbol="XAUUSD")
    bad = Bar(
        symbol="EURUSD",
        timeframe="M1",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("1.0"),
        high=Decimal("1.1"),
        low=Decimal("0.9"),
        close=Decimal("1.0"),
        tick_volume=10,
    )
    with pytest.raises(ValueError, match="EURUSD"):
        builder.on_bar(bad)


def test_ohlc_builder_rejects_other_symbol_on_tick() -> None:
    builder = OHLCBuilder(symbol="XAUUSD")
    bad = Tick(
        symbol="EURUSD",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        bid=Decimal("1.0"),
        ask=Decimal("1.01"),
    )
    with pytest.raises(ValueError, match="EURUSD"):
        # on_tick is a generator; the validation runs on first iteration.
        for _ in builder.on_tick(bad):
            pass


# =============================================================== 7. reset


def test_ohlc_builder_reset_clears_state() -> None:
    """reset() must drop all open and closed bars."""

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(3):
        builder.on_bar(_m1_bar(base + timedelta(minutes=i), 2000, 2001, 1999, 2000.5))
    assert len(builder.closed_bars("M1")) == 3
    builder.reset()
    assert builder.closed_bars("M1") == []
    assert builder.closed_bars_by_tf == {}


def test_ohlc_builder_non_m1_source_raises() -> None:
    """A non-M1 source timeframe is not yet supported in this block."""

    builder = OHLCBuilder(symbol="XAUUSD", source_timeframe="M5")
    with pytest.raises(NotImplementedError, match="M1 source"):
        builder.on_bar(
            _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 2000, 2001, 1999, 2000.5)
        )


# =============================================================== 8. cascade through tick input


def test_tick_input_cascades_into_m5() -> None:
    """Feeding ticks in minutes 0..4 then ticks in minute 5..6 closes
    the first M5 bucket.

    Cascade semantics: a tick in minute M closes the M1 at minute M-1
    (which cascades to on_bar). The M5 bucket rolls only when the
    minute crosses a multiple of 5 — so we need at least one tick in
    minute 5 to trigger the minute-4 M1 close (same M5 bucket), and
    one more tick in minute 6 to trigger the minute-5 M1 close
    (different M5 bucket) which rolls the M5.
    """

    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # 5 ticks in minutes 0..4, all in the M5 bucket [0, 5).
    for i in range(5):
        for _ in builder.on_tick(_tick(base + timedelta(seconds=i * 5), 2000.0 + i)):
            pass
    # A tick in minute 5 closes minute 4 (same M5 bucket — no roll yet).
    for _ in builder.on_tick(_tick(base + timedelta(minutes=5), 2010.0)):
        pass
    # A tick in minute 6 closes minute 5 (different M5 bucket — rolls the M5).
    for _ in builder.on_tick(_tick(base + timedelta(minutes=6), 2020.0)):
        pass
    m5 = builder.closed_bars("M5")
    assert len(m5) == 1
    assert m5[0].time == base  # bucket start = 00:00
    # _tick uses bid=mid, ask=mid+0.01 → aggregator mid = mid + 0.005.
    assert m5[0].low == Decimal("2000.005")
    assert m5[0].high == Decimal("2004.005")
