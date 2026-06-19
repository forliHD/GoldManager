"""Common / cross-cutting Pydantic schemas.

These objects ride on Redis Streams between the five services (data-collector,
feature-engine, decision-engine, execution-engine, review). They are
**separate** from the connector-layer wire types in
:mod:`xauusd_bot.connectors.schemas` because the latter are bound to the
broker abstraction (Bid/Ask spread, etc.) while the former describe
*decisions, features, and journal entries* — domain objects, not transport.

Versioning
----------
Every event carries ``schema_version``. Consumers should reject messages
with an unknown version rather than guess.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump on breaking changes to the event schema.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------- enums


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NO_TRADE = "no_trade"


class DecisionAction(str, Enum):
    ENTER = "enter"
    PREPARE = "prepare"
    OBSERVE = "observe"
    NO_TRADE = "no_trade"


# ----------------------------------------------------------------- base


class _BaseEvent(BaseModel):
    """Common base: schema version, source service, timestamp."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION)
    source: str = Field(description="Origin service, e.g. 'data-collector'.")
    ts: datetime = Field(description="UTC timestamp of emission.")
    correlation_id: str | None = Field(
        default=None, description="Setup- or trade-id that ties this event to others."
    )


# --------------------------------------------------------------- market


class MarketData(_BaseEvent):
    """Raw market data event (used for tick forwarding, sparse)."""

    kind: Literal["market_data"] = "market_data"
    symbol: str
    last_bid: Decimal
    last_ask: Decimal
    last: Decimal | None = None
    volume: int = 0


class BarEvent(_BaseEvent):
    """A new OHLC bar (any timeframe)."""

    kind: Literal["bar"] = "bar"
    symbol: str
    timeframe: str
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int
    real_volume: int | None = None
    spread_points: float | None = None


# --------------------------------------------------------------- features


class FeatureSnapshot(_BaseEvent):
    """Aggregated feature snapshot for a single bar close."""

    kind: Literal["feature_snapshot"] = "feature_snapshot"
    symbol: str
    bar_time: datetime
    score: float = Field(ge=0, le=100, description="0..100 composite score.")
    components: dict[str, float] = Field(
        default_factory=dict, description="Per-feature sub-scores (e.g. vwap=72, htf_vp=65)."
    )
    context: dict[str, Any] = Field(
        default_factory=dict, description="Free-form context (levels, zones, news flags)."
    )


# --------------------------------------------------------------- decision


class Decision(_BaseEvent):
    """Decision engine output."""

    kind: Literal["decision"] = "decision"
    symbol: str
    action: DecisionAction
    side: Side
    entry_zone_min: Decimal | None = None
    entry_zone_max: Decimal | None = None
    invalidations: list[Decimal] = Field(default_factory=list)
    management: dict[str, Any] = Field(default_factory=dict)
    comment: str = ""
    score: float | None = Field(default=None, ge=0, le=100)
    source_ai: bool = Field(default=False, description="True if AI layer (not rule-based) authored this.")


# --------------------------------------------------------------- orders


class OrderEvent(_BaseEvent):
    """Execution-engine order event (sent / filled / cancelled / rejected)."""

    kind: Literal["order"] = "order"
    symbol: str
    status: Literal["sent", "filled", "partial", "cancelled", "rejected"]
    order_id: str | None = None
    client_order_id: str | None = None
    side: Side | None = None
    volume: Decimal | None = None
    price: Decimal | None = None
    error: str | None = None


# --------------------------------------------------------------- journal


class JournalEntry(_BaseEvent):
    """Persisted journal record (TimescaleDB)."""

    kind: Literal["journal"] = "journal"
    symbol: str
    event_type: str = Field(description="e.g. 'order_filled', 'decision_made', 'risk_block'.")
    payload: dict[str, Any] = Field(default_factory=dict)
