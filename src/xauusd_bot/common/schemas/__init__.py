"""Domain Pydantic schemas that flow through Redis Streams.

These are higher-level objects (FeatureSnapshot, Decision, JournalEntry)
distinct from the connector-layer wire types in
:mod:`xauusd_bot.connectors.schemas`. The decision engine emits
:class:`Decision`; the feature engine emits :class:`FeatureSnapshot`;
the journal persists :class:`JournalEntry`. Each carries a stable
``schema_version`` so consumers can reject incompatible payloads.
"""

from xauusd_bot.common.schemas.events import (
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

__all__ = [
    "BarEvent",
    "Decision",
    "DecisionAction",
    "FeatureSnapshot",
    "JournalEntry",
    "MarketData",
    "OrderEvent",
    "SCHEMA_VERSION",
    "Side",
]
