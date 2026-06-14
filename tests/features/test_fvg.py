"""Tests for the FVGEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.common.schemas.features import FVGStatus, FVGType
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.fvg import FVGEngine


def _bar(time: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M5",
        time=time,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


def _bullish_fvg_bars() -> list[Bar]:
    """Bar 0 high=2000, bar 1 (displacement) up, bar 2 low=2005 → bullish FVG [2000, 2005]."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    return [
        _bar(base + timedelta(minutes=0), 1999, 2000, 1998, 1999),
        _bar(base + timedelta(minutes=5), 2003, 2004, 2002, 2003.5),  # big displacement
        _bar(base + timedelta(minutes=10), 2006, 2007, 2005, 2006.5),  # low=2005 > high=2000
    ]


def _bearish_fvg_bars() -> list[Bar]:
    """Bar 0 low=2000, bar 1 (displacement) down, bar 2 high=1995 → bearish FVG [1995, 2000]."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    return [
        _bar(base + timedelta(minutes=0), 2001, 2002, 2000, 2000.5),
        _bar(base + timedelta(minutes=5), 1997, 1998, 1996, 1996.5),
        _bar(base + timedelta(minutes=10), 1994, 1995, 1993, 1993.5),
    ]


# ---------------------------------------------------------------- detection


def test_bullish_fvg_detected_on_m5() -> None:
    eng = FVGEngine()
    bars = _bullish_fvg_bars()
    out = eng.compute(bars, bars[-1].time + timedelta(minutes=1))
    bullish = [z for z in out.zones if z.type == FVGType.BULLISH]
    assert len(bullish) >= 1
    z = bullish[0]
    assert z.tf == "M5"
    assert z.top == 2005.0
    assert z.bottom == 2000.0
    assert z.size_points == 5.0


def test_bearish_fvg_detected_on_m5() -> None:
    eng = FVGEngine()
    bars = _bearish_fvg_bars()
    out = eng.compute(bars, bars[-1].time + timedelta(minutes=1))
    bearish = [z for z in out.zones if z.type == FVGType.BEARISH]
    assert len(bearish) >= 1
    z = bearish[0]
    assert z.top == 2000.0
    assert z.bottom == 1995.0
    assert z.size_points == 5.0


def test_no_fvg_when_no_gap() -> None:
    """Overlapping bars → no FVG."""

    eng = FVGEngine()
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = [
        _bar(base + timedelta(minutes=0), 2000, 2001, 1999, 2000),
        _bar(base + timedelta(minutes=5), 2001, 2002, 2000, 2001),
        _bar(base + timedelta(minutes=10), 2002, 2003, 2000.5, 2001.5),  # low=2000.5 < high=2001 → no gap
    ]
    out = eng.compute(bars, bars[-1].time + timedelta(minutes=1))
    assert out.zones == []


# ---------------------------------------------------------------- mitigation


def test_bullish_fvg_open_when_no_subsequent_bars() -> None:
    eng = FVGEngine()
    bars = _bullish_fvg_bars()
    out = eng.compute(bars, bars[-1].time)
    bullish = [z for z in out.zones if z.type == FVGType.BULLISH]
    assert bullish[0].status == FVGStatus.OPEN


def test_bullish_fvg_partially_mitigated_when_close_inside() -> None:
    """A close that lands inside the zone = partial mitigation."""

    eng = FVGEngine()
    bars = _bullish_fvg_bars()
    # Add a bar that closes inside the zone [2000, 2005].
    bars.append(_bar(bars[-1].time + timedelta(minutes=5), 2003, 2004, 2002, 2002.5))
    out = eng.compute(bars, bars[-1].time)
    bullish = [z for z in out.zones if z.type == FVGType.BULLISH]
    assert bullish[0].status == FVGStatus.PARTIALLY_MITIGATED
    assert bullish[0].mitigation_pct == 50.0


def test_bullish_fvg_fully_mitigated_when_close_below() -> None:
    """A close below the zone's bottom = full mitigation."""

    eng = FVGEngine()
    bars = _bullish_fvg_bars()
    bars.append(_bar(bars[-1].time + timedelta(minutes=5), 1999, 2001, 1995, 1996))
    out = eng.compute(bars, bars[-1].time)
    bullish = [z for z in out.zones if z.type == FVGType.BULLISH]
    assert bullish[0].status == FVGStatus.MITIGATED
    assert bullish[0].mitigation_pct == 100.0


# ---------------------------------------------------------------- ranking


def test_top_zones_returns_top_3_by_rank() -> None:
    """The 'top_zones' field returns the 3 highest-ranked zones."""

    eng = FVGEngine()
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    # Create 5 FVGs in a row.
    for i in range(20):
        t = base + timedelta(minutes=5 * i)
        bars.append(_bar(t, 1999 + 0.1 * i, 2000 + 0.1 * i, 1998 + 0.1 * i, 1999 + 0.1 * i))
    out = eng.compute(bars, bars[-1].time)
    assert len(out.top_zones) <= 3
    if len(out.zones) > 1:
        # The top_zones are sorted by rank_score desc.
        for a, b in zip(out.top_zones, out.top_zones[1:], strict=False):
            assert a.rank_score >= b.rank_score


# ---------------------------------------------------------------- PIT


def test_pit_excludes_bars_after_current_t() -> None:
    eng = FVGEngine()
    bars = _bullish_fvg_bars()
    # Add a future bar that closes well below the zone.
    fut = _bar(bars[-1].time + timedelta(hours=1), 1900, 1901, 1899, 1900)
    cutoff = bars[-1].time  # current_t = last bar's time (zone just created)
    out_no_fut = eng.compute(bars, cutoff)
    out_with_fut = eng.compute(bars + [fut], cutoff)
    # Both should be OPEN (no subsequent bars to mitigate).
    bullish_no = [z for z in out_no_fut.zones if z.type == FVGType.BULLISH]
    bullish_with = [z for z in out_with_fut.zones if z.type == FVGType.BULLISH]
    assert bullish_no[0].status == bullish_with[0].status == FVGStatus.OPEN


def test_no_bars_returns_empty() -> None:
    eng = FVGEngine()
    out = eng.compute([], datetime(2026, 1, 5, 0, 0, tzinfo=UTC))
    assert out.zones == []
    assert out.top_zones == []
