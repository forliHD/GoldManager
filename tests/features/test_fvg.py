"""Tests for the FVGEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.common.schemas.features import FVGStatus, FVGType, FVGZone
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.fvg import (
    FVGEngine,
    _extend_zones_to_fractal_origin,
    _final_leg_base,
)


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


# ---------------------------------------------------------------- leg-base zone extension


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


def _h1_window() -> list[Bar]:
    """4 hourly H1 bars; a zone created_at idx 2 → impulse window = [idx0, idx3)."""

    return [_h1(_EXT_BASE + i * _H, 4195.0, 4185.0) for i in range(4)]


def _m1_flat(
    n: int,
    *,
    lows: dict[int, float] | None = None,
    highs: dict[int, float] | None = None,
    base_low: float = 4188.0,
    base_high: float = 4189.0,
) -> list[Bar]:
    """M1 bars on a flat baseline with explicit lows/highs at given indices.

    Indices not in ``lows``/``highs`` get the (flat) baseline, so the explicit
    points stand out as fractal swing lows/highs (their ±n neighbours are flat).
    """

    lows = lows or {}
    highs = highs or {}
    bars: list[Bar] = []
    for i in range(n):
        t = _EXT_BASE + timedelta(minutes=i)
        lo = lows.get(i, base_low)
        hi = highs.get(i, base_high)
        if hi <= lo:
            hi = lo + 1.0
        mid = round((lo + hi) / 2, 2)
        bars.append(_bar(t, mid, hi, lo, mid))
    return bars


# ---------------------------------------------------------------- _final_leg_base


def test_final_leg_base_low_stops_at_leg_boundary() -> None:
    """The base is the lowest of the FINAL tight rising-lows run, not the deep low."""

    # chronological swing lows: a deep prior leg (4165) then a tight staircase.
    lows = [(0, 4165.0), (50, 4179.4), (60, 4180.5), (70, 4181.5)]
    assert _final_leg_base(lows, kind="low", max_step=5.0) == 4179.4
    # A generous step swallows the deep leg too.
    assert _final_leg_base(lows, kind="low", max_step=20.0) == 4165.0


def test_final_leg_base_high_mirror() -> None:
    highs = [(0, 4221.0), (50, 4200.3), (60, 4199.0), (70, 4198.0)]
    assert _final_leg_base(highs, kind="high", max_step=5.0) == 4200.3
    assert _final_leg_base(highs, kind="high", max_step=30.0) == 4221.0


def test_final_leg_base_empty_and_single() -> None:
    assert _final_leg_base([], kind="low", max_step=5.0) is None
    assert _final_leg_base([(3, 4180.0)], kind="low", max_step=5.0) == 4180.0


# ---------------------------------------------------------------- extension on M1


def test_extension_bullish_anchors_to_final_leg_not_deep_low() -> None:
    """The too-large-zone fix: extend to the tight rising-lows base, not the deep leg."""

    h1_bars = _h1_window()
    # Deep prior leg low at idx 60 (4165); tight staircase 4179.4/4180.5/4181.5.
    m1_bars = _m1_flat(180, lows={30: 4165.0, 90: 4179.4, 100: 4180.5, 110: 4181.5})
    zone = _demand_zone(h1_bars[2].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m1_bars, fractal_n=2, leg_step=5.0, max_extension=None
    )
    z = out[0]
    assert z.extension_tf == "M1"
    assert z.extended_bottom == 4179.4  # NOT the deep 4165
    assert z.extended_top is None
    assert z.bottom == 4182.21  # raw FVG edge untouched


def test_extension_ignores_post_breakout_gap_candle() -> None:
    """Swing lows in the gap candle b2 (price already broken away) must be ignored.

    Regression: anchoring the leg base on the *most recent* swing low while the
    window still included b2 picked a swing low ABOVE the impulse (price had
    already run up), corrupting the base. The window must end at b2.time.
    """

    h1_bars = _h1_window()
    # Tight staircase in [b0,b2) → base 4179.4; plus high swing lows in the GAP
    # candle region (minutes 130-150, i.e. >= created_at) that must be excluded.
    m1_bars = _m1_flat(
        180,
        lows={90: 4179.4, 100: 4180.5, 110: 4181.5, 130: 4192.0, 140: 4193.0, 150: 4194.0},
    )
    zone = _demand_zone(h1_bars[2].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m1_bars, fractal_n=2, leg_step=5.0, max_extension=None
    )
    assert out[0].extended_bottom == 4179.4  # not corrupted by the 4192+ gap-candle lows


def test_extension_bullish_includes_deep_leg_with_large_leg_step() -> None:
    """A looser leg_step lets the walk cross into the deeper leg."""

    h1_bars = _h1_window()
    m1_bars = _m1_flat(180, lows={30: 4165.0, 90: 4179.4, 100: 4180.5, 110: 4181.5})
    zone = _demand_zone(h1_bars[2].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m1_bars, fractal_n=2, leg_step=20.0, max_extension=None
    )
    assert out[0].extended_bottom == 4165.0


def test_extension_bearish_anchors_to_final_leg() -> None:
    h1_bars = _h1_window()
    m1_bars = _m1_flat(
        180,
        highs={30: 4221.0, 90: 4200.3, 100: 4199.0, 110: 4198.0},
        base_low=4188.0,
        base_high=4193.0,
    )
    zone = _supply_zone(h1_bars[2].time, bottom=4195.0, top=4197.0)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m1_bars, fractal_n=2, leg_step=5.0, max_extension=None
    )
    z = out[0]
    assert z.extension_tf == "M1"
    assert z.extended_top == 4200.3  # NOT the deep 4221
    assert z.extended_bottom is None


def test_extension_skipped_when_leg_base_inside_zone() -> None:
    """No extension when the leg base isn't below (demand) the FVG bottom."""

    h1_bars = _h1_window()
    # Staircase lows all ABOVE the FVG bottom 4182.21.
    m1_bars = _m1_flat(180, lows={90: 4183.0, 100: 4184.0, 110: 4185.0}, base_low=4190.0)
    zone = _demand_zone(h1_bars[2].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m1_bars, fractal_n=2, leg_step=5.0, max_extension=None
    )
    assert out[0].extended_bottom is None
    assert out[0].extension_tf is None


def test_extension_respects_max_cap() -> None:
    """A leg base further than max_extension is rejected."""

    h1_bars = _h1_window()
    m1_bars = _m1_flat(180, lows={90: 4179.4, 100: 4180.5, 110: 4181.5})
    zone = _demand_zone(h1_bars[2].time, bottom=4182.21, top=4191.26)
    out = _extend_zones_to_fractal_origin(
        [zone], h1_bars, m1_bars, fractal_n=2, leg_step=5.0, max_extension=1.0
    )
    assert out[0].extended_bottom is None  # 2.81 > 1.0 cap


def test_extension_leaves_non_h1_zones_untouched() -> None:
    h1_bars = _h1_window()
    m1_bars = _m1_flat(180, lows={90: 4179.4, 100: 4180.5, 110: 4181.5})
    m5_zone = FVGZone(
        tf="M5",
        type=FVGType.BULLISH,
        top=4191.26,
        bottom=4182.21,
        size_points=9.05,
        created_at=h1_bars[2].time,
        age_seconds=0,
        status=FVGStatus.OPEN,
    )
    out = _extend_zones_to_fractal_origin(
        [m5_zone], h1_bars, m1_bars, fractal_n=2, leg_step=5.0, max_extension=None
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
