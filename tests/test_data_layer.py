"""Data-layer unit tests: OHLCBuilder, SpreadMonitor, DataQualityMonitor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar, SymbolSpec
from xauusd_bot.data.ohlc_builder import OHLCBuilder
from xauusd_bot.data.quality_monitor import DataQualityMonitor
from xauusd_bot.data.spread_monitor import SpreadMonitor


def _spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD",
        description="XAUUSD CFD",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
        price_limit_max=Decimal("5000"),
    )


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


# --------------------------------------------------------------- OHLCBuilder


def test_ohlc_builder_aggregates_m5_from_m1() -> None:
    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Feed 5 M1 bars that all fall in the same M5 bucket.
    for i in range(5):
        bar = _m1_bar(base + timedelta(minutes=i), 2000 + i, 2002 + i, 1999 + i, 2001 + i)
        builder.on_bar(bar)
    # The 6th bar at 00:05 rolls the M5 bucket.
    bar5 = _m1_bar(base + timedelta(minutes=5), 2010, 2015, 2005, 2012)
    closed = list(builder.on_bar(bar5))
    # The M5 closure should be exactly one bar, and it should span 00:00..00:05.
    [m5] = [b for b in closed if b.timeframe == "M5"]
    assert m5.time == base
    assert m5.open == Decimal("2000")
    # high is the max of bars 0..4: max(2002..2006) = 2006
    assert m5.high == Decimal("2006")
    assert m5.low == Decimal("1999")
    # close is the close of the last bar in the bucket (bar index 4)
    assert m5.close == Decimal("2005")


def test_ohlc_builder_h1_rolls_after_60_minutes() -> None:
    builder = OHLCBuilder(symbol="XAUUSD")
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(60):
        bar = _m1_bar(base + timedelta(minutes=i), 2000, 2001, 1999, 2000.5, tv=10)
        builder.on_bar(bar)
    # The 61st bar at 01:00 rolls the H1 bucket.
    roll = _m1_bar(base + timedelta(hours=1), 2010, 2011, 2009, 2010.5)
    closed = list(builder.on_bar(roll))
    h1 = [b for b in closed if b.timeframe == "H1"]
    assert len(h1) == 1
    assert h1[0].open == Decimal("2000")
    assert h1[0].tick_volume == 60 * 10


# --------------------------------------------------------------- SpreadMonitor


def test_spread_monitor_percentiles() -> None:
    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"), window=100, warn_points=200, block_points=400)
    for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        m.update_from_points(v)
    s = m.snapshot()
    # Linear-interpolated percentiles. n=10 evenly-spaced samples [10,20,...,100].
    # p50 rank = 0.5 * 9 = 4.5 → 50 + 0.5*(60-50) = 55.0
    # p90 rank = 0.9 * 9 = 8.1 → 90 + 0.1*(100-90) = 91.0
    # p99 rank = 0.99 * 9 = 8.91 → 90 + 0.91*(100-90) = 99.1
    assert s.p50 == 55.0
    assert s.p90 == 91.0
    assert s.p99 == 99.1
    assert s.n == 10
    assert s.is_outlier is False  # absolute warn is 200, current is 100


def test_spread_monitor_flags_high_spread() -> None:
    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"), warn_points=50, block_points=100)
    for v in [10, 20, 30, 40]:
        m.update_from_points(v)
    s1 = m.snapshot()
    assert s1.is_outlier is False
    m.update_from_points(60)
    s2 = m.snapshot()
    assert s2.is_outlier is True
    m.update_from_points(150)
    s3 = m.snapshot()
    assert s3.is_block is True


def test_spread_monitor_from_tick() -> None:
    from xauusd_bot.connectors.schemas import Tick

    m = SpreadMonitor(symbol="XAUUSD", point=Decimal("0.01"))
    t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    tick = Tick(symbol="XAUUSD", time=t, bid=Decimal("2000.00"), ask=Decimal("2000.50"))
    m.update_from_tick(tick)
    assert m.last == 50.0  # 0.50 / 0.01 = 50 points


# --------------------------------------------------------------- DataQualityMonitor


def test_quality_monitor_flags_ohlc_inconsistency() -> None:
    qm = DataQualityMonitor(spec=_spec())
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 100, 99, 101, 100)
    qm.update(bad)
    assert qm.report.n_ohlc_inconsistent == 1


def test_quality_monitor_flags_gap() -> None:
    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    qm.update(_m1_bar(base, 100, 101, 99, 100))
    # Skip a bar, feed at +2 minutes.
    qm.update(_m1_bar(base + timedelta(minutes=2), 101, 102, 100, 101))
    assert qm.report.n_gaps == 1
    assert qm.report.max_gap_bars == 1


def test_quality_monitor_clean_run() -> None:
    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(20):
        bar = _m1_bar(base + timedelta(minutes=i), 2000 + i * 0.10, 2000.5 + i * 0.10, 1999.5 + i * 0.10, 2000.2 + i * 0.10, tv=10)
        qm.update(bar)
    assert qm.report.n_bars == 20
    assert qm.report.n_gaps == 0
    assert qm.report.n_ohlc_inconsistent == 0
    assert qm.report.n_spikes == 0


# --------------------------------------------------------------- SymbolSpecLoader


def test_symbol_spec_loader_caches() -> None:
    from xauusd_bot.data.symbol_spec_loader import SymbolSpecLoader

    spec = _spec()
    fetch_calls: list[str] = []

    def fetch(symbol: str) -> SymbolSpec:
        fetch_calls.append(symbol)
        return spec

    loader = SymbolSpecLoader(fetch=fetch)
    a = loader.get("XAUUSD")
    b = loader.get("XAUUSD")
    assert a is b
    assert fetch_calls == ["XAUUSD"]

    # Refresh detects no change.
    new_spec, changed = loader.refresh("XAUUSD")
    assert changed is False
    assert new_spec is spec
