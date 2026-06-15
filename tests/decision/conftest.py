"""Shared fixtures + factories for xauusd_bot decision-layer tests.

Pytest auto-discovers this conftest and the factories are exposed
as session/function-scoped fixtures. Tests can also use them as
plain functions via the ``factories`` fixture, or import them
through ``pytest``'s fixture-injection mechanism.

For test code that wants a "plain helper function" (e.g. building
a :class:`FeatureSnapshotBundle` inside an assertion), the
factories are also re-exported as module-level callables at the
bottom of this file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    EngineSubscore,
)
from xauusd_bot.common.schemas.features import (
    CandleMomentumOutput,
    CandleMomentumPerBar,
    FeatureSnapshotBundle,
    FVGOutput,
    FVGStatus,
    FVGType,
    FVGZone,
    LiquidityEngineOutput,
    LiquidityZone,
    MarketStructureOutput,
    NewsContextOutput,
    SessionEngineOutput,
    SessionName,
    StructureEvent,
    StructureEventType,
    TripleVWAPOutput,
    ValueAreaStatus,
    VolumeProfileOutput,
    VolumeProfileState,
    VolumeRangeOutput,
    VWAPLevel,
    VWAPLevelOutput,
)


# ---------------------------------------------------------------- factories
# (Plain functions, not fixtures, so tests can call them directly.)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        "environment": "test",
    }
    base.update(overrides)
    return Settings(**base)


def make_volume_profile(
    *,
    state: VolumeProfileState = VolumeProfileState.DEVELOPING,
    value_status: ValueAreaStatus | None = ValueAreaStatus.WITHIN_VALUE,
    acceptance_count: int = 50,
    breakout: bool = False,
    rotation: bool = False,
) -> VolumeProfileOutput:
    return VolumeProfileOutput(
        name="weekly",
        state=state,
        period_start=datetime(2026, 4, 13, tzinfo=UTC),
        period_end=datetime(2026, 4, 20, tzinfo=UTC),
        bin_size=1.0,
        vah=2380.0,
        vpoc=2375.0,
        val=2370.0,
        value_area_pct=0.70,
        distance_to_vah_points=2.0,
        distance_to_vah_atr=0.5,
        distance_to_val_points=2.0,
        distance_to_val_atr=0.5,
        distance_to_vpoc_points=0.0,
        distance_to_vpoc_atr=0.0,
        value_status=value_status,
        acceptance_count=acceptance_count,
        rejection_count=5,
        rotation=rotation,
        breakout=breakout,
        n_bars=1440,
    )


def make_bundle(
    *,
    ts: datetime | None = None,
    atr: float | None = 0.35,
    session: SessionEngineOutput | None = None,
    vwap: TripleVWAPOutput | None = None,
    volume_range: VolumeRangeOutput | None = None,
    fvg: FVGOutput | None = None,
    structure: MarketStructureOutput | None = None,
    momentum: CandleMomentumOutput | None = None,
    liquidity: LiquidityEngineOutput | None = None,
    news: NewsContextOutput | None = None,
) -> FeatureSnapshotBundle:
    if ts is None:
        ts = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)
    if session is None:
        session = SessionEngineOutput(
            current_session=SessionName.LONDON,
            session_start=datetime(2026, 4, 15, 7, 0, tzinfo=UTC),
            session_end=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            session_open=Decimal("2370.00"),
            session_high=Decimal("2378.00"),
            session_low=Decimal("2368.00"),
            session_progress_pct=100.0,
            is_session_sweep=False,
            equal_highs_flag=False,
            equal_lows_flag=False,
            session_risk_factor=1.0,
        )
    if vwap is None:
        vwap = TripleVWAPOutput(
            levels={
                "utc00": VWAPLevelOutput(
                    level=VWAPLevel.UTC00,
                    value=2372.0,
                    distance_points=2.0,
                    distance_atr=5.0,
                    n_bars_anchored=600,
                ),
                "utc07": VWAPLevelOutput(
                    level=VWAPLevel.UTC07,
                    value=2373.0,
                    distance_points=1.0,
                    distance_atr=2.5,
                    n_bars_anchored=200,
                ),
                "utc12": VWAPLevelOutput(
                    level=VWAPLevel.UTC12,
                    value=2372.5,
                    distance_points=0.5,
                    distance_atr=1.25,
                    n_bars_anchored=60,
                ),
            },
            cluster_within_atr=1.5,
            is_cluster=True,
            cluster_center=2372.5,
        )
    if volume_range is None:
        weekly = make_volume_profile()
        monthly = make_volume_profile(state=VolumeProfileState.DEVELOPING)
        yearly = make_volume_profile(state=VolumeProfileState.DEVELOPING)
        volume_range = VolumeRangeOutput(
            weekly=weekly,
            monthly=monthly,
            yearly=yearly,
            cluster_within_atr=0.5,
        )
    if structure is None:
        structure = MarketStructureOutput(
            swings=[],
            last_bos=StructureEvent(
                type=StructureEventType.BOS_UP,
                level=2375.0,
                time=datetime(2026, 4, 15, 13, 0, tzinfo=UTC),
                bar_index=100,
                close=2376.0,
                distance_atr=0.5,
            ),
            last_choch=None,
            liquidity_pools=[],
            trend="up",
            fractal_n=3,
        )
    if fvg is None:
        fvg = FVGOutput(
            zones=[
                FVGZone(
                    tf="H1",
                    type=FVGType.BULLISH,
                    top=2375.0,
                    bottom=2373.0,
                    size_points=2.0,
                    created_at=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
                    age_seconds=5400,
                    displacement_atr=2.0,
                    status=FVGStatus.OPEN,
                    mitigation_pct=0.0,
                    rank_score=4.0,
                ),
                FVGZone(
                    tf="M5",
                    type=FVGType.BULLISH,
                    top=2374.0,
                    bottom=2373.5,
                    size_points=0.5,
                    created_at=datetime(2026, 4, 15, 13, 25, tzinfo=UTC),
                    age_seconds=300,
                    displacement_atr=1.5,
                    status=FVGStatus.OPEN,
                    mitigation_pct=0.0,
                    rank_score=1.5,
                ),
            ],
            top_zones=[],
        )
    if momentum is None:
        momentum = CandleMomentumOutput(
            by_tf={
                "M1": CandleMomentumPerBar(
                    body_size_atr=0.6,
                    wick_body_ratio=0.4,
                    close_position=0.7,
                    displacement=False,
                    impulsive_follow_through=2,
                    tick_volume_percentile=60.0,
                ),
                "M5": CandleMomentumPerBar(
                    body_size_atr=1.2,
                    wick_body_ratio=0.3,
                    close_position=0.75,
                    displacement=True,
                    impulsive_follow_through=3,
                    tick_volume_percentile=70.0,
                ),
            },
            score=68.0,
        )
    if liquidity is None:
        liquidity = LiquidityEngineOutput(
            tp_targets_above=[
                LiquidityZone(
                    kind="high",
                    price_low=2378.0,
                    price_high=2380.0,
                    center=2379.0,
                    pool_count=2,
                )
            ],
            tp_targets_below=[
                LiquidityZone(
                    kind="low",
                    price_low=2368.0,
                    price_high=2369.0,
                    center=2368.5,
                    pool_count=2,
                )
            ],
            sl_protection_zones=[],
        )
    if news is None:
        news = NewsContextOutput(
            minutes_until_next_high_impact=None,
            in_blackout_flag=False,
            next_high_impact=None,
            upcoming_events=[],
            surprise_score=0.0,
        )
    return FeatureSnapshotBundle(
        ts=ts,
        session=session,
        vwap=vwap,
        volume_range=volume_range,
        fvg=fvg,
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        news=news,
        atr=atr,
    )


def make_subscore(
    name: str,
    *,
    value: float = 50.0,
    weight: float = 10.0,
    direction_bias: int = 0,
    reasoning: str = "test",
    percentile: float = 50.0,
) -> EngineSubscore:
    return EngineSubscore(
        name=name,
        raw=value,
        value=value,
        percentile=percentile,
        weight=weight,
        direction_bias=direction_bias,  # type: ignore[arg-type]
        reasoning=reasoning,
    )


def make_aggregated(
    *,
    subscores: dict[str, EngineSubscore] | None = None,
    has_data: bool = True,
    conflicts: list | None = None,
    ts: datetime | None = None,
) -> AggregatedFeatures:
    from xauusd_bot.decision._weights import ENGINE_WEIGHTS

    if ts is None:
        ts = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)
    if subscores is None:
        subscores = {
            name: make_subscore(name, weight=ENGINE_WEIGHTS[name], value=50.0)
            for name in ENGINE_WEIGHTS
        }
    return AggregatedFeatures(
        ts=ts,
        symbol="XAUUSD",
        subscores=subscores,
        conflicts=conflicts or [],
        dominant_engine=(
            max(subscores.values(), key=lambda s: s.value * s.weight).name
            if subscores
            else None
        ),
        has_data=has_data,
    )


# ---------------------------------------------------------------- fixtures
# (Pytest auto-injects these into test functions by name.)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def default_bundle() -> FeatureSnapshotBundle:
    return make_bundle()


@pytest.fixture
def empty_bundle() -> FeatureSnapshotBundle:
    """Bundle with all engine outputs None (no_data path)."""

    return FeatureSnapshotBundle(ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC))


@pytest.fixture
def default_aggregated() -> AggregatedFeatures:
    return make_aggregated()
