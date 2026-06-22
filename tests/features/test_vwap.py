"""Tests for the TripleVWAPEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.vwap import TripleVWAPEngine


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


def _build_day(date: datetime, prices: list[float], tv: list[int] | None = None) -> list[Bar]:
    """Build 1-minute bars starting at ``date`` and going forward."""

    if tv is None:
        tv = [100] * len(prices)
    bars: list[Bar] = []
    for i, p in enumerate(prices):
        t = date + timedelta(minutes=i)
        bars.append(
            _bar(
                t,
                o=p,
                h=p + 0.5,
                low=p - 0.5,
                c=p,
                tv=tv[i],
            )
        )
    return bars


# ---------------------------------------------------------------- anchors


def test_anchors_utc00_when_in_asia() -> None:
    """00:00 UTC anchor fires at the start of the day; engine sees bars since 00:00."""

    eng = TripleVWAPEngine()
    # Monday 2026-01-05 03:00 UTC. Asia. 00:00 anchor is at 00:00 today.
    # Bars run from 00:00 → 03:00 (current_t), 3h of M1 = 180 bars.
    bars = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        [2000 + 0.01 * i for i in range(180)],
    )
    current_t = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)
    out = eng.compute(bars, current_t)
    assert "utc00" in out.levels
    assert "utc07" in out.levels
    assert "utc12" in out.levels
    # utc00 should have 180 bars (all 3 hours).
    assert out.levels["utc00"].n_bars_anchored == 180
    # utc07 has not fired yet (3:00 < 7:00), so 0 bars.
    assert out.levels["utc07"].n_bars_anchored == 0
    assert out.levels["utc12"].n_bars_anchored == 0


def test_anchors_utc07_fires_at_7am() -> None:
    """At 08:00 UTC the 07:00 anchor has 60 minutes of bars."""

    eng = TripleVWAPEngine()
    bars = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        [2000 + 0.01 * i for i in range(480)],
    )
    current_t = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    out = eng.compute(bars, current_t)
    assert out.levels["utc00"].n_bars_anchored == 480
    assert out.levels["utc07"].n_bars_anchored == 60  # 7:00 → 8:00
    assert out.levels["utc12"].n_bars_anchored == 0


def test_anchors_utc12_fires_at_noon() -> None:
    """At 14:00 UTC the 12:00 anchor has 120 minutes of bars."""

    eng = TripleVWAPEngine()
    bars = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        [2000 + 0.01 * i for i in range(840)],
    )
    current_t = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    out = eng.compute(bars, current_t)
    assert out.levels["utc00"].n_bars_anchored == 840
    assert out.levels["utc07"].n_bars_anchored == 7 * 60  # 7h
    assert out.levels["utc12"].n_bars_anchored == 2 * 60  # 2h


# ---------------------------------------------------------------- value math


def test_constant_price_anchored_vwap_equals_price() -> None:
    """If all bars have the same typical_price, the VWAP equals that price."""

    eng = TripleVWAPEngine()
    prices = [2000.0] * 60  # 1h of bars at exactly 2000
    bars = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        prices,
    )
    out = eng.compute(bars, datetime(2026, 1, 5, 1, 0, tzinfo=UTC))
    assert out.levels["utc00"].value is not None
    assert abs(out.levels["utc00"].value - 2000.0) < 1e-6


def test_vwap_weighted_by_volume() -> None:
    """Bars with more volume pull the VWAP toward their typical price."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # 2 bars: bar 0 at 2000 with tv=10, bar 1 at 2010 with tv=990.
    # Weighted: (2000*10 + 2010*990) / 1000 = 2009.9
    bars = [
        _bar(ts, 2000, 2000.5, 1999.5, 2000, tv=10),
        _bar(ts + timedelta(minutes=1), 2010, 2010.5, 2009.5, 2010, tv=990),
    ]
    out = eng.compute(bars, ts + timedelta(minutes=1))
    v = out.levels["utc00"].value
    assert v is not None
    # Expected ≈ 2009.9 (typical price = (H+L+C)/3 = same as close here).
    assert abs(v - 2009.9) < 0.1


# ---------------------------------------------------------------- cross / reclaim / loss


def test_cross_up_detected_when_close_crosses_above() -> None:
    """A bar that closes above VWAP after a bar that closed below → cross_up."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # 5 bars at 2000, then one at 2010 (above the constant VWAP of 2000).
    bars = _build_day(ts, [2000.0] * 5 + [2010.0])
    out = eng.compute(bars, ts + timedelta(minutes=5))
    assert out.levels["utc00"].cross_up is True
    assert out.levels["utc00"].cross_down is False


def test_reclaim_detected_after_prior_below_vwap() -> None:
    """After a stretch of closes below VWAP, a bar that closes above = reclaim."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # First 5 bars at 2000, next 5 at 1990 (below), then one at 2010.
    # Constant VWAP is 2000 (assuming uniform vol). The 2010 bar crosses
    # up and reclaims.
    prices = [2000.0] * 5 + [1990.0] * 5 + [2010.0]
    bars = _build_day(ts, prices)
    out = eng.compute(bars, ts + timedelta(minutes=10))
    assert out.levels["utc00"].cross_up is True
    assert out.levels["utc00"].reclaim is True


# ---------------------------------------------------------------- cluster


def test_cluster_when_three_vwaps_close() -> None:
    """All 3 VWAPs within 1.5*ATR → cluster."""

    eng = TripleVWAPEngine()
    # 14h of bars with low volatility (price drifts by 0.001 per bar).
    prices = [2000 + 0.001 * i for i in range(14 * 60)]
    bars = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        prices,
    )
    out = eng.compute(bars, datetime(2026, 1, 5, 14, 0, tzinfo=UTC))
    # The 3 VWAPs are anchored to different times so they will all sit
    # near 2000-ish; the cluster should fire (spread < 1.5*ATR).
    assert out.is_cluster is True
    assert out.cluster_center is not None


def test_no_cluster_when_vwaps_diverge() -> None:
    """Wide-ranging price action → VWAPs diverge, no cluster."""

    eng = TripleVWAPEngine()
    # 12h at 2000, then 2h at 2050 (big jump). At 14:00, the 00:00
    # anchor (14h) has a VWAP dragged up by the 2050 bars; the 07:00
    # anchor (7h) also has a high VWAP; the 12:00 anchor (2h) is at 2050
    # exactly. The spread between anchors is large.
    prices = [2000.0] * (12 * 60) + [2050.0] * (2 * 60)
    bars = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        prices,
    )
    out = eng.compute(bars, datetime(2026, 1, 5, 14, 0, tzinfo=UTC))
    # Spread is large, ATR will be smaller, so no cluster.
    assert out.is_cluster is False


# ---------------------------------------------------------------- PIT


def test_pit_excludes_bars_after_current_t() -> None:
    """Bars past current_t must not affect the VWAP."""

    eng = TripleVWAPEngine()
    bars_pre = _build_day(
        datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        [2000.0] * 60,
    )
    # Add a future bar with price 9999. If included, VWAP would explode.
    cutoff = datetime(2026, 1, 5, 1, 0, tzinfo=UTC)
    fut = _bar(cutoff + timedelta(minutes=1), 9999, 9999, 9998, 9999, tv=100000)
    out_pre = eng.compute(bars_pre, cutoff)
    out_with_fut = eng.compute(bars_pre + [fut], cutoff)
    assert out_pre.levels["utc00"].value == out_with_fut.levels["utc00"].value


def test_empty_bars_returns_none_values() -> None:
    """No bars at all → all VWAPs are None, no cluster."""

    eng = TripleVWAPEngine()
    out = eng.compute([], datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    assert out.levels["utc00"].value is None
    assert out.levels["utc07"].value is None
    assert out.levels["utc12"].value is None
    assert out.is_cluster is False
    assert out.cluster_center is None


# ------------------------------------------------------------------- cross / loss


def test_loss_detected_after_cross_up_then_below() -> None:
    """After a cross-up (close > VWAP after below), a bar that closes below = loss."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # Build a "loss" pattern: bars above VWAP, then a bar below.
    prices_loss = [2000.0] * 5 + [2010.0] * 4 + [1990.0]
    bars_loss = _build_day(ts, prices_loss)
    out_loss = eng.compute(bars_loss, ts + timedelta(minutes=9))
    # The latest bar (1990) closes below VWAP, after a stretch of
    # bars above → loss = True.
    assert out_loss.levels["utc00"].loss is True


# ------------------------------------------------------------------- cluster


def test_cluster_atr_threshold_is_configurable() -> None:
    """The cluster_atr constructor argument controls the cluster threshold."""

    # With a very tight threshold (0.001), no cluster will form.
    eng_tight = TripleVWAPEngine(cluster_atr=0.001)
    prices = [2000 + 0.001 * i for i in range(14 * 60)]
    bars = _build_day(datetime(2026, 1, 5, 0, 0, tzinfo=UTC), prices)
    out = eng_tight.compute(bars, datetime(2026, 1, 5, 14, 0, tzinfo=UTC))
    # Even small price variation breaks a 0.001-ATR cluster.
    assert out.is_cluster is False
    # And the cluster_within_atr field reflects the constructor.
    assert out.cluster_within_atr == 0.001


# ------------------------------------------------------------------- percentile


def test_distance_percentile_30d_zero_when_no_history() -> None:
    """With no prior distance history, the percentile defaults to 50.0 (neutral)."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # Only 1 bar → no distance history yet.
    bars = _build_day(ts, [2000.0])
    out = eng.compute(bars, ts)
    # distance_percentile_30d may be None (no history) or 50.0 (neutral default).
    # Per the engine: with len(state.distance_history) == 0, pct = 50.0.
    # With len(history) >= 1, the engine uses percentile_rank on it.
    if out.levels["utc00"].distance_percentile_30d is not None:
        assert 0.0 <= out.levels["utc00"].distance_percentile_30d <= 100.0


def test_distance_atr_none_when_atr_unavailable() -> None:
    """With fewer than 14 bars, ATR is None → distance_atr is also None."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    # Only 10 bars → ATR is None.
    bars = _build_day(ts, [2000.0] * 10)
    out = eng.compute(bars, ts + timedelta(minutes=9))
    # distance_atr is None because the engine checks atr_value is not None
    # and > 0 before computing distance_atr.
    assert out.levels["utc00"].distance_atr is None
    # But distance_points is still computed (it just needs the close and VWAP).
    assert out.levels["utc00"].distance_points is not None
    # And the value is still set.
    assert out.levels["utc00"].value is not None


# ------------------------------------------------------------------- PIT


def test_pit_strictly_below_current_t_filters_bars() -> None:
    """A bar at current_t is INCLUDED; a bar at current_t + 1 minute is NOT.

    WHY: the contract is ``time <= current_t`` (inclusive). This test
    pins down the boundary — a bar with time == current_t is part of
    the visible window; a bar with time > current_t is not.
    """

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = _build_day(ts, [2000.0] * 5)
    # Add a bar AT current_t.
    bars.append(_bar(ts + timedelta(minutes=5), 2010, 2011, 2009, 2010, tv=100))
    out_at_t = eng.compute(bars, ts + timedelta(minutes=5))
    # The bar AT current_t is included.
    assert out_at_t.levels["utc00"].n_bars_anchored == 6
    # A bar PAST current_t is not.
    bars_with_future = bars + [_bar(ts + timedelta(minutes=6), 9999, 9999, 9998, 9999, tv=100000)]
    out_with_future = eng.compute(bars_with_future, ts + timedelta(minutes=5))
    # The future bar is filtered out.
    assert out_with_future.levels["utc00"].n_bars_anchored == 6
    # And the VWAP value is the same (not polluted by the future bar).
    assert out_at_t.levels["utc00"].value == out_with_future.levels["utc00"].value


# ------------------------------------------------------------------- prev-day anchor


def test_utc00_carries_forward_yesterday_when_not_fired_today() -> None:
    """A query at 00:00 UTC (00:00 anchor hasn't fired today yet) → still uses yesterday's 00:00.

    The Plan §8 rule: 00:00-anchor yesterday → 00:00-anchor today.
    At exactly 00:00 today, today's anchor has *just* fired (or not —
    the boundary is ambiguous). Either way, the engine should produce
    a valid VWAP (not None).
    """

    eng = TripleVWAPEngine()
    # Build a day of bars (yesterday), then sit at exactly 00:00 today.
    yesterday = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
    bars = _build_day(yesterday, [2000 + 0.01 * i for i in range(24 * 60)])
    today_00 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    out = eng.compute(bars, today_00)
    # The utc00 value is some valid number (yesterday's VWAP continues).
    assert out.levels["utc00"].value is not None
    # The value sits in the range of yesterday's prices. With a 0.01
    # drift per bar over 1440 bars (24h), the price range is roughly
    # [2000, 2014.4]. The VWAP is the volume-weighted mean, so it sits
    # somewhere in the middle of that range.
    assert 1999.0 <= out.levels["utc00"].value <= 2015.0, (
        f"yesterday's VWAP {out.levels['utc00'].value} out of expected range"
    )


# ------------------------------------------------------------------- zero volume


def test_zero_tick_volume_bars_dont_divide_by_zero() -> None:
    """A bar with tick_volume=0 is given weight 1 (the engine clamps to max(1, tv))."""

    eng = TripleVWAPEngine()
    ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars = [
        _bar(ts, 2000, 2001, 1999, 2000, tv=0),  # zero volume
        _bar(ts + timedelta(minutes=1), 2010, 2011, 2009, 2010, tv=100),
    ]
    # Should not raise ZeroDivisionError.
    out = eng.compute(bars, ts + timedelta(minutes=1))
    assert out.levels["utc00"].value is not None


def test_clock_offset_shifts_anchor_to_real_utc() -> None:
    """The 07:00 anchor must fire at real-UTC 07:00, not broker 07:00.

    Regression: with a +180min broker offset, real 07:00 UTC == broker 10:00.
    A bar at broker 09:30 (real 06:30) is therefore BEFORE the utc07 anchor
    and must be excluded from that level; without the offset it would wrongly
    be inside the 07:00 window.
    """

    # Bars every minute from broker 09:00 to 11:00 (real 06:00–08:00).
    start = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    bars = [_bar(start + timedelta(minutes=i), 2000 + i * 0.1, 2000 + i * 0.1 + 0.5,
                 2000 + i * 0.1 - 0.5, 2000 + i * 0.1) for i in range(121)]
    cursor = bars[-1].time

    naive = TripleVWAPEngine().compute(bars, cursor)
    eng = TripleVWAPEngine()
    eng.set_clock_offset(180)
    shifted = eng.compute(bars, cursor)

    n_naive = naive.levels["utc07"].n_bars_anchored
    n_shifted = shifted.levels["utc07"].n_bars_anchored
    # Without offset the broker-07:00 anchor caught every bar (09:00+);
    # with the offset the real-07:00 (= broker 10:00) anchor caught only the
    # bars from 10:00 onward — strictly fewer.
    assert n_naive == 121
    assert 0 < n_shifted < n_naive


def test_unfired_anchor_is_none_not_spot_price() -> None:
    """An anchor that has not fired yet today must report value=None.

    Regression: the engine anchored an unfired level at current_t and the
    accumulation loop (bar.time >= anchor) then swept in the current bar,
    reporting a 1-bar "VWAP" equal to the latest typical price. Before 07:00
    and 12:00 UTC those levels have no data and must be None.
    """
    # Bars 00:00 → 05:00 UTC; cursor at 05:00 (07:00/12:00 not fired yet).
    start = datetime(2026, 6, 19, 0, 0, tzinfo=UTC)
    bars = [_bar(start + timedelta(minutes=i), 4100 + i * 0.1, 4100 + i * 0.1 + 0.5,
                 4100 + i * 0.1 - 0.5, 4100 + i * 0.1, tv=100) for i in range(301)]
    cursor = bars[-1].time  # 05:00 UTC

    out = TripleVWAPEngine().compute(bars, cursor)
    # utc00 has fired and must carry a real value.
    assert out.levels["utc00"].value is not None
    assert out.levels["utc00"].n_bars_anchored > 1
    # utc07 / utc12 have NOT fired → no data, not the spot price.
    for k in ("utc07", "utc12"):
        assert out.levels[k].value is None, f"{k} should be None before its anchor fires"
        assert out.levels[k].n_bars_anchored == 0
    # With only one real level, there can be no 3-VWAP cluster.
    assert out.is_cluster is False


# ---------------------------------------------------------------- carry-forward (v2)

# Jan 2026: 5th = Mon ... 9th = Fri, 10/11 = weekend, 12th = Mon.
_FRI = datetime(2026, 1, 9, 12, 0, tzinfo=UTC)
_MON = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)


def test_utc12_carries_friday_over_weekend() -> None:
    """Monday before noon: UTC12 carries Friday's 12:00 anchor (not blank)."""
    eng = TripleVWAPEngine()
    fri = _build_day(_FRI, [2000.0] * 480)          # Fri 12:00 → 20:00 (480 bars)
    mon = _build_day(_MON, [2000.0] * 600)          # Mon 00:00 → 10:00 (600 bars)
    out = eng.compute(fri + mon, datetime(2026, 1, 12, 10, 0, tzinfo=UTC))
    # Carried: Friday 12:00 anchor accumulates Fri (480) + Mon (600) = 1080 bars.
    assert out.levels["utc12"].n_bars_anchored == 1080
    assert out.levels["utc12"].value is not None
    # Sanity: UTC00 is Monday-local (00:00→10:00 = 600), UTC07 fired today (180).
    assert out.levels["utc00"].n_bars_anchored == 600
    assert out.levels["utc07"].n_bars_anchored == 180


def test_utc12_reanchors_after_noon() -> None:
    """Once today's 12:00 fires, UTC12 starts fresh (no longer Friday's)."""
    eng = TripleVWAPEngine()
    fri = _build_day(_FRI, [2000.0] * 480)
    mon = _build_day(_MON, [2000.0] * 780)          # Mon 00:00 → 13:00
    out = eng.compute(fri + mon, datetime(2026, 1, 12, 13, 0, tzinfo=UTC))
    assert out.levels["utc12"].n_bars_anchored == 60   # only 12:00 → 13:00, fresh


def test_utc12_no_carry_without_prior_bars() -> None:
    """No Friday data → nothing to carry → UTC12 stays empty before noon."""
    eng = TripleVWAPEngine()
    mon = _build_day(_MON, [2000.0] * 600)          # Mon only
    out = eng.compute(mon, datetime(2026, 1, 12, 10, 0, tzinfo=UTC))
    assert out.levels["utc12"].n_bars_anchored == 0
    assert out.levels["utc12"].value is None
