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
from xauusd_bot.common.schemas.features import (
    CandleMomentumOutput,
    CandleMomentumPerBar,
    FeatureSnapshotBundle,
    FVGOutput,
    FVGStatus,
    FVGType,
    FVGZone,
    LiquidityEngineOutput,
    LiquidityPool,
    LiquidityZone,
    MarketStructureOutput,
    NewsContextOutput,
    NewsEvent,
    NewsImpact,
    SessionEngineOutput,
    SessionName,
    StructureEvent,
    StructureEventType,
    SwingPoint,
    TripleVWAPOutput,
    ValueAreaStatus,
    VolumeProfileName,
    VolumeProfileOutput,
    VolumeProfileState,
    VolumeRangeOutput,
    VWAPLevel,
    VWAPLevelOutput,
)

__all__ = [
    "BarEvent",
    "CandleMomentumOutput",
    "CandleMomentumPerBar",
    "Decision",
    "DecisionAction",
    "FVGOutput",
    "FVGStatus",
    "FVGType",
    "FVGZone",
    "FeatureSnapshot",
    "FeatureSnapshotBundle",
    "JournalEntry",
    "LiquidityEngineOutput",
    "LiquidityPool",
    "LiquidityZone",
    "MarketData",
    "MarketStructureOutput",
    "NewsContextOutput",
    "NewsEvent",
    "NewsImpact",
    "OrderEvent",
    "SCHEMA_VERSION",
    "SessionEngineOutput",
    "SessionName",
    "Side",
    "StructureEvent",
    "StructureEventType",
    "SwingPoint",
    "TripleVWAPOutput",
    "ValueAreaStatus",
    "VolumeProfileName",
    "VolumeProfileOutput",
    "VolumeProfileState",
    "VolumeRangeOutput",
    "VWAPLevel",
    "VWAPLevelOutput",
]
