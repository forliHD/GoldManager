"""Tests for the decision-layer Pydantic schemas.

The factory functions in :file:`conftest.py` are also re-defined
here as plain helpers (not pytest fixtures) so they can be called
from inside test bodies and assertion expressions. Conftest
auto-injects them as fixtures by name, but pytest does not
auto-import conftest as a module, so a local copy is the simplest
path that avoids an ``__init__.py`` + ``sys.path`` shuffle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    ConflictEntry,
    Decision,
    DecisionAction,
    EngineSubscore,
    EntryType,
    Score,
    ScoreBand,
    TradeQualification,
)
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    NewsContextOutput,
)
from xauusd_bot.connectors.schemas import AccountInfo


# ---------------------------------------------------------------- inline factories


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


# ---------------------------------------------------------------- tests


class TestEngineSubscore:
    def test_value_in_range(self) -> None:
        sub = make_subscore("test", value=50.0)
        assert sub.value == 50.0
        assert sub.weight == 10.0
        assert sub.direction_bias == 0

    def test_value_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EngineSubscore(name="x", value=150.0, weight=10.0)
        with pytest.raises(ValidationError):
            EngineSubscore(name="x", value=-1.0, weight=10.0)

    def test_weight_must_be_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            EngineSubscore(name="x", value=50.0, weight=-5.0)

    def test_direction_bias_must_be_in_literal(self) -> None:
        sub = EngineSubscore(name="x", value=50.0, weight=10.0, direction_bias=1)  # type: ignore[arg-type]
        assert sub.direction_bias == 1
        sub2 = EngineSubscore(name="x", value=50.0, weight=10.0, direction_bias=-1)  # type: ignore[arg-type]
        assert sub2.direction_bias == -1


class TestAggregatedFeatures:
    def test_total_score_uses_weighted_normalization(self) -> None:
        """Total = sum(value * weight/100) — NOT raw value*weight."""

        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=100.0, weight=50.0),
                "b": make_subscore("b", value=0.0, weight=50.0),
            }
        )
        assert agg.total_score == 50.0

    def test_total_score_all_hundred(self) -> None:
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=100.0, weight=70.0),
                "b": make_subscore("b", value=100.0, weight=30.0),
            }
        )
        assert agg.total_score == 100.0

    def test_total_score_all_zero(self) -> None:
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=0.0, weight=70.0),
                "b": make_subscore("b", value=0.0, weight=30.0),
            }
        )
        assert agg.total_score == 0.0

    def test_dominant_engine_picks_max_contribution(self) -> None:
        agg = make_aggregated(
            subscores={
                "a": make_subscore("a", value=80.0, weight=10.0),
                "b": make_subscore("b", value=90.0, weight=5.0),
                "c": make_subscore("c", value=50.0, weight=20.0),
            }
        )
        assert agg.dominant_engine == "c"

    def test_to_source_snapshot(self) -> None:
        bundle = FeatureSnapshotBundle(
            ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            session=None,
            atr=0.35,
            structure=None,
            news=NewsContextOutput(
                minutes_until_next_high_impact=None,
                in_blackout_flag=False,
                next_high_impact=None,
                upcoming_events=[],
                surprise_score=0.0,
            ),
        )
        agg = make_aggregated(has_data=True)
        snap = agg.to_source_snapshot(bundle)
        assert snap["atr"] == 0.35
        assert snap["news_in_blackout"] is False

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AggregatedFeatures(
                ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
                symbol="XAUUSD",
                subscores={},
                conflicts=[],
                dominant_engine=None,
                has_data=True,
                not_a_field="oops",  # type: ignore[call-arg]
            )


class TestScore:
    def test_band_for_thresholds(self) -> None:
        assert Score.band_for(0.0) == ScoreBand.BELOW_55
        assert Score.band_for(54.99) == ScoreBand.BELOW_55
        assert Score.band_for(55.0) == ScoreBand.OBSERVE_55_64
        assert Score.band_for(64.99) == ScoreBand.OBSERVE_55_64
        assert Score.band_for(65.0) == ScoreBand.PREPARE_65_74
        assert Score.band_for(74.99) == ScoreBand.PREPARE_65_74
        assert Score.band_for(75.0) == ScoreBand.REDUCED_75_84
        assert Score.band_for(84.99) == ScoreBand.REDUCED_75_84
        assert Score.band_for(85.0) == ScoreBand.FULL_85_PLUS
        assert Score.band_for(100.0) == ScoreBand.FULL_85_PLUS

    def test_score_range_validated(self) -> None:
        with pytest.raises(ValidationError):
            Score(
                total_score=150.0,
                band=ScoreBand.FULL_85_PLUS,
                direction="long",
                timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            )
        with pytest.raises(ValidationError):
            Score(
                total_score=-1.0,
                band=ScoreBand.BELOW_55,
                direction="long",
                timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            )

    def test_direction_literal_validated(self) -> None:
        with pytest.raises(ValidationError):
            Score(
                total_score=70.0,
                band=ScoreBand.PREPARE_65_74,
                direction="up",  # type: ignore[arg-type]
                timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            )


class TestDecision:
    def test_blocked_decision_has_no_entry_type(self) -> None:
        d = Decision(
            action=DecisionAction.NO_TRADE,
            entry_type=None,
            block_reason="news_blackout",
            source_score=50.0,
            source_band=ScoreBand.OBSERVE_55_64,
            source_direction="neutral",
            timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        )
        assert d.action == DecisionAction.NO_TRADE
        assert d.entry_type is None
        assert d.block_reason == "news_blackout"

    def test_enter_decision_has_entry_type(self) -> None:
        d = Decision(
            action=DecisionAction.ENTER_LONG,
            entry_type=EntryType.SCOUT,
            block_reason=None,
            source_score=68.0,
            source_band=ScoreBand.PREPARE_65_74,
            source_direction="long",
            timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        )
        assert d.entry_type == EntryType.SCOUT
        assert d.block_reason is None


class TestTradeQualification:
    def test_from_decision_qualified(self) -> None:
        d = Decision(
            action=DecisionAction.ENTER_LONG,
            entry_type=EntryType.FULL,
            block_reason=None,
            source_score=90.0,
            source_band=ScoreBand.FULL_85_PLUS,
            source_direction="long",
            timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        )
        score = Score(
            total_score=90.0,
            band=ScoreBand.FULL_85_PLUS,
            direction="long",
            timestamp=d.timestamp,
        )
        qual = TradeQualification.from_decision(d, score)
        assert qual.qualified is True
        assert qual.final_action == DecisionAction.ENTER_LONG
        assert qual.final_entry_type == EntryType.FULL
        assert qual.block_reasons == []

    def test_from_decision_vetoed(self) -> None:
        d = Decision(
            action=DecisionAction.NO_TRADE,
            entry_type=None,
            block_reason="news_blackout",
            source_score=50.0,
            source_band=ScoreBand.OBSERVE_55_64,
            source_direction="neutral",
            timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        )
        score = Score(
            total_score=50.0,
            band=ScoreBand.OBSERVE_55_64,
            direction="neutral",
            timestamp=d.timestamp,
        )
        qual = TradeQualification.from_decision(d, score, extra_block_reasons=["no_clear_tp_target"])
        assert qual.qualified is False
        assert qual.final_action == DecisionAction.NO_TRADE
        assert qual.block_reasons == ["no_clear_tp_target"]

    def test_qualification_id_is_unique_uuid(self) -> None:
        d = Decision(
            action=DecisionAction.ENTER_LONG,
            entry_type=EntryType.SCOUT,
            block_reason=None,
            source_score=70.0,
            source_band=ScoreBand.PREPARE_65_74,
            source_direction="long",
            timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        )
        score = Score(
            total_score=70.0,
            band=ScoreBand.PREPARE_65_74,
            direction="long",
            timestamp=d.timestamp,
        )
        q1 = TradeQualification.from_decision(d, score)
        q2 = TradeQualification.from_decision(d, score)
        assert q1.qualification_id != q2.qualification_id


class TestAccountInfo:
    """The new optional risk fields are None by default (no block)."""

    def test_default_risk_fields_are_none(self) -> None:
        ai = AccountInfo(
            login=1,
            broker="x",
            balance=Decimal("1000"),
            equity=Decimal("1000"),
            margin=Decimal("0"),
            free_margin=Decimal("1000"),
            server_time=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        )
        assert ai.daily_pnl is None
        assert ai.weekly_pnl is None
        assert ai.current_spread is None

    def test_explicit_risk_fields(self) -> None:
        ai = AccountInfo(
            login=1,
            broker="x",
            balance=Decimal("1000"),
            equity=Decimal("1000"),
            margin=Decimal("0"),
            free_margin=Decimal("1000"),
            server_time=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            daily_pnl=Decimal("-100"),
            weekly_pnl=Decimal("-300"),
            current_spread=Decimal("35"),
        )
        assert ai.daily_pnl == Decimal("-100")
        assert ai.weekly_pnl == Decimal("-300")
        assert ai.current_spread == Decimal("35")
