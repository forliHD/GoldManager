"""Pipeline stream envelopes — the wire format between the trading services.

These are **transport envelopes** that wrap the *rich* domain schemas
(``Bar``, ``FeatureSnapshotBundle``, ``Decision`` …) so the full payload
survives the hop between services. Each
:class:`xauusd_bot.common.messaging.streams.StreamTopic` carries exactly
**one** envelope type, so a :class:`Consumer` can deserialize with a
single ``model_cls``:

================  ===================
Topic             Envelope
================  ===================
``market_ticks``  :class:`BarClosedEvent`
``features``      :class:`FeaturesEvent`
``decisions``     :class:`DecisionEvent`
``orders``        :class:`OrderEvent`
``journal``       :class:`JournalEvent`
================  ===================

Relationship to :mod:`xauusd_bot.common.schemas.events`
-------------------------------------------------------
That module holds an older, *lightweight* domain-event model (a flat
``score``-only ``FeatureSnapshot``, a display-oriented ``Decision``)
intended for human/dashboard display. The envelopes here instead carry
the **complete** engine outputs because the next service in the chain
needs to reconstruct the exact object the previous service produced
(e.g. the decision-engine needs the whole ``FeatureSnapshotBundle``,
not a single number). Keeping them in a separate module avoids a name
clash with the tested lightweight models and keeps the two concerns —
*transport between services* vs *display* — cleanly apart.

Versioning
----------
Every envelope carries ``schema_version``; consumers drop events whose
version they do not understand at the boundary (see
:mod:`xauusd_bot.common.messaging.streams`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xauusd_bot.common.schemas.decision import (
    Decision,
    Score,
    TradeQualification,
)
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.common.schemas.journal import (
    DecisionLogRecord,
    FeatureSnapshotRecord,
    OrderRecord,
    TradeCloseUpdate,
    TradeRecord,
)
from xauusd_bot.connectors.schemas import Bar

# Bump when an envelope shape changes incompatibly. Consumers compare
# against this and drop events from an unknown (future) version.
ENVELOPE_SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(tz=UTC)


class _Envelope(BaseModel):
    """Common fields shared by every pipeline envelope."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=ENVELOPE_SCHEMA_VERSION)
    produced_at: datetime = Field(default_factory=_now)
    symbol: str


class BarClosedEvent(_Envelope):
    """A freshly closed OHLC bar, emitted by the data-collector."""

    kind: Literal["bar_closed"] = "bar_closed"
    bar: Bar


class FeaturesEvent(_Envelope):
    """The full feature bundle for one bar, emitted by the feature-engine."""

    kind: Literal["features"] = "features"
    bundle: FeatureSnapshotBundle = Field(
        description=(
            "Feature bundle for the bar. Publishers compact it first "
            "(see xauusd_bot.common.messaging.compact.compact_bundle) — the "
            "fvg.zones / structure history tail is dropped to keep the wire "
            "payload small; everything downstream reads survives."
        ),
    )
    ref_price: Decimal | None = Field(
        default=None,
        description="Close of the bar these features were computed on — the execution-engine's market entry reference.",
    )


class DecisionEvent(_Envelope):
    """The decision + score (+ qualification) for one bar.

    ``qualification`` is ``None`` when the decision stack produced a
    ``no_trade`` before qualification ran, so downstream consumers can
    still observe every bar's score for monitoring.
    """

    kind: Literal["decision"] = "decision"
    decision: Decision
    score: Score
    qualification: TradeQualification | None = None
    bundle: FeatureSnapshotBundle = Field(
        description=(
            "The feature bundle the decision was made on — the execution-engine "
            "needs it for ATR-based SL/TP placement. Compacted before publish "
            "(see compact_bundle): execution reads the latest swing high/low and "
            "the open/top FVG zones, all of which survive compaction."
        ),
    )
    ref_price: Decimal | None = Field(
        default=None,
        description="Reference price (bar close) carried from the features event for market entry.",
    )


class OrderEvent(_Envelope):
    """An order the execution-engine submitted (filled, pending, or rejected)."""

    kind: Literal["order"] = "order"
    order: OrderRecord


class JournalEvent(_Envelope):
    """A journal write request, emitted by the execution-engine.

    A single consumer (the journal-writer) persists these, so the event
    is a small tagged union rather than three separate topics:
    ``entry_type`` selects which payload is populated.
    """

    kind: Literal["journal"] = "journal"
    entry_type: Literal["trade", "order", "feature_snapshot", "trade_close", "decision"]
    trade: TradeRecord | None = None
    order: OrderRecord | None = None
    snapshot: FeatureSnapshotRecord | None = None
    trade_close: TradeCloseUpdate | None = None
    decision: DecisionLogRecord | None = None


# Topic → envelope map, so services and tests resolve the model class for
# a topic in one place instead of hard-coding it at every call site.
def envelope_for_topic() -> dict[str, type[_Envelope]]:
    """Return the ``{topic_value: envelope_cls}`` mapping.

    Imported lazily as a function (not a module-level dict) so this
    module does not import :mod:`streams` and risk an import cycle.
    """

    from xauusd_bot.common.messaging.streams import StreamTopic

    return {
        StreamTopic.MARKET_TICKS.value: BarClosedEvent,
        # Forming-bar animation channel — same envelope as closed bars.
        StreamTopic.MARKET_LIVE.value: BarClosedEvent,
        # Chart-only historical bars — same envelope, never consumed by services.
        StreamTopic.CHART_HISTORY.value: BarClosedEvent,
        StreamTopic.FEATURES.value: FeaturesEvent,
        StreamTopic.DECISIONS.value: DecisionEvent,
        StreamTopic.ORDERS.value: OrderEvent,
        StreamTopic.JOURNAL.value: JournalEvent,
    }


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "BarClosedEvent",
    "DecisionEvent",
    "FeaturesEvent",
    "JournalEvent",
    "OrderEvent",
    "envelope_for_topic",
]
