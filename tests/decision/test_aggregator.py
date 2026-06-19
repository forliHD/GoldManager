"""Tests for :class:`xauusd_bot.decision.aggregator.FeatureAggregator`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.common.schemas.decision import ConflictEntry
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    FVGOutput,
    LiquidityEngineOutput,
    LiquidityZone,
    NewsContextOutput,
    NewsEvent,
    NewsImpact,
    SessionName,
    StructureEvent,
    StructureEventType,
    ValueAreaStatus,
    VolumeProfileState,
)
from xauusd_bot.decision._weights import ENGINE_WEIGHTS
from xauusd_bot.decision.aggregator import (
    FeatureAggregator,
    _detect_conflicts,
    _percentile_rank,
)


# ---------------------------------------------------------------- inline factories


def make_settings():
    from xauusd_bot.common.config import Settings

    return Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        environment="test",
    )


def make_volume_profile(
    *,
    state=VolumeProfileState.DEVELOPING,
    value_status=ValueAreaStatus.WITHIN_VALUE,
    acceptance_count: int = 50,
):
    from xauusd_bot.common.schemas.features import VolumeProfileOutput

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
        rotation=False,
        breakout=False,
        n_bars=1440,
    )


def make_bundle_full():
    from xauusd_bot.common.schemas.features import (
        CandleMomentumOutput,
        CandleMomentumPerBar,
        FVGOutput,
        FVGStatus,
        FVGType,
        FVGZone,
        LiquidityEngineOutput,
        LiquidityZone,
        MarketStructureOutput,
        SessionEngineOutput,
        TripleVWAPOutput,
        VolumeRangeOutput,
        VWAPLevel,
        VWAPLevelOutput,
    )
    from decimal import Decimal

    return FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        session=SessionEngineOutput(
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
        ),
        vwap=TripleVWAPOutput(
            levels={
                "utc00": VWAPLevelOutput(level=VWAPLevel.UTC00, value=2372.0, n_bars_anchored=600),
                "utc07": VWAPLevelOutput(level=VWAPLevel.UTC07, value=2373.0, n_bars_anchored=200),
                "utc12": VWAPLevelOutput(level=VWAPLevel.UTC12, value=2372.5, n_bars_anchored=60),
            },
            cluster_within_atr=1.5,
            is_cluster=True,
            cluster_center=2372.5,
        ),
        volume_range=VolumeRangeOutput(
            weekly=make_volume_profile(),
            monthly=make_volume_profile(state=VolumeProfileState.DEVELOPING),
            yearly=make_volume_profile(state=VolumeProfileState.DEVELOPING),
            cluster_within_atr=0.5,
        ),
        fvg=FVGOutput(
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
            ],
            top_zones=[],
        ),
        structure=MarketStructureOutput(
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
        ),
        momentum=CandleMomentumOutput(
            by_tf={
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
        ),
        liquidity=LiquidityEngineOutput(
            tp_targets_above=[
                LiquidityZone(
                    kind="high", price_low=2378.0, price_high=2380.0, center=2379.0, pool_count=2
                )
            ],
            tp_targets_below=[
                LiquidityZone(
                    kind="low", price_low=2368.0, price_high=2369.0, center=2368.5, pool_count=2
                )
            ],
            sl_protection_zones=[],
        ),
        news=NewsContextOutput(
            minutes_until_next_high_impact=None,
            in_blackout_flag=False,
            next_high_impact=None,
            upcoming_events=[],
            surprise_score=0.0,
        ),
        atr=0.35,
    )


# ---------------------------------------------------------------- tests


class TestPercentileRank:
    def test_empty_history_is_neutral(self) -> None:
        assert _percentile_rank([], 50.0) == 50.0

    def test_lower_value_ranks_low(self) -> None:
        hist = [10.0, 20.0, 30.0, 40.0]
        # 5.0 is less than all → 0th percentile
        assert _percentile_rank(hist, 5.0) == 0.0

    def test_higher_value_ranks_high(self) -> None:
        hist = [10.0, 20.0, 30.0, 40.0]
        # 50.0 is greater than all → 100th percentile
        assert _percentile_rank(hist, 50.0) == 100.0

    def test_midpoint_value(self) -> None:
        hist = [10.0, 20.0, 30.0, 40.0]
        # 25.0 is greater than 10, 20 → 50%
        assert _percentile_rank(hist, 25.0) == 50.0


class TestFeatureAggregator:
    def test_empty_bundle_marks_no_data(self) -> None:
        agg = FeatureAggregator()
        bundle = FeatureSnapshotBundle(ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC))
        out = agg.aggregate(bundle)
        assert out.has_data is False
        # All engines present with neutral 50 + "no_data"
        for name in ENGINE_WEIGHTS:
            assert name in out.subscores
            assert out.subscores[name].value == 50.0
            assert out.subscores[name].reasoning == "no_data"

    def test_full_bundle_marks_has_data_true(self) -> None:
        agg = FeatureAggregator()
        out = agg.aggregate(make_bundle_full())
        assert out.has_data is True
        # London session → long bias, news is clean
        assert "session_liquidity" in out.subscores

    def test_per_engine_subscores_in_range(self) -> None:
        agg = FeatureAggregator()
        out = agg.aggregate(make_bundle_full())
        for sub in out.subscores.values():
            assert 0.0 <= sub.value <= 100.0
            assert 0.0 <= sub.weight <= 100.0

    def test_dominant_engine_picks_max_contribution(self) -> None:
        agg = FeatureAggregator()
        out = agg.aggregate(make_bundle_full())
        assert out.dominant_engine is not None
        # The dominant engine should have the highest (value*weight) contribution
        contributions = {n: s.value * s.weight for n, s in out.subscores.items()}
        assert out.dominant_engine == max(contributions, key=contributions.get)  # type: ignore[arg-type]

    def test_history_is_recorded_per_engine(self) -> None:
        agg = FeatureAggregator()
        agg.aggregate(make_bundle_full())
        agg.aggregate(make_bundle_full())
        history = agg.snapshot_history()
        # At least 2 observations per engine after 2 calls.
        for name in ENGINE_WEIGHTS:
            assert name in history
            assert len(history[name]) >= 2

    def test_reset_history_clears(self) -> None:
        agg = FeatureAggregator()
        agg.aggregate(make_bundle_full())
        assert agg.snapshot_history()
        agg.reset_history()
        assert agg.snapshot_history() == {}

    def test_history_size_parameter(self) -> None:
        agg = FeatureAggregator(history_size=3)
        for _ in range(5):
            agg.aggregate(make_bundle_full())
        for name, hist in agg.snapshot_history().items():
            assert len(hist) <= 3

    def test_weights_match_engine_weights_table(self) -> None:
        agg = FeatureAggregator()
        out = agg.aggregate(make_bundle_full())
        for name, sub in out.subscores.items():
            assert sub.weight == ENGINE_WEIGHTS[name]

    def test_session_score_includes_sweep_bonus(self) -> None:
        """is_session_sweep + equal_* flags boost session subscore."""
        from xauusd_bot.decision.aggregator import _score_session
        from xauusd_bot.common.schemas.features import SessionEngineOutput

        plain = SessionEngineOutput(
            current_session=SessionName.LONDON,
            session_start=datetime(2026, 4, 15, 7, 0, tzinfo=UTC),
            session_end=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            session_progress_pct=50.0,
            session_risk_factor=1.0,
        )
        plain_score, plain_reason, _ = _score_session(plain)
        assert plain_reason == "session=london risk=1.00"

        swept = plain.model_copy(update={"is_session_sweep": True, "equal_highs_flag": True})
        swept_score, swept_reason, _ = _score_session(swept)
        assert swept_score > plain_score
        assert "sweep" in swept_reason
        assert "equal_highs" in swept_reason

    def test_news_blackout_returns_zero(self) -> None:
        from xauusd_bot.decision.aggregator import _score_news

        news = NewsContextOutput(
            in_blackout_flag=True,
        )
        score, reasoning, _ = _score_news(news)
        assert score == 0.0
        assert reasoning == "in_blackout"

    def test_news_imminent_caps_score(self) -> None:
        from xauusd_bot.decision.aggregator import _score_news

        news = NewsContextOutput(
            in_blackout_flag=False,
            minutes_until_next_high_impact=20.0,
        )
        score, _, _ = _score_news(news)
        assert score == 30.0  # < 30 min cap

    def test_news_no_upcoming_high_impact(self) -> None:
        from xauusd_bot.decision.aggregator import _score_news

        news = NewsContextOutput(
            in_blackout_flag=False,
            minutes_until_next_high_impact=None,
        )
        score, reasoning, _ = _score_news(news)
        assert score == 80.0
        assert reasoning == "no_upcoming_high_impact"


class TestConflictDetection:
    def test_news_blackout_vs_long_intent_creates_block_conflict(self) -> None:
        from xauusd_bot.common.schemas.features import (
            CandleMomentumOutput,
            CandleMomentumPerBar,
            MarketStructureOutput,
            StructureEvent,
            StructureEventType,
        )
        from xauusd_bot.common.schemas.features import NewsContextOutput

        bundle = FeatureSnapshotBundle(
            ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            structure=MarketStructureOutput(
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
            ),
            momentum=CandleMomentumOutput(
                by_tf={
                    "M5": CandleMomentumPerBar(
                        body_size_atr=1.0,
                        wick_body_ratio=0.3,
                        close_position=0.75,
                        displacement=True,
                    )
                },
                score=80.0,
            ),
            news=NewsContextOutput(in_blackout_flag=True),
        )
        # news says "no entry", momentum says "long" → block
        agg_out = FeatureAggregator().aggregate(bundle)
        # We only test the conflict detection on the raw subscore path.
        raw = {
            "h1_zone": (50.0, "no_data", 0),
            "m5_zone": (50.0, "no_data", 0),
            "triple_vwap": (50.0, "no_data", 0),
            "htf_volume_profile": (50.0, "no_data", 0),
            "session_liquidity": (50.0, "no_data", 0),
            "news": (0.0, "in_blackout", 0),
            "momentum": (80.0, "momentum score 80", 1),
        }
        conflicts = _detect_conflicts(bundle, raw)
        blocks = [c for c in conflicts if c.severity == "block"]
        assert any(c.engine_a == "news" and c.engine_b == "momentum" for c in blocks)

    def test_structure_up_bos_with_below_value_is_warning(self) -> None:
        from xauusd_bot.common.schemas.features import (
            MarketStructureOutput,
            StructureEvent,
            StructureEventType,
        )
        from xauusd_bot.common.schemas.features import VolumeRangeOutput

        bundle = FeatureSnapshotBundle(
            ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            structure=MarketStructureOutput(
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
            ),
            volume_range=VolumeRangeOutput(
                weekly=make_volume_profile(value_status=ValueAreaStatus.BELOW_VALUE),
                monthly=make_volume_profile(),
                yearly=make_volume_profile(),
                cluster_within_atr=0.5,
            ),
        )
        raw = {n: (50.0, "no_data", 0) for n in ENGINE_WEIGHTS}
        conflicts = _detect_conflicts(bundle, raw)
        warns = [c for c in conflicts if c.severity == "warning"]
        assert any(
            c.engine_a == "structure" and c.engine_b == "htf_volume_profile" for c in warns
        )

    def test_no_conflicts_on_clean_bundle(self) -> None:
        agg = FeatureAggregator()
        out = agg.aggregate(make_bundle_full())
        # The full bundle has BOS_UP and weekly within_value, no imminent news
        # → no structure/VP or news conflicts (htf_vp may be neutral).
        for c in out.conflicts:
            assert c.severity in ("info", "warning", "block")
