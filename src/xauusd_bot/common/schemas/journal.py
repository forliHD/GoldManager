"""Pydantic schemas for the Journal / Persistence layer (Block 5a).

This module defines the *persisted* record shapes — they are the
contract that Block 5a (this code) writes and Block 5b/5c
(BacktestEngine + Review) reads back. They are deliberately
**persistence-shaped**, not the same as the in-memory Block-3 / Block-4
Pydantic types. The journal is the *flattened* view of a trade /
feature-snapshot / order / discrepancy:

* Money / size fields use ``Decimal`` — no float drift in money.
* Prices use ``Decimal`` — same reason.
* Time fields are timezone-aware UTC ``datetime`` objects.
* All schemas are ``extra='forbid'`` — a missing/extra field is a bug.
* Each schema carries a ``timestamp`` for log/journal correlation.

Why a separate journal schema?
------------------------------
The decision/execution schemas are *runtime* types — they change
shape as the engine grows (e.g. we might add a new field to
:class:`TradeQualification`). The journal schema is the *write-only*
contract that is *backwards-compatible* — once a record is written,
its shape must remain valid for the read-API forever. This way a
record written by Block 4 today can still be replayed by Block 5b
six months from now.

Invariant I-4: this module is **read-write persistence only**. It
NEVER computes position size, SL, TP, R-multiple, equity curve,
score, etc. from raw inputs. All numeric fields on the records are
*copies* of values the upstream layer (Block 3 / Block 4) computed
and passed in.

Invariant I-1: this module does NOT import MetaTrader5. It operates
on Pydantic data and the store backend (in-memory or asyncpg).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
)
from xauusd_bot.connectors.schemas import (
    OrderSide,
    OrderType,
)

# ----------------------------------------------------------------- enums


class SessionTag(str, Enum):
    """Session tag at the moment of the trade (Block-2 SessionEngine output).

    Duplicated from the feature schema so the journal does not need to
    import the features module (Block 2 may add new sessions in the
    future and we want the journal to remain stable).
    """

    ASIA = "asia"
    LONDON = "london"
    NY = "ny"
    OVERLAP = "overlap"
    CLOSED = "closed"


# Use a Literal-typed alias so callers can pass strings too. We keep
# the enum for stability of the string values in the journal.
SessionLiteral = Literal["asia", "london", "ny", "overlap", "closed"]


class ExitReasonTag(str, Enum):
    """Stable string for the *why* a trade closed.

    Mirrors :class:`xauusd_bot.common.schemas.execution.ExitReason`
    but is kept as a separate enum so the journal can outlive
    internal enum renames. New values may be appended; never change
    or remove an existing one.
    """

    SL_HIT = "sl_hit"
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    TP3_HIT = "tp3_hit"
    TRAILED = "trail_stop"
    EMERGENCY = "emergency_flatten"
    MANUAL = "manual"
    INVALIDATION = "invalidation"


class TradeCloseUpdate(BaseModel):
    """Close-time finalisation for a previously-opened journal trade.

    Emitted by the execution-engine when a tracked position disappears from
    the broker (SL/TP/manual close). The journal-writer resolves ``order_id``
    (the broker ticket) to the open trade record and applies these fields via
    ``update_trade``. ``pnl_realized`` / ``r_multiple`` are optional because a
    connector without deal-history can only report the exit price.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(description="Broker ticket; resolves to the open trade via order_ids.")
    timestamp_close: datetime
    exit_price: Decimal
    pnl_realized: Decimal | None = None
    r_multiple: float | None = None
    exit_reason: ExitReasonTag


class DecisionLogRecord(BaseModel):
    """Slim, persistence-shaped log of one decision (no feature bundle).

    Journaled by the decision-engine for the dashboard's decision-history tab —
    enough to audit "did it see a setup, and why wasn't it taken" without the
    heavy bundle. Immutable once written.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    ts: datetime = Field(description="Decision time (broker bar time, as displayed on the feed).")
    written_at: datetime = Field(description="Wall-clock UTC when journaled.")
    symbol: str
    action: str = Field(description="no_trade / enter / ...")
    direction: str | None = None
    score: float | None = None
    band: str | None = None
    subscores: dict[str, float] = Field(default_factory=dict)
    block_reason: str | None = None
    qualified: bool = False
    entry_type: str | None = None
    source_ai: bool = False
    ref_price: float | None = None
    # Why the AI did / didn't run: ran | ai_off | score_low | news_blackout | llm_error.
    ai_status: str | None = None
    # LLM rationale for this decision (only when the AI layer actually ran).
    ai_reasoning: str | None = None
    ai_confidence: float | None = None
    ai_invalidations: list[str] = Field(default_factory=list)


class OrderStatusTag(str, Enum):
    """Stable order status strings for the journal."""

    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class DiscrepancyResolutionTag(str, Enum):
    """Stable tags for LLM ↔ RuleBasedFallback discrepancy.

    * ``agreement`` — both said the same thing.
    * ``rule_vetoed`` — fallback blocked, LLM wanted to enter.
      Rule wins (I-4: RuleBasedFallback is safety-authoritative).
    * ``llm_vetoed`` — LLM blocked / declined, fallback wanted to
      enter. LLM veto allowed (LLM may be more conservative).
    * ``rule_relaxed`` — fallback allowed, LLM said no_trade with
      higher confidence. LLM wins here (rare — LLM is supplementary).
    """

    AGREEMENT = "agreement"
    RULE_VETOED = "rule_vetoed"
    LLM_VETOED = "llm_vetoed"
    RULE_RELAXED = "rule_relaxed"


# ----------------------------------------------------------------- helpers


def _ensure_utc(v: datetime) -> datetime:
    """Strip-and-add-UTC validator (mirrors :class:`Bar` convention)."""

    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC).")
    return v.astimezone(tz=timezone.utc)


# ----------------------------------------------------------------- TradeRecord


class TradeRecord(BaseModel):
    """A persisted trade.

    A trade is born at *open* (``timestamp_open``). It may still be
    open (in which case ``timestamp_close``, ``exit_price``,
    ``pnl_realized``, ``r_multiple``, ``exit_reason`` are all None).
    Updates land via :meth:`xauusd_bot.journal.store.JournalStore.update_trade`.

    Field ownership
    ---------------
    * ``id`` — assigned on creation (UUID).
    * ``setup_id`` — copied from :attr:`TradeQualification.qualification_id`.
    * ``feature_snapshot_id`` — FK to the
      :class:`FeatureSnapshotRecord` that was on hand when the decision
      was made (PIT-anchored). The journal does NOT back-fill this from
      a later snapshot.
    * ``score`` / ``subscores`` / ``band`` / ``entry_type`` / ``block_reasons``
      — copied verbatim from the Block-3 decision/qualification stack.
    * ``stop_loss`` / ``take_profits`` / ``volume_lots`` — copied
      verbatim from the Block-4 order envelope (NOT recomputed).
    * ``slippage_pips`` / ``slippage_bps`` — pre-computed by the
      execution layer; the journal stores the value as-is.

    The journal NEVER derives ``r_multiple`` from ``pnl_realized`` and
    ``risk_amount`` at read time — it is stored as a flat field at
    close time so the read-API can stay pure and the value is
    immutable once the trade is closed.
    """

    model_config = ConfigDict(extra="forbid")

    # --- identity
    id: UUID = Field(default_factory=uuid4, description="Stable trade UUID.")
    timestamp_open: datetime = Field(description="Open time (UTC).")
    timestamp_close: datetime | None = Field(
        default=None, description="Close time (UTC). None while the trade is still open."
    )
    symbol: str = Field(default="XAUUSD")

    # --- direction / entry / exit
    side: Literal["long", "short"] = Field(description="Trade side.")
    entry_price: Decimal = Field(description="Fill price at open.")
    exit_price: Decimal | None = Field(default=None, description="Fill price at close (None if open).")
    stop_loss: Decimal = Field(description="SL price set by the executor.")
    take_profits: list[Decimal] = Field(
        default_factory=list,
        description="TP price ladder, in order [tp1, tp2, tp3]. May be empty if not armed.",
    )
    volume_lots: Decimal = Field(ge=0, description="Filled volume in lots.")
    risk_amount: Decimal = Field(ge=0, description="USD at risk (= |entry - sl| × lots × contract_size).")
    pnl_realized: Decimal | None = Field(
        default=None, description="Realized PnL in USD at close (None if open)."
    )
    pnl_unrealized: Decimal | None = Field(
        default=None, description="Mark-to-market PnL (None if not tracked)."
    )
    r_multiple: float | None = Field(
        default=None,
        description="Pre-computed pnl_realized / risk_amount. Stored at close time.",
    )

    # --- decision context (Block 3 outputs)
    setup_id: UUID = Field(description="TradeQualification.qualification_id this trade came from.")
    strategy_version: str = Field(
        default="block5a-v1", description="Version of the executor + decision stack."
    )
    engine_source: Literal["rule", "ai"] = Field(
        default="rule", description="Which decision source produced this trade (Block 3 rule vs Block 6 LLM)."
    )
    score: float = Field(ge=0, le=100, description="Total score at decision time.")
    subscores: dict[str, float] = Field(
        default_factory=dict, description="Flat per-engine 0-100 scores at decision time."
    )
    band: ScoreBand = Field(description="Score band at decision time.")
    entry_type: EntryType = Field(description="Sizing intent (scout / reduced / full).")
    block_reasons: list[str] = Field(
        default_factory=list, description="Empty for executed trades; populated only for diagnostic records."
    )

    # --- PIT anchor
    feature_snapshot_id: UUID | None = Field(
        default=None,
        description=(
            "FK to FeatureSnapshotRecord with timestamp <= timestamp_open. "
            "PIT-anchored — the journal does NOT enrich this from later snapshots."
        ),
    )

    # --- execution details
    order_ids: list[str] = Field(
        default_factory=list,
        description="Broker order_ids attached to this trade (entry + partial closes + final close).",
    )
    fill_price: Decimal = Field(description="Average fill price of the entry order.")
    slippage_pips: float | None = Field(
        default=None, description="Entry slippage in pips (XAUUSD: 1 pip = 10 points)."
    )
    slippage_bps: float | None = Field(
        default=None, description="Entry slippage in basis points of price."
    )

    # --- context at entry
    session: SessionLiteral = Field(
        default="closed", description="Trading session at open time (asia / london / ny / overlap / closed)."
    )
    atr_at_entry: float | None = Field(
        default=None, ge=0, description="ATR(M1, 14) at open time, in USD."
    )
    structure_at_entry: Literal["up", "down", "range"] = Field(
        default="range", description="Market-structure trend at open time."
    )
    exit_reason: ExitReasonTag | None = Field(
        default=None, description="Why the trade closed. None if still open."
    )

    # --- free-form / extension
    tags: dict[str, str] = Field(
        default_factory=dict, description="Free-form key-value tags (e.g. 'force_trade': 'true')."
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
        description="Wall-clock time the record was written (UTC).",
    )

    @field_validator("timestamp_open", "timestamp_close", "created_at")
    @classmethod
    def _validate_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return _ensure_utc(v)

    @field_validator("take_profits")
    @classmethod
    def _validate_tp_list(cls, v: list[Decimal]) -> list[Decimal]:
        # TP list is intentionally permissive: empty is allowed (e.g.
        # emergency flatten, no TPs armed), but Pydantic already
        # coerces numeric values. We just ensure ordering is
        # monotonically increasing (for longs) or decreasing (for
        # shorts); the consumer decides side.
        if len(v) > 4:
            raise ValueError("take_profits may have at most 4 entries (tp1..tp3 + optional runner).")
        return v


# ----------------------------------------------------------------- FeatureSnapshotRecord


class FeatureSnapshotRecord(BaseModel):
    """A persisted feature snapshot.

    Written whenever the decision stack makes a decision (qualified
    or not). The journal is the *write-only* anchor for replay /
    review: the Block 5b BacktestEngine and the Block 5c ReviewAgent
    pull these records back to re-run analysis exactly as it ran
    live.

    PIT compliance
    --------------
    * ``bar_time`` is the bar close time that drove the feature
      computation. ``timestamp`` is the wall-clock time the snapshot
      was *persisted* (typically bar_time + a few microseconds). The
      journal does NOT touch either field after write.
    * ``has_data`` is True iff the underlying :class:`FeatureSnapshotBundle`
      had at least one engine with non-empty data. Consumers can
      filter on this to skip "no_data" snapshots.
    * ``features`` is a flat ``{engine_name: serializable_value}``
      dict. The exact shape is *not* frozen — engines may add new
      keys as Block 2 grows, so consumers must use ``.get()`` access.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(description="Wall-clock time the snapshot was persisted (UTC).")
    symbol: str = Field(default="XAUUSD")
    timeframe: Literal["m1", "m5", "h1", "d1"] = Field(
        default="m1", description="Timeframe the features were computed on."
    )
    bar_time: datetime = Field(description="Bar close time (UTC) the features describe.")
    has_data: bool = Field(description="False if every engine emitted no_data.")
    features: dict[str, Any] = Field(
        default_factory=dict,
        description="Flat {engine_name: serializable} dict. Values are float / int / str / bool / None / list / dict.",
    )
    source_version: str = Field(
        default="block2-v1", description="Feature-engine build version (for analytics + schema evolution)."
    )
    engine_name: str | None = Field(
        default=None,
        description="Engine that produced this snapshot (None for combined bundles).",
    )

    @field_validator("timestamp", "bar_time")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ----------------------------------------------------------------- LLMFallbackDiscrepancy


class LLMFallbackDiscrepancy(BaseModel):
    """A record of a single decision where LLM and RuleBasedFallback disagreed.

    The journal writes one of these per *qualification attempt* where
    ``rule_action != llm_action`` OR where one side blocked and the
    other did not. The Block 5c ReviewAgent uses these to evaluate
    the LLM's contribution over time.

    Stability: ``rule_action`` and ``llm_action`` are stable strings
    from :class:`DecisionAction`. ``final_action`` is the actual
    action taken by the executor — always the one that won the
    resolution (``rule_vetoed`` → rule, ``llm_vetoed`` → llm, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(description="Wall-clock time the discrepancy was logged (UTC).")
    decision_id: UUID = Field(
        description="TradeQualification.qualification_id (or a derived decision UUID) this discrepancy is tied to."
    )
    # --- rule side
    rule_action: DecisionAction = Field(description="Action RuleBasedFallback emitted.")
    rule_score: float = Field(ge=0, le=100, description="Total score at decision time (from rule stack).")
    rule_band: ScoreBand
    rule_block_reasons: list[str] = Field(default_factory=list)
    # --- LLM side (Block 6 — may be all None in pre-Block-6 runs)
    llm_action: DecisionAction | None = Field(
        default=None, description="Action the LLM suggested (None if LLM did not respond or is disabled)."
    )
    llm_score: float | None = Field(
        default=None, ge=0, le=100, description="LLM-supplied score (None if absent)."
    )
    llm_reasoning: str | None = Field(
        default=None, description="LLM's free-form explanation (None if absent)."
    )
    # --- resolution
    final_action: DecisionAction = Field(description="Action that was actually executed.")
    final_source: Literal["rule", "llm"] = Field(description="Whose action was executed.")
    resolution: DiscrepancyResolutionTag = Field(
        description="Tag of how rule vs llm was resolved (rule_vetoed / llm_vetoed / agreement / rule_relaxed)."
    )

    @field_validator("timestamp")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ----------------------------------------------------------------- LLMFallbackDiscrepancyV2


class LLMFallbackDiscrepancyV2(BaseModel):
    """Block-6-spec-exact discrepancy record.

    A simpler, narrower schema than :class:`LLMFallbackDiscrepancy`
    (which is the Block-5a schema written by the BacktestEngine and
    InMemoryJournalStore). This variant is what
    :class:`xauusd_bot.decision.ai_orchestrator.AIDecisionOrchestrator`
    writes — it has the exact field list the Block-6 task spec asks
    for (``timestamp``, ``decision_id``, ``score``,
    ``llm_raw_response``, ``fallback_reason``, ``rule_decision``,
    ``llm_decision``).
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(description="Wall-clock time the discrepancy was logged (UTC).")
    decision_id: UUID = Field(description="Identifier for the decision attempt this record is tied to.")
    score: float = Field(ge=0, le=100, description="Total score at decision time (0..100).")
    llm_raw_response: str | None = Field(
        default=None,
        description="LLM's raw JSON response (None if the LLM was bypassed or did not respond).",
    )
    fallback_reason: Literal[
        "timeout",
        "validation_error",
        "zone_violation",
        "hard_rule_violation",
        "score_below_threshold",
        "openrouter_disabled",
    ] = Field(description="Stable tag of why the fallback was used.")
    rule_decision: str = Field(description="Action the RuleBasedFallback emitted (enter_long/enter_short/no_trade).")
    llm_decision: str | None = Field(
        default=None,
        description="Action the LLM emitted (None if the LLM was bypassed or did not respond).",
    )

    @field_validator("timestamp")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ----------------------------------------------------------------- OrderRecord


class OrderRecord(BaseModel):
    """A persisted order (entry or partial-close / final-close).

    Every OrderEnvelope produced by the Block-4 OrderManager maps to
    one of these. The trade_id FK links it back to the parent
    :class:`TradeRecord`. For partial closes, ``trade_id`` stays the
    same across the entries (it's a "trade", not an "order").
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(description="Time the order was submitted (UTC).")
    trade_id: UUID = Field(description="FK to the parent TradeRecord.id.")
    client_order_id: str = Field(description="Idempotency key from the OrderEnvelope.")
    symbol: str = Field(default="XAUUSD")
    side: OrderSide
    type: OrderType
    volume: Decimal = Field(ge=0, description="Requested volume in lots.")
    requested_price: Decimal | None = Field(
        default=None, description="Requested price (None for market orders)."
    )
    fill_price: Decimal | None = Field(
        default=None, description="Average fill price (None if not filled)."
    )
    slippage_pips: float | None = Field(default=None, description="Fill slippage in pips.")
    slippage_bps: float | None = Field(default=None, description="Fill slippage in basis points.")
    status: OrderStatusTag
    error: str | None = Field(
        default=None, description="Error code / message if rejected (None if accepted)."
    )
    strategy_version: str = Field(default="block5a-v1")

    @field_validator("timestamp")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ----------------------------------------------------------------- re-exports

__all__ = [
    "DiscrepancyResolutionTag",
    "ExitReasonTag",
    "FeatureSnapshotRecord",
    "LLMFallbackDiscrepancy",
    "LLMFallbackDiscrepancyV2",
    "OrderRecord",
    "OrderStatusTag",
    "SessionLiteral",
    "SessionTag",
    "TradeRecord",
    # re-exports
    "DecisionAction",
    "EntryType",
    "OrderSide",
    "OrderType",
    "ScoreBand",
]
