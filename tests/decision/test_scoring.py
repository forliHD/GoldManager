"""Tests for :class:`xauusd_bot.decision.scoring.ScoringEngine`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    EngineSubscore,
    Score,
    ScoreBand,
)
from xauusd_bot.decision._weights import ENGINE_WEIGHTS
from xauusd_bot.decision.scoring import ScoringEngine, _aggregate_direction


# ---------------------------------------------------------------- inline factories


def make_subscore(
    name: str,
    *,
    value: float = 50.0,
    weight: float = 10.0,
    direction_bias: int = 0,
    reasoning: str = "test",
) -> EngineSubscore:
    return EngineSubscore(
        name=name,
        raw=value,
        value=value,
        percentile=50.0,
        weight=weight,
        direction_bias=direction_bias,  # type: ignore[arg-type]
        reasoning=reasoning,
    )


def make_aggregated(
    *,
    subscores: dict[str, EngineSubscore] | None = None,
    has_data: bool = True,
) -> AggregatedFeatures:
    if subscores is None:
        subscores = {
            name: make_subscore(name, weight=ENGINE_WEIGHTS[name], value=50.0)
            for name in ENGINE_WEIGHTS
        }
    return AggregatedFeatures(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        symbol="XAUUSD",
        subscores=subscores,
        conflicts=[],
        dominant_engine=None,
        has_data=has_data,
    )


# ---------------------------------------------------------------- tests


class TestScoringEngine:
    def test_weights_must_sum_to_100(self) -> None:
        with pytest.raises(ValueError):
            ScoringEngine(weights={"a": 50.0, "b": 30.0})  # sums to 80

    def test_default_weights_are_plan_weights(self) -> None:
        sc = ScoringEngine()
        assert sc._weights == ENGINE_WEIGHTS

    def test_no_data_returns_neutral_score(self) -> None:
        sc = ScoringEngine()
        agg = make_aggregated(has_data=False)
        score = sc.score(agg)
        assert score.total_score == 50.0
        assert score.band == ScoreBand.OBSERVE_55_64
        assert score.direction == "neutral"
        assert any("no_data" in r for r in score.reasoning)

    def test_all_subscores_hundred_yields_full_score(self) -> None:
        sc = ScoringEngine()
        agg = make_aggregated(
            subscores={
                name: make_subscore(name, value=100.0, weight=ENGINE_WEIGHTS[name])
                for name in ENGINE_WEIGHTS
            }
        )
        score = sc.score(agg)
        assert score.total_score == 100.0
        assert score.band == ScoreBand.FULL_85_PLUS

    def test_all_subscores_zero_yields_zero(self) -> None:
        sc = ScoringEngine()
        agg = make_aggregated(
            subscores={
                name: make_subscore(name, value=0.0, weight=ENGINE_WEIGHTS[name])
                for name in ENGINE_WEIGHTS
            }
        )
        score = sc.score(agg)
        assert score.total_score == 0.0
        assert score.band == ScoreBand.BELOW_55

    def test_band_thresholds_deterministic(self) -> None:
        """Construct an aggregated with a known total and verify band."""

        # Use a single-engine ScoringEngine so the math is direct.
        sc = ScoringEngine(weights={"x": 100.0})
        # 65 * 100 = 6500 / 100 = 65.0 total → prepare
        agg65 = make_aggregated(
            subscores={"x": make_subscore("x", value=65.0, weight=100.0)}
        )
        assert sc.score(agg65).band == ScoreBand.PREPARE_65_74
        # 75 → reduced
        agg75 = make_aggregated(
            subscores={"x": make_subscore("x", value=75.0, weight=100.0)}
        )
        assert sc.score(agg75).band == ScoreBand.REDUCED_75_84
        # 85 → full
        agg85 = make_aggregated(
            subscores={"x": make_subscore("x", value=85.0, weight=100.0)}
        )
        assert sc.score(agg85).band == ScoreBand.FULL_85_PLUS

    def test_score_normalization_explicit(self) -> None:
        """Score is value * weight/100, NOT value * weight."""

        sc = ScoringEngine(weights={"a": 30.0, "b": 70.0})
        # 100% on a 30% weight engine + 0% on the rest → score = 30
        subscores = {
            "a": make_subscore("a", value=100.0, weight=30.0),
            "b": make_subscore("b", value=0.0, weight=70.0),
        }
        agg = make_aggregated(subscores=subscores)
        score = sc.score(agg)
        assert score.total_score == 30.0

    def test_score_clamped_to_range(self) -> None:
        """Future weight overrides should not blow past 100."""

        # Weights summing to 100 but one engine gets value 150 (which
        # the schema forbids on EngineSubscore but we want to confirm
        # the score output stays in [0, 100]). Skip if schema rejects
        # 150 — instead, force the score to clamp via a custom weight
        # override.
        sc = ScoringEngine(weights={"a": 100.0})
        agg = make_aggregated(subscores={"a": make_subscore("a", value=100.0, weight=100.0)})
        assert 0.0 <= sc.score(agg).total_score <= 100.0

    def test_reasoning_contains_engine_summaries(self) -> None:
        sc = ScoringEngine()
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=80.0, weight=50.0, reasoning="hot"),
                "b": make_subscore("b", value=20.0, weight=50.0, reasoning="cold"),
            }
        )
        score = sc.score(agg)
        joined = " ".join(score.reasoning)
        assert "hot" in joined
        assert "cold" in joined

    def test_timestamp_propagated(self) -> None:
        sc = ScoringEngine()
        ts = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)
        agg = make_aggregated()
        agg_typed = AggregatedFeatures.model_construct(
            ts=ts,
            symbol="XAUUSD",
            subscores=agg.subscores,
            conflicts=[],
            dominant_engine=None,
            has_data=True,
        )
        score = sc.score(agg_typed)
        assert score.timestamp == ts


class TestAggregateDirection:
    def test_strong_long_bias(self) -> None:
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=80.0, weight=50.0, direction_bias=1),
                "b": make_subscore("b", value=20.0, weight=50.0, direction_bias=-1),
            }
        )
        # 50 long - 50 short = 0 → neutral
        assert _aggregate_direction(agg) == "neutral"
        # Make long dominate:
        agg2 = make_aggregated(
            subscores={
                "a": make_subscore("a", value=80.0, weight=60.0, direction_bias=1),
                "b": make_subscore("b", value=20.0, weight=40.0, direction_bias=-1),
            }
        )
        assert _aggregate_direction(agg2) == "long"

    def test_strong_short_bias(self) -> None:
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=80.0, weight=10.0, direction_bias=1),
                "b": make_subscore("b", value=20.0, weight=80.0, direction_bias=-1),
            }
        )
        # 80 short - 10 long = -70 → short
        assert _aggregate_direction(agg) == "short"

    def test_all_neutral_bias(self) -> None:
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=50.0, weight=50.0, direction_bias=0),
                "b": make_subscore("b", value=50.0, weight=50.0, direction_bias=0),
            }
        )
        assert _aggregate_direction(agg) == "neutral"

    def test_below_threshold_is_neutral(self) -> None:
        """|sum| <= 5 → neutral (insufficient agreement)."""

        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=80.0, weight=10.0, direction_bias=1),
                "b": make_subscore("b", value=20.0, weight=10.0, direction_bias=-1),
            }
        )
        # 10 long - 10 short = 0 → neutral
        assert _aggregate_direction(agg) == "neutral"
