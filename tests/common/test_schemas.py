"""Tests for the common Pydantic event schemas — round-trip and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from xauusd_bot.common.schemas import (
    SCHEMA_VERSION,
    BarEvent,
    Decision,
    DecisionAction,
    FeatureSnapshot,
    JournalEntry,
    MarketData,
    OrderEvent,
    Side,
)

# ---------------------------------------------------------------- MarketData


def test_market_data_minimal_example() -> None:
    """The minimal valid MarketData event."""

    md = MarketData(
        source="data-collector",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        last_bid=Decimal("2000.00"),
        last_ask=Decimal("2000.50"),
    )
    assert md.schema_version == SCHEMA_VERSION
    assert md.kind == "market_data"
    assert md.symbol == "XAUUSD"
    assert md.last_bid == Decimal("2000.00")
    assert md.last_ask == Decimal("2000.50")
    assert md.last is None
    assert md.volume == 0


def test_market_data_maximal_example() -> None:
    """The maximal valid MarketData event (all fields populated)."""

    md = MarketData(
        source="data-collector",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        correlation_id="corr-123",
        symbol="XAUUSD",
        last_bid=Decimal("2000.00"),
        last_ask=Decimal("2000.50"),
        last=Decimal("2000.25"),
        volume=10,
    )
    assert md.last == Decimal("2000.25")
    assert md.volume == 10
    assert md.correlation_id == "corr-123"


def test_market_data_garbage_raises() -> None:
    """Missing required fields raise ValidationError."""

    with pytest.raises(ValidationError):
        MarketData(source="x")  # type: ignore[call-arg]


def test_market_data_extra_field_rejected() -> None:
    """Unknown fields are rejected (extra='forbid')."""

    with pytest.raises(ValidationError):
        MarketData(
            source="data-collector",
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            symbol="XAUUSD",
            last_bid=Decimal("2000"),
            last_ask=Decimal("2000.5"),
            unknown_field="surprise",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------- BarEvent


def test_bar_event_minimal_example() -> None:
    bar = BarEvent(
        source="data-collector",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000.5"),
        tick_volume=10,
    )
    assert bar.kind == "bar"
    assert bar.real_volume is None
    assert bar.spread_points is None


def test_bar_event_maximal_example() -> None:
    bar = BarEvent(
        source="data-collector",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        correlation_id="corr-x",
        symbol="XAUUSD",
        timeframe="M5",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("2000"),
        high=Decimal("2002"),
        low=Decimal("1999"),
        close=Decimal("2001.5"),
        tick_volume=100,
        real_volume=500,
        spread_points=35.0,
    )
    assert bar.real_volume == 500
    assert bar.spread_points == 35.0


def test_bar_event_round_trip() -> None:
    """BarEvent.model_dump + model_validate round-trips losslessly."""

    bar = BarEvent(
        source="data-collector",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000.5"),
        tick_volume=10,
    )
    j = bar.model_dump(mode="json")
    parsed = BarEvent.model_validate(j)
    assert parsed == bar


def test_bar_event_garbage_raises() -> None:
    with pytest.raises(ValidationError):
        BarEvent(symbol="XAUUSD")  # type: ignore[call-arg]


# ---------------------------------------------------------------- FeatureSnapshot


def test_feature_snapshot_minimal() -> None:
    fs = FeatureSnapshot(
        source="feature-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        bar_time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        score=50.0,
    )
    assert fs.score == 50.0
    assert fs.components == {}
    assert fs.context == {}


def test_feature_snapshot_maximal() -> None:
    fs = FeatureSnapshot(
        source="feature-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        correlation_id="c1",
        symbol="XAUUSD",
        bar_time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        score=72.5,
        components={"vwap": 80.0, "fvg": 65.0, "session": 50.0},
        context={"levels": [2000.0, 2010.0], "news": "FOMC"},
    )
    assert fs.components["vwap"] == 80.0
    assert fs.context["news"] == "FOMC"


def test_feature_snapshot_score_bounds_low() -> None:
    """Score must be in [0, 100]."""

    with pytest.raises(ValidationError):
        FeatureSnapshot(
            source="feature-engine",
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            symbol="XAUUSD",
            bar_time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            score=-0.1,
        )


def test_feature_snapshot_score_bounds_high() -> None:
    with pytest.raises(ValidationError):
        FeatureSnapshot(
            source="feature-engine",
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            symbol="XAUUSD",
            bar_time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            score=100.1,
        )


# ---------------------------------------------------------------- Decision


def test_decision_minimal() -> None:
    d = Decision(
        source="decision-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        action=DecisionAction.NO_TRADE,
        side=Side.NO_TRADE,
    )
    assert d.kind == "decision"
    assert d.action == DecisionAction.NO_TRADE
    assert d.side == Side.NO_TRADE
    assert d.source_ai is False


def test_decision_maximal() -> None:
    d = Decision(
        source="decision-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        correlation_id="setup-1",
        symbol="XAUUSD",
        action=DecisionAction.ENTER,
        side=Side.BUY,
        entry_zone_min=Decimal("2000"),
        entry_zone_max=Decimal("2005"),
        invalidations=[Decimal("1990")],
        management={"tp": "2010", "sl": "1990"},
        comment="FVG + VWAP bounce",
        score=75.0,
        source_ai=True,
    )
    assert d.entry_zone_min == Decimal("2000")
    assert d.source_ai is True
    assert d.invalidations == [Decimal("1990")]


def test_decision_garbage_action_raises() -> None:
    with pytest.raises(ValidationError):
        Decision(
            source="decision-engine",
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            symbol="XAUUSD",
            action="YOLO",  # not in DecisionAction
            side=Side.BUY,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------- OrderEvent


def test_order_event_minimal() -> None:
    e = OrderEvent(
        source="execution-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        status="sent",
    )
    assert e.kind == "order"
    assert e.status == "sent"


def test_order_event_filled() -> None:
    e = OrderEvent(
        source="execution-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        status="filled",
        order_id="ord-1",
        client_order_id="client-1",
        side=Side.BUY,
        volume=Decimal("0.10"),
        price=Decimal("2000.50"),
    )
    assert e.status == "filled"
    assert e.price == Decimal("2000.50")


def test_order_event_unknown_status_raises() -> None:
    with pytest.raises(ValidationError):
        OrderEvent(
            source="execution-engine",
            ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            symbol="XAUUSD",
            status="exploded",  # not in Literal
        )


# ---------------------------------------------------------------- JournalEntry


def test_journal_entry_minimal() -> None:
    je = JournalEntry(
        source="execution-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        event_type="order_filled",
    )
    assert je.kind == "journal"
    assert je.payload == {}


def test_journal_entry_with_payload() -> None:
    je = JournalEntry(
        source="execution-engine",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        event_type="decision_made",
        payload={"score": 75, "side": "buy"},
    )
    assert je.payload["score"] == 75


# ---------------------------------------------------------------- SCHEMA_VERSION invariant


def test_schema_version_default() -> None:
    """Every event without an explicit schema_version defaults to SCHEMA_VERSION."""

    d = Decision(
        source="decision-engine",
        ts=datetime.now(tz=UTC),
        symbol="XAUUSD",
        action=DecisionAction.NO_TRADE,
        side=Side.NO_TRADE,
    )
    assert d.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 1
