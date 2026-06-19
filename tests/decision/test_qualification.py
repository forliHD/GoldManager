"""Tests for :class:`xauusd_bot.decision.qualification.TradeQualificationEngine`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.common.config import Settings
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
    LiquidityEngineOutput,
    LiquidityZone,
    MarketStructureOutput,
    StructureEvent,
    StructureEventType,
)
from xauusd_bot.decision._weights import ENGINE_WEIGHTS
from xauusd_bot.decision.qualification import (
    REASON_ENGINE_SIGNALS_CONFLICT,
    REASON_NO_CLEAR_TP_TARGET,
    REASON_NO_LIQUIDITY_DATA,
    REASON_STRUCTURE_AGAINST_DIRECTION,
    REASON_VOLATILITY_OUT_OF_RANGE,
    TradeQualificationEngine,
)


# ---------------------------------------------------------------- inline factories


def make_settings(**overrides) -> Settings:
    base = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        "environment": "test",
    }
    base.update(overrides)
    return Settings(**base)


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
    conflicts: list | None = None,
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
        conflicts=conflicts or [],
        dominant_engine=None,
        has_data=has_data,
    )


def make_score(total: float = 70.0, direction: str = "long") -> Score:
    return Score(
        total_score=total,
        subscores={n: 50.0 for n in ENGINE_WEIGHTS},
        band=ScoreBand.PREPARE_65_74,
        reasoning=[],
        direction=direction,  # type: ignore[arg-type]
        timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )


def make_decision(
    *,
    action: DecisionAction = DecisionAction.ENTER_LONG,
    entry_type: EntryType = EntryType.SCOUT,
    block_reason: str | None = None,
) -> Decision:
    return Decision(
        action=action,
        entry_type=entry_type,
        block_reason=block_reason,
        source_score=70.0,
        source_band=ScoreBand.PREPARE_65_74,
        source_direction="long",
        timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )


def make_bundle(
    *,
    liquidity: LiquidityEngineOutput | None = None,
    structure: MarketStructureOutput | None = None,
    atr: float | None = 0.35,
) -> FeatureSnapshotBundle:
    if liquidity is None:
        # TP zones within 2.0 USD of the structure's BOS close (2376.0):
        # 2377.0 above (1.0 USD away), 2375.0 below (1.0 USD away).
        liquidity = LiquidityEngineOutput(
            tp_targets_above=[
                LiquidityZone(
                    kind="high", price_low=2376.5, price_high=2377.5, center=2377.0, pool_count=2
                )
            ],
            tp_targets_below=[
                LiquidityZone(
                    kind="low", price_low=2374.5, price_high=2375.5, center=2375.0, pool_count=2
                )
            ],
            sl_protection_zones=[],
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
    return FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        structure=structure,
        liquidity=liquidity,
        atr=atr,
    )


# ---------------------------------------------------------------- tests


class TestPassThrough:
    """A clean setup should pass through unchanged."""

    def test_long_pass_through_qualified(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision(
            action=DecisionAction.ENTER_LONG, entry_type=EntryType.SCOUT
        )
        score = make_score(total=70.0, direction="long")
        agg = make_aggregated()
        bundle = make_bundle(atr=0.35)  # in [0.05, 2.0] USD range
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert qual.qualified is True
        assert qual.final_action == DecisionAction.ENTER_LONG
        assert qual.final_entry_type == EntryType.SCOUT
        assert qual.block_reasons == []

    def test_short_pass_through_qualified(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision(
            action=DecisionAction.ENTER_SHORT, entry_type=EntryType.SCOUT
        )
        score = make_score(total=70.0, direction="short")
        agg = make_aggregated()
        # For a short, the structure's last event should NOT be BOS_DOWN
        # (otherwise structure_against_direction blocks). Use BOS_UP
        # as the "trend" — this is what we use as a counter-trend setup
        # in real trading, but Block 3 has no reversal_setup flag so
        # any opposing structure is a hard veto. To pass we need the
        # structure to align with the direction, so let's flip it.
        from xauusd_bot.common.schemas.features import (
            MarketStructureOutput,
            StructureEvent,
            StructureEventType,
        )

        structure = MarketStructureOutput(
            swings=[],
            last_bos=StructureEvent(
                type=StructureEventType.BOS_DOWN,
                level=2375.0,
                time=datetime(2026, 4, 15, 13, 0, tzinfo=UTC),
                bar_index=100,
                close=2374.0,
                distance_atr=0.5,
            ),
            last_choch=None,
            liquidity_pools=[],
            trend="down",
            fractal_n=3,
        )
        bundle = make_bundle(structure=structure, atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert qual.qualified is True
        assert qual.final_action == DecisionAction.ENTER_SHORT


class TestPropagatesFallbackBlock:
    def test_propagates_news_blackout_reason(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision(
            action=DecisionAction.NO_TRADE,
            entry_type=None,
            block_reason="news_blackout",
        )
        score = make_score(total=50.0, direction="neutral")
        agg = make_aggregated()
        bundle = make_bundle()
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert qual.qualified is False
        assert qual.block_reasons == ["news_blackout"]


class TestTpTargetProximity:
    def test_no_clear_tp_when_zones_far(self) -> None:
        """TP zones are 50 USD away; max proximity = 1.5*0.35=0.525; floor 2.0 USD.
        50 USD > 2.0 USD → no clear TP."""

        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated()
        # TP zone center 50 USD above the structure's last close (2376.0)
        liquidity = LiquidityEngineOutput(
            tp_targets_above=[
                LiquidityZone(
                    kind="high", price_low=2425.0, price_high=2427.0, center=2426.0, pool_count=1
                )
            ],
            tp_targets_below=[],
            sl_protection_zones=[],
        )
        bundle = make_bundle(liquidity=liquidity, atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert qual.qualified is False
        assert REASON_NO_CLEAR_TP_TARGET in qual.block_reasons

    def test_clear_tp_within_atr_proximity(self) -> None:
        """TP zone center 1.0 USD above latest close (2376.0). 1.0 < 2.0 floor → OK."""

        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated()
        liquidity = LiquidityEngineOutput(
            tp_targets_above=[
                LiquidityZone(
                    kind="high", price_low=2376.5, price_high=2377.5, center=2377.0, pool_count=1
                )
            ],
            tp_targets_below=[],
            sl_protection_zones=[],
        )
        bundle = make_bundle(liquidity=liquidity, atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert qual.qualified is True

    def test_no_liquidity_data_warning(self) -> None:
        """If liquidity is None in the bundle AND atr is None → REASON_NO_LIQUIDITY_DATA
        (we have latest_close from structure but no way to measure proximity)."""

        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated(
            subscores={
                "session_liquidity": make_subscore(
                    "session_liquidity", value=50.0, weight=10.0, reasoning="no_data"
                )
            }
        )
        # atr=None + liquidity=None → enters the `elif latest_close is not None` branch
        # and adds no_liquidity_data (the data is missing, can't measure).
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
            liquidity=None,
            atr=None,
        )
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_NO_LIQUIDITY_DATA in qual.block_reasons


class TestStructureVsDirection:
    def test_long_with_bos_down_blocks(self) -> None:
        """Long proposed, last structure is BOS_DOWN → structure_against_direction."""

        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision(
            action=DecisionAction.ENTER_LONG, entry_type=EntryType.SCOUT
        )
        score = make_score(direction="long")
        agg = make_aggregated()
        structure = MarketStructureOutput(
            swings=[],
            last_bos=StructureEvent(
                type=StructureEventType.BOS_DOWN,
                level=2375.0,
                time=datetime(2026, 4, 15, 13, 0, tzinfo=UTC),
                bar_index=100,
                close=2374.0,
                distance_atr=0.5,
            ),
            last_choch=None,
            liquidity_pools=[],
            trend="down",
            fractal_n=3,
        )
        bundle = make_bundle(structure=structure, atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_STRUCTURE_AGAINST_DIRECTION in qual.block_reasons

    def test_short_with_bos_up_blocks(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision(
            action=DecisionAction.ENTER_SHORT, entry_type=EntryType.SCOUT
        )
        score = make_score(direction="short")
        agg = make_aggregated()
        # Bundle with BOS_UP — opposing the short direction
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
        bundle = make_bundle(structure=structure, atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_STRUCTURE_AGAINST_DIRECTION in qual.block_reasons


class TestVolatility:
    def test_atr_too_low_blocks(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated()
        # 0.01 USD < 0.05 USD floor → block
        bundle = make_bundle(atr=0.01)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_VOLATILITY_OUT_OF_RANGE in qual.block_reasons

    def test_atr_too_high_blocks(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated()
        # 5.0 USD > 2.0 USD ceiling → block
        bundle = make_bundle(atr=5.0)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_VOLATILITY_OUT_OF_RANGE in qual.block_reasons

    def test_atr_in_range_passes(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated()
        bundle = make_bundle(atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_VOLATILITY_OUT_OF_RANGE not in qual.block_reasons


class TestConflictFanOut:
    def test_three_conflicts_blocks(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated(
            conflicts=[
                ConflictEntry(engine_a="a", engine_b="b", description="x", severity="info"),
                ConflictEntry(engine_a="a", engine_b="b", description="x", severity="info"),
                ConflictEntry(engine_a="a", engine_b="b", description="x", severity="info"),
            ]
        )
        bundle = make_bundle(atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_ENGINE_SIGNALS_CONFLICT in qual.block_reasons

    def test_two_conflicts_passes(self) -> None:
        qe = TradeQualificationEngine(settings=make_settings())
        decision = make_decision()
        score = make_score()
        agg = make_aggregated(
            conflicts=[
                ConflictEntry(engine_a="a", engine_b="b", description="x", severity="info"),
                ConflictEntry(engine_a="a", engine_b="b", description="x", severity="info"),
            ]
        )
        bundle = make_bundle(atr=0.35)
        qual = qe.qualify(decision, score, agg, bundle, account=None)
        assert REASON_ENGINE_SIGNALS_CONFLICT not in qual.block_reasons
