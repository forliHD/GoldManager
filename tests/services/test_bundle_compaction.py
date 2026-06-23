"""Tests for transport compaction of the FeatureSnapshotBundle.

The feature- and decision-engines publish a *compacted* bundle on the
``features`` / ``decisions`` streams (see
:func:`xauusd_bot.common.messaging.compact.compact_bundle`). The rich
bundle's ``fvg.zones`` and ``structure`` history are ~99 % of the wire
payload and the root cause of Redis OOM. These tests pin the two
properties that make compaction safe:

* the payload shrinks by an order of magnitude, and
* everything the decision-aggregator scores and the execution-engine
  reads (latest swing high/low, open zones, rank order) survives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xauusd_bot.common.messaging.compact import compact_bundle
from xauusd_bot.common.messaging.events import FeaturesEvent
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    FVGOutput,
    FVGStatus,
    FVGType,
    FVGZone,
    LiquidityPool,
    MarketStructureOutput,
    SwingPoint,
)
from xauusd_bot.decision.aggregator import FeatureAggregator

_T0 = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _zone(
    tf: str,
    status: FVGStatus,
    *,
    rank: float,
    typ: FVGType = FVGType.BULLISH,
    top: float = 2400.0,
) -> FVGZone:
    return FVGZone(
        tf=tf,  # type: ignore[arg-type]
        type=typ,
        top=top,
        bottom=top - 1.0,
        size_points=1.0,
        created_at=_T0,
        age_seconds=60,
        displacement_atr=1.0,
        status=status,
        mitigation_pct=100.0 if status is FVGStatus.MITIGATED else 0.0,
        rank_score=rank,
    )


def _swing(kind: str, idx: int, price: float) -> SwingPoint:
    return SwingPoint(
        kind=kind,  # type: ignore[arg-type]
        price=price,
        time=_T0 + timedelta(minutes=idx),
        bar_index=idx,
    )


def _bundle(
    *,
    zones: list[FVGZone] | None = None,
    top_zones: list[FVGZone] | None = None,
    swings: list[SwingPoint] | None = None,
    pools: list[LiquidityPool] | None = None,
) -> FeatureSnapshotBundle:
    fvg = None
    if zones is not None:
        fvg = FVGOutput(zones=zones, top_zones=top_zones or zones[:3])
    structure = None
    if swings is not None or pools is not None:
        structure = MarketStructureOutput(
            swings=swings or [],
            liquidity_pools=pools or [],
            fractal_n=3,
        )
    return FeatureSnapshotBundle(ts=_T0, fvg=fvg, structure=structure, atr=2.0)


# ---------------------------------------------------------------- fvg zones


def test_keeps_all_open_and_partial_zones() -> None:
    zones = [_zone("M1", FVGStatus.MITIGATED, rank=0.0) for _ in range(100)]
    zones += [_zone("M5", FVGStatus.OPEN, rank=5.0) for _ in range(7)]
    zones += [_zone("H1", FVGStatus.PARTIALLY_MITIGATED, rank=4.0) for _ in range(3)]

    out = compact_bundle(_bundle(zones=zones), max_mitigated_zones_per_tf=10)

    kept = out.fvg.zones
    assert sum(1 for z in kept if z.status is FVGStatus.OPEN) == 7
    assert sum(1 for z in kept if z.status is FVGStatus.PARTIALLY_MITIGATED) == 3
    # The 100 mitigated M1 zones are capped to the per-tf sample.
    assert sum(1 for z in kept if z.status is FVGStatus.MITIGATED) == 10


def test_mitigated_sample_is_per_timeframe() -> None:
    zones = [_zone("M1", FVGStatus.MITIGATED, rank=0.0) for _ in range(50)]
    zones += [_zone("M5", FVGStatus.MITIGATED, rank=0.0) for _ in range(50)]

    out = compact_bundle(_bundle(zones=zones), max_mitigated_zones_per_tf=10)

    by_tf = {tf: sum(1 for z in out.fvg.zones if z.tf == tf) for tf in ("M1", "M5")}
    assert by_tf == {"M1": 10, "M5": 10}


def test_preserves_rank_order() -> None:
    # take_profit picks the *first* qualifying zone in rank order, so the
    # kept subset must stay in the original order.
    zones = [_zone("M5", FVGStatus.OPEN, rank=float(100 - i)) for i in range(20)]
    out = compact_bundle(_bundle(zones=zones))
    ranks = [z.rank_score for z in out.fvg.zones]
    assert ranks == sorted(ranks, reverse=True)


def test_top_zones_survive_even_when_mitigated_tail_dropped() -> None:
    # A mitigated zone beyond the per-tf cap that is also a headline
    # top_zones member must not be dropped.
    tail = [_zone("M1", FVGStatus.MITIGATED, rank=0.0) for _ in range(30)]
    headline = _zone("M1", FVGStatus.MITIGATED, rank=9.9, top=2500.0)
    zones = tail + [headline]
    out = compact_bundle(
        _bundle(zones=zones, top_zones=[headline]), max_mitigated_zones_per_tf=5
    )
    assert any(z.rank_score == 9.9 for z in out.fvg.zones)


def test_zero_mitigated_cap_drops_all_mitigated() -> None:
    zones = [_zone("M1", FVGStatus.MITIGATED, rank=0.0) for _ in range(20)]
    open_zone = _zone("M5", FVGStatus.OPEN, rank=1.0)
    zones += [open_zone]
    out = compact_bundle(
        _bundle(zones=zones, top_zones=[open_zone]), max_mitigated_zones_per_tf=0
    )
    assert all(z.status is not FVGStatus.MITIGATED for z in out.fvg.zones)
    assert len(out.fvg.zones) == 1


# ---------------------------------------------------------------- swings


def test_windows_swings_to_most_recent() -> None:
    swings = [_swing("high" if i % 2 else "low", i, 2400.0 + i) for i in range(200)]
    out = compact_bundle(_bundle(swings=swings), max_swings=50)
    assert len(out.structure.swings) == 50
    # The most-recent swing survives (highest bar_index).
    assert out.structure.swings[-1].bar_index == 199


def test_structure_h1_is_windowed_too() -> None:
    # The H1 structure must be slimmed the same way as the M5 structure.
    swings = [_swing("high" if i % 2 else "low", i, 2400.0 + i) for i in range(200)]
    bundle = FeatureSnapshotBundle(
        ts=_T0,
        structure_h1=MarketStructureOutput(swings=swings, fractal_n=2),
        atr=2.0,
    )
    out = compact_bundle(bundle, max_swings=50)
    assert len(out.structure_h1.swings) == 50
    assert out.structure_h1.swings[-1].bar_index == 199


def test_latest_high_and_low_preserved_for_execution() -> None:
    # Execution's _last_swing reads the latest high and the latest low.
    swings = [_swing("high" if i % 2 else "low", i, 2400.0 + i) for i in range(200)]
    out = compact_bundle(_bundle(swings=swings), max_swings=50)

    def last(sw: list[SwingPoint], kind: str) -> float:
        return next(s.price for s in reversed(sw) if s.kind == kind)

    assert last(out.structure.swings, "high") == last(swings, "high")
    assert last(out.structure.swings, "low") == last(swings, "low")


def test_one_sided_tail_still_keeps_latest_low() -> None:
    # A long run of highs at the tail would push the latest low out of a
    # naive window — compaction must re-inject it.
    swings = [_swing("low", 0, 2399.0)]
    swings += [_swing("high", i, 2400.0 + i) for i in range(1, 60)]
    out = compact_bundle(_bundle(swings=swings), max_swings=5)
    lows = [s for s in out.structure.swings if s.kind == "low"]
    assert lows and lows[-1].price == 2399.0


def test_drops_liquidity_pools() -> None:
    pools = [
        LiquidityPool(kind="high", price=2400.0 + i, created_at=_T0)
        for i in range(100)
    ]
    out = compact_bundle(_bundle(swings=[_swing("high", 0, 2400.0)], pools=pools))
    assert out.structure.liquidity_pools == []


# ---------------------------------------------------------------- properties


def test_idempotent() -> None:
    zones = [_zone("M1", FVGStatus.MITIGATED, rank=0.0) for _ in range(80)]
    zones += [_zone("M5", FVGStatus.OPEN, rank=1.0) for _ in range(5)]
    swings = [_swing("high" if i % 2 else "low", i, 2400.0 + i) for i in range(120)]
    once = compact_bundle(_bundle(zones=zones, swings=swings))
    twice = compact_bundle(once)
    assert twice.model_dump_json() == once.model_dump_json()


def test_none_engines_pass_through() -> None:
    bundle = FeatureSnapshotBundle(ts=_T0)
    out = compact_bundle(bundle)
    assert out.fvg is None
    assert out.structure is None


def test_payload_shrinks_order_of_magnitude() -> None:
    # A realistic worst-case: thousands of mitigated zones + long history.
    zones = [_zone("M1", FVGStatus.MITIGATED, rank=0.0) for _ in range(7000)]
    zones += [_zone("M5", FVGStatus.MITIGATED, rank=0.0) for _ in range(1000)]
    zones += [_zone("M5", FVGStatus.OPEN, rank=5.0) for _ in range(40)]
    swings = [_swing("high" if i % 2 else "low", i, 2400.0 + i) for i in range(700)]
    pools = [LiquidityPool(kind="high", price=2400.0 + i, created_at=_T0) for i in range(700)]
    full = _bundle(zones=zones, swings=swings, pools=pools)
    compact = compact_bundle(full)

    full_bytes = len(FeaturesEvent(symbol="XAUUSD", bundle=full).model_dump_json().encode())
    compact_bytes = len(FeaturesEvent(symbol="XAUUSD", bundle=compact).model_dump_json().encode())
    assert compact_bytes * 10 < full_bytes


def test_aggregator_subscore_values_unchanged() -> None:
    # The aggregator penalises M5 when mitigated zones > 5. Keeping a
    # per-tf mitigated sample above that threshold means the numeric
    # subscore is identical even though the mitigated tail was dropped.
    zones = [_zone("M5", FVGStatus.MITIGATED, rank=0.0) for _ in range(40)]
    zones += [_zone("M5", FVGStatus.OPEN, rank=5.0, typ=FVGType.BULLISH) for _ in range(6)]
    zones += [_zone("H1", FVGStatus.OPEN, rank=6.0, typ=FVGType.BEARISH) for _ in range(2)]
    full = _bundle(zones=zones)
    compact = compact_bundle(full, max_mitigated_zones_per_tf=10)

    agg_full = FeatureAggregator().aggregate(full)
    agg_compact = FeatureAggregator().aggregate(compact)

    for name, sub in agg_full.subscores.items():
        other = agg_compact.subscores[name]
        assert (sub.value, sub.direction_bias, sub.raw) == (
            other.value,
            other.direction_bias,
            other.raw,
        ), name
    assert agg_full.dominant_engine == agg_compact.dominant_engine
    assert [c.model_dump() for c in agg_full.conflicts] == [
        c.model_dump() for c in agg_compact.conflicts
    ]
