"""Tests for the journal Pydantic schemas (Block 5a)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
)
from xauusd_bot.common.schemas.journal import (
    DiscrepancyResolutionTag,
    ExitReasonTag,
    FeatureSnapshotRecord,
    LLMFallbackDiscrepancy,
    OrderRecord,
    OrderStatusTag,
    TradeRecord,
)
from xauusd_bot.connectors.schemas import OrderSide, OrderType


# ----------------------------------------------------------------- factories


def _ts(year: int = 2026, month: int = 6, day: int = 15, hour: int = 13, minute: int = 30) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_trade(**overrides) -> TradeRecord:
    base = dict(
        timestamp_open=_ts(),
        side="long",
        entry_price=Decimal("2370.00"),
        stop_loss=Decimal("2365.00"),
        take_profits=[Decimal("2375.00"), Decimal("2380.00"), Decimal("2385.00")],
        volume_lots=Decimal("0.10"),
        risk_amount=Decimal("50"),
        setup_id=uuid4(),
        score=87.5,
        subscores={"h1_zone": 80, "m5_zone": 75, "news": 90},
        band=ScoreBand.FULL_85_PLUS,
        entry_type=EntryType.FULL,
        fill_price=Decimal("2370.05"),
        session="london",
        atr_at_entry=0.35,
        structure_at_entry="up",
    )
    base.update(overrides)
    return TradeRecord(**base)


def make_snapshot(**overrides) -> FeatureSnapshotRecord:
    base = dict(
        timestamp=_ts(),
        bar_time=_ts(),
        has_data=True,
        features={"h1_zone": 80, "vwap": 2372.5, "in_blackout": False},
    )
    base.update(overrides)
    return FeatureSnapshotRecord(**base)


def make_order(**overrides) -> OrderRecord:
    base = dict(
        timestamp=_ts(),
        trade_id=uuid4(),
        client_order_id="cli-001",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        volume=Decimal("0.10"),
        fill_price=Decimal("2370.05"),
        status=OrderStatusTag.FILLED,
    )
    base.update(overrides)
    return OrderRecord(**base)


def make_discrepancy(**overrides) -> LLMFallbackDiscrepancy:
    base = dict(
        timestamp=_ts(),
        decision_id=uuid4(),
        rule_action=DecisionAction.ENTER_LONG,
        rule_score=87.5,
        rule_band=ScoreBand.FULL_85_PLUS,
        rule_block_reasons=[],
        llm_action=DecisionAction.ENTER_LONG,
        llm_score=82.0,
        llm_reasoning="h1 zone aligned, vwap reclaim",
        final_action=DecisionAction.ENTER_LONG,
        final_source="rule",
        resolution=DiscrepancyResolutionTag.AGREEMENT,
    )
    base.update(overrides)
    return LLMFallbackDiscrepancy(**base)


# ----------------------------------------------------------------- TradeRecord


def test_trade_record_creates_with_defaults() -> None:
    t = make_trade()
    assert isinstance(t.id, UUID)
    assert t.timestamp_open == _ts()
    assert t.timestamp_close is None
    assert t.exit_price is None
    assert t.pnl_realized is None
    assert t.r_multiple is None
    assert t.exit_reason is None
    assert t.engine_source == "rule"
    assert t.strategy_version == "block5a-v1"
    assert t.tags == {}
    assert t.order_ids == []
    # Decimal precision
    assert isinstance(t.entry_price, Decimal)
    assert isinstance(t.volume_lots, Decimal)
    assert isinstance(t.risk_amount, Decimal)


def test_trade_record_blocks_naive_datetime() -> None:
    with pytest.raises(ValidationError) as exc:
        make_trade(timestamp_open=datetime(2026, 6, 15, 13, 30))  # noqa: DTZ001 - intentional
    assert "timezone-aware" in str(exc.value)


def test_trade_record_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError) as exc:
        make_trade(weird_field="x")
    assert "weird_field" in str(exc.value)


def test_trade_record_tp_list_capped_at_four() -> None:
    with pytest.raises(ValidationError):
        make_trade(
            take_profits=[Decimal("2375"), Decimal("2380"), Decimal("2385"), Decimal("2390"), Decimal("2395")]
        )


def test_trade_record_side_must_be_long_or_short() -> None:
    with pytest.raises(ValidationError):
        make_trade(side="neutral")  # type: ignore[arg-type]


def test_trade_record_r_multiple_can_be_set() -> None:
    t = make_trade(pnl_realized=Decimal("50"), r_multiple=1.0)
    assert t.r_multiple == 1.0
    assert t.pnl_realized == Decimal("50")


def test_trade_record_take_profits_defaults_to_empty_list() -> None:
    t = make_trade(take_profits=[])
    assert t.take_profits == []


def test_trade_record_engine_source_accepts_ai() -> None:
    t = make_trade(engine_source="ai")
    assert t.engine_source == "ai"


# ----------------------------------------------------------------- FeatureSnapshotRecord


def test_snapshot_record_roundtrip() -> None:
    s = make_snapshot()
    assert s.has_data is True
    assert s.features["h1_zone"] == 80
    assert isinstance(s.id, UUID)
    assert s.source_version == "block2-v1"
    assert s.symbol == "XAUUSD"
    assert s.timeframe == "m1"


def test_snapshot_record_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        make_snapshot(unknown="x")


def test_snapshot_record_handles_dict_features() -> None:
    features = {
        "engines": {"session": "london", "vwap": 2372.5},
        "conflicts": [{"engine_a": "x", "engine_b": "y", "description": "z"}],
    }
    s = make_snapshot(features=features)
    assert s.features == features


def test_snapshot_record_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        make_snapshot(timestamp=datetime(2026, 6, 15, 13, 30))  # noqa: DTZ001


def test_snapshot_record_timeframe_choices() -> None:
    s = make_snapshot(timeframe="h1")
    assert s.timeframe == "h1"
    with pytest.raises(ValidationError):
        make_snapshot(timeframe="m3")  # type: ignore[arg-type]


def test_snapshot_record_engine_name_optional() -> None:
    s = make_snapshot(engine_name="session")
    assert s.engine_name == "session"
    s2 = make_snapshot(engine_name=None)
    assert s2.engine_name is None


# ----------------------------------------------------------------- LLMFallbackDiscrepancy


def test_discrepancy_roundtrip_agreement() -> None:
    d = make_discrepancy()
    assert d.resolution == DiscrepancyResolutionTag.AGREEMENT
    assert d.rule_action == DecisionAction.ENTER_LONG
    assert d.llm_action == DecisionAction.ENTER_LONG
    assert d.final_source == "rule"


def test_discrepancy_rule_vetoed() -> None:
    d = make_discrepancy(
        rule_action=DecisionAction.NO_TRADE,
        rule_block_reasons=["news_blackout"],
        llm_action=DecisionAction.ENTER_LONG,
        final_action=DecisionAction.NO_TRADE,
        final_source="rule",
        resolution=DiscrepancyResolutionTag.RULE_VETOED,
    )
    assert d.resolution == DiscrepancyResolutionTag.RULE_VETOED
    assert d.rule_block_reasons == ["news_blackout"]


def test_discrepancy_llm_vetoed() -> None:
    d = make_discrepancy(
        rule_action=DecisionAction.ENTER_LONG,
        llm_action=DecisionAction.NO_TRADE,
        llm_reasoning="I see a head-fake; skip.",
        final_action=DecisionAction.NO_TRADE,
        final_source="llm",
        resolution=DiscrepancyResolutionTag.LLM_VETOED,
    )
    assert d.final_source == "llm"
    assert d.llm_reasoning is not None


def test_discrepancy_score_bounded() -> None:
    with pytest.raises(ValidationError):
        make_discrepancy(rule_score=120.0)
    with pytest.raises(ValidationError):
        make_discrepancy(rule_score=-5.0)


def test_discrepancy_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        make_discrepancy(unknown="x")


# ----------------------------------------------------------------- OrderRecord


def test_order_record_roundtrip() -> None:
    o = make_order()
    assert o.status == OrderStatusTag.FILLED
    assert o.fill_price == Decimal("2370.05")
    assert isinstance(o.id, UUID)
    assert o.strategy_version == "block5a-v1"


def test_order_record_rejected_status() -> None:
    o = make_order(
        status=OrderStatusTag.REJECTED,
        fill_price=None,
        error="SAFETY_BLOCK",
    )
    assert o.error == "SAFETY_BLOCK"
    assert o.fill_price is None


def test_order_record_volume_must_be_ge_0() -> None:
    with pytest.raises(ValidationError):
        make_order(volume=Decimal("-1"))


def test_order_record_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        make_order(unknown_field="x")


def test_order_record_slippage_pips_optional() -> None:
    o = make_order(slippage_pips=0.5, slippage_bps=2.0)
    assert o.slippage_pips == 0.5
    assert o.slippage_bps == 2.0


# ----------------------------------------------------------------- PIT anchor (schema-level)


def test_feature_snapshot_id_is_optional_uuid_field() -> None:
    """TradeRecord.feature_snapshot_id is an optional UUID FK."""

    t = make_trade(feature_snapshot_id=uuid4())
    assert isinstance(t.feature_snapshot_id, UUID)
    t2 = make_trade(feature_snapshot_id=None)
    assert t2.feature_snapshot_id is None
