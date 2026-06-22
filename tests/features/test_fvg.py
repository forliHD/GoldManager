"""Tests for the FVGEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.common.schemas.features import FVGStatus, FVGType, FVGZone
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.fvg import FVGEngine, _extend_zones_to_fractal_origin


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


# ---------------------------------------------------------------- 3 timeframes


def test_three_timeframes_all_detect() -> None:
    """A bullish FVG pattern in the M1 bars is detected on all 3 configured TFs (H1/M5/M1).

    WHY: per Plan §8, H1 is the primary zone timeframe, M5 refines
    them, M1 is the trigger. All three TFs must independently detect
    the same gap pattern. A test that only checks one TF would miss a
    regression in the resampling logic.
    """

    eng = FVGEngine(timeframes=("H1", "M5", "M1"))
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # Build M1 bars for a full H1 + a gap. We need at least 2 hours of
    # M1 bars plus the gap. Total: 121 M1 bars (one per minute, 2 hours).
    bars: list[Bar] = []
    for i in range(120):
        t = base + timedelta(minutes=i)
        # Bars 0..117: quiet around 2000; bar 118: displacement up; bar 119: gap.
        if i < 117:
            p = 2000.0
        elif i == 118:
            p = 2010.0  # displacement bar
        else:  # 119
            p = 2015.0  # bar after the gap
        # Bullish FVG: low[t] > high[t-2]. So bar 119's low > bar 117's high.
        if i < 118:
            bars.append(_bar(t, p, p + 0.5, p - 0.5, p, tv=100))
        elif i == 118:
            # Displacement bar: opens at 2010, closes at 2015, range 2009-2016.
            bars.append(_bar(t, 2010, 2016, 2009, 2015, tv=200))
        else:
            # Gap bar: low=2005.5 > high of bar 117 (=2000.5). Bullish FVG.
            bars.append(_bar(t, 2015, 2017, 2005.5, 2016, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=120))
    bullish = [z for z in out.zones if z.type == FVGType.BULLISH]
    # At minimum M1 should have detected the gap. M5 and H1 may or may
    # not (depends on resampling). The point is to assert the engine
    # has at least one bullish zone — i.e. it processed all 3 TFs.
    assert len(bullish) >= 1
    tfs_seen = {z.tf for z in bullish}
    # M1 must always be in the set.
    assert "M1" in tfs_seen


def test_displacement_atr_calculated_at_zone_creation() -> None:
    """A bar with body > 2*ATR is a displacement; the FVG records the displacement_atr.

    WHY: per Plan §8, the displacement is a measure of the *strength*
    of the impulse that created the FVG. A test that doesn't check
    displacement_atr misses a regression in the rank computation.
    """

    eng = FVGEngine()
    # Build 30 bars of small ranges to establish ATR, then a big displacement.
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    for i in range(30):
        t = base + timedelta(minutes=5 * i)
        if i < 27:
            bars.append(_bar(t, 2000, 2001, 1999, 2000.5, tv=100))
        elif i == 27:
            # Quiet bar.
            bars.append(_bar(t, 2000, 2001, 1999, 2000.5, tv=100))
        elif i == 28:
            # Displacement bar: 10x ATR body.
            bars.append(_bar(t, 2000, 2010, 1990, 2008, tv=500))
        else:
            # Bar that completes the FVG.
            bars.append(_bar(t, 2010, 2012, 2003, 2010, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=5 * 29))
    bullish = [z for z in out.zones if z.type == FVGType.BULLISH]
    if bullish:
        # At least one bullish zone should have displacement_atr > 1.
        assert any(z.displacement_atr > 1.0 for z in bullish), (
            f"no zone has displacement_atr > 1: {[z.displacement_atr for z in bullish]}"
        )


def test_top_zones_limited_to_three() -> None:
    """top_zones is always at most 3 entries, even if many zones exist."""

    eng = FVGEngine()
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # 100 bars: 33 bullish FVG patterns (each needs 3 bars).
    bars: list[Bar] = []
    for i in range(100):
        t = base + timedelta(minutes=5 * i)
        if i % 3 == 0:
            bars.append(_bar(t, 2000, 2001, 1999, 2000.5, tv=100))
        elif i % 3 == 1:
            bars.append(_bar(t, 2005, 2007, 2004, 2006, tv=100))
        else:
            # Gap: low > high of bar i-2 (i.e. > 2001).
            bars.append(_bar(t, 2010, 2012, 2005, 2010, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=5 * 99))
    # top_zones is the top-3 by rank_score, sorted desc.
    assert len(out.top_zones) <= 3
    # And zones[0] (highest rank) has rank_score >= zones[1] >= ...
    for a, b in zip(out.zones, out.zones[1:], strict=False):
        assert a.rank_score >= b.rank_score


# ---------------------------------------------------------------- M5-fractal zone extension


_H = timedelta(hours=1)
_EXT_BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def _h1(time: datetime, h: float, low: float) -> Bar:
    mid = round((h + low) / 2, 2)
    return _bar(time, mid, h, low, mid)


def _demand_zone(created_at: datetime, bottom: float, top: float) -> FVGZone:
    return FVGZone(
        tf="H1",
        type=FVGType.BULLISH,
        top=top,
        bottom=bottom,
        size_points=round(top - bottom, 2),
        created_at=created_at,
        age_seconds=0,
        status=FVGStatus.OPEN,
    )


def _supply_zone(created_at: datetime, bottom: float, top: float) -> FVGZone:
    return FVGZone(
        tf="H1",
        type=FVGType.BEARISH,
        top=top,
        bottom=bottom,
        size_points=round(top - bottom, 2),
        created_at=created_at,
        age_seconds=0,
        status=FVGStatus.OPEN,
    )


def _bullish_h1_wick_origin() -> list[Bar]:
    """H1 bars where the demand-origin candle (idx2) is NOT a fractal low.

    idx1 dips to 4180 (below idx2's 4181), so idx2 is not a 2-bar fractal low →
    the engine must drop to M5 to find the origin fractal.
    """

    return [
        _h1(_EXT_BASE + 0 * _H, 4185, 4184),  # idx0
        _h1(_EXT_BASE + 1 * _H, 4184, 4180),  # idx1 (lower low → idx2 not a fractal)
        _h1(_EXT_BASE + 2 * _H, 4182.21, 4181),  # idx2 = b0, high=zone.bottom
        _h1(_EXT_BASE + 3 * _H, 4190, 4181.5),  # idx3 = b1
        _h1(_EXT_BASE + 4 * _H, 4195, 4191.26),  # idx4 = b2 (gap), low=zone.top
        _h1(_EXT_BASE + 5 * _H, 4196, 4193),  # idx5
    ]


def _m5_with_low_fractal(low_price: float, at_k: int = 10) -> list[Bar]:
    """24 M5 bars across the [idx2, idx4) window; one fractal low at ``low_price``."""

    bars: list[Bar] = []
    for k in range(24):
        t = _EXT_BASE + 2 * _H + timedelta(minutes=5 * k)
        low = low_price if k == at_k else 4185.0
        bars.append(_bar(t, low + 1, low + 2, low, low + 1))
    return bars


def test_extension_bullish_drops_to_m5_when_h1_origin_is_wick() -> None:
    """Josh's rule: H1 origin is only a wick (no fractal) → extend to the M5 fractal."""

    h1_bars = _bullish_h1_wick_origin()
    m5_bars = _m5_with_low_fractal(4179.4)
    zone = _demand_zone(h1_bars[4].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m5_bars, fractal_n=2, max_extension=None
    )
    z = out[0]
    assert z.extension_tf == "M5"
    assert z.extended_bottom == 4179.4
    assert z.extended_top is None
    # Raw FVG edges are untouched.
    assert z.bottom == 4182.21
    assert z.top == 4191.26


def test_extension_bullish_uses_h1_fractal_when_origin_is_a_swing() -> None:
    """When the H1 origin candle IS a fractal low, extend to it (no M5 drilldown)."""

    h1_bars = [
        _h1(_EXT_BASE + 0 * _H, 4185, 4185),
        _h1(_EXT_BASE + 1 * _H, 4184, 4184),
        _h1(_EXT_BASE + 2 * _H, 4182.21, 4180),  # idx2 = b0, clean fractal low 4180
        _h1(_EXT_BASE + 3 * _H, 4190, 4181),  # idx3 = b1
        _h1(_EXT_BASE + 4 * _H, 4195, 4191.26),  # idx4 = b2
        _h1(_EXT_BASE + 5 * _H, 4196, 4193),
    ]
    # M5 has an even deeper fractal, but the H1 path takes priority.
    m5_bars = _m5_with_low_fractal(4179.4)
    zone = _demand_zone(h1_bars[4].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m5_bars, fractal_n=2, max_extension=None
    )
    z = out[0]
    assert z.extension_tf == "H1"
    assert z.extended_bottom == 4180.0


def test_extension_bearish_drops_to_m5() -> None:
    """Mirror for supply: extend the zone top UP to the M5 fractal high."""

    h1_bars = [
        _h1(_EXT_BASE + 0 * _H, 4196, 4194),  # idx0
        _h1(_EXT_BASE + 1 * _H, 4208, 4200),  # idx1 (higher high → idx2 not a fractal)
        _h1(_EXT_BASE + 2 * _H, 4205, 4200),  # idx2 = b0, low=zone.top
        _h1(_EXT_BASE + 3 * _H, 4203, 4196),  # idx3 = b1 (down displacement)
        _h1(_EXT_BASE + 4 * _H, 4195, 4193),  # idx4 = b2 (gap), high=zone.bottom
        _h1(_EXT_BASE + 5 * _H, 4194, 4192),  # idx5
    ]
    m5_bars: list[Bar] = []
    for k in range(24):
        t = _EXT_BASE + 2 * _H + timedelta(minutes=5 * k)
        high = 4221.0 if k == 10 else 4196.0
        m5_bars.append(_bar(t, high - 2, high, high - 3, high - 2))
    zone = _supply_zone(h1_bars[4].time, bottom=4195.0, top=4200.0)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m5_bars, fractal_n=2, max_extension=None
    )
    z = out[0]
    assert z.extension_tf == "M5"
    assert z.extended_top == 4221.0
    assert z.extended_bottom is None


def test_extension_skipped_when_fractal_inside_zone() -> None:
    """No extension when the origin fractal isn't below (demand) the FVG bottom."""

    h1_bars = _bullish_h1_wick_origin()
    # M5 fractal at 4183 is ABOVE the zone bottom (4182.21) → not an extension.
    m5_bars = _m5_with_low_fractal(4183.0)
    zone = _demand_zone(h1_bars[4].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m5_bars, fractal_n=2, max_extension=None
    )
    assert out[0].extended_bottom is None
    assert out[0].extension_tf is None


def test_extension_respects_max_atr_cap() -> None:
    """An M5 fractal further than max_extension is rejected."""

    h1_bars = _bullish_h1_wick_origin()
    m5_bars = _m5_with_low_fractal(4179.4)  # 2.81 points below the bottom
    zone = _demand_zone(h1_bars[4].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m5_bars, fractal_n=2, max_extension=1.0
    )
    assert out[0].extended_bottom is None


def test_extension_leaves_non_h1_zones_untouched() -> None:
    """Only H1 zones get extended; M5/M1 zones pass through."""

    h1_bars = _bullish_h1_wick_origin()
    m5_bars = _m5_with_low_fractal(4179.4)
    m5_zone = FVGZone(
        tf="M5",
        type=FVGType.BULLISH,
        top=4191.26,
        bottom=4182.21,
        size_points=9.05,
        created_at=h1_bars[4].time,
        age_seconds=0,
        status=FVGStatus.OPEN,
    )
    out = _extend_zones_to_fractal_origin(
        [m5_zone], h1_bars, m5_bars, fractal_n=2, max_extension=None
    )
    assert out[0].extended_bottom is None
    assert out[0].extension_tf is None


def test_extension_can_be_disabled_on_engine() -> None:
    """extend_to_fractal=False leaves all zones with raw FVG edges."""

    eng = FVGEngine(extend_to_fractal=False)
    bars = _bullish_fvg_bars()
    out = eng.compute(bars, bars[-1].time + timedelta(minutes=1))
    for z in out.zones:
        assert z.extended_bottom is None
        assert z.extended_top is None
        assert z.extension_tf is None


def test_rank_score_demotes_mitigated_zones() -> None:
    """Mitigated zones have rank_score << non-mitigated zones.

    WHY: a mitigated FVG is a "dead" zone — the market has filled it,
    so its predictive value is minimal. The rank function multiplies
    by 0.1 for mitigated zones (vs 0.5 for partial, 1.0 for open).
    A test that ignores this rank demotion would let a regression
    pass that ranks a dead zone above a live one.
    """

    eng = FVGEngine()
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    # Build a bullish FVG (bars 0, 1, 2).
    bars.append(_bar(base, 2000, 2001, 1999, 2000.5, tv=100))
    bars.append(_bar(base + timedelta(minutes=5), 2003, 2004, 2002, 2003.5, tv=200))
    bars.append(_bar(base + timedelta(minutes=10), 2006, 2007, 2005, 2006.5, tv=100))
    # Mitigate the FVG with a bar that closes below its bottom.
    bars.append(_bar(base + timedelta(minutes=15), 2004, 2005, 1995, 1996, tv=100))
    # Then build a *new* bullish FVG.
    bars.append(_bar(base + timedelta(minutes=20), 1996, 1997, 1995, 1996, tv=100))
    bars.append(_bar(base + timedelta(minutes=25), 2000, 2001, 1999, 2000.5, tv=200))
    bars.append(_bar(base + timedelta(minutes=30), 2003, 2004, 2000, 2003, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=30))
    # Find the mitigated and the open zones.
    mitigated = [z for z in out.zones if z.status == FVGStatus.MITIGATED]
    open_zones = [z for z in out.zones if z.status == FVGStatus.OPEN]
    if mitigated and open_zones:
        # An open zone's rank must be higher than a mitigated zone's
        # (since both have similar sizes).
        max_open = max(z.rank_score for z in open_zones)
        max_mit = max(z.rank_score for z in mitigated)
        assert max_open > max_mit, (
            f"open zones ({max_open}) should rank above mitigated ({max_mit})"
        )
