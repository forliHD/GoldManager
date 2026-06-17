"""Pydantic schemas for the Review / FittingProposal layer (Block 5c).

This module defines the data shapes the DailyReviewEngine,
WeeklyReviewEngine and FittingProposalEngine exchange with each
other, the LLM (OpenRouter), and the JournalStore.

Architectural position
----------------------
The Review layer sits *after* the Block-5b BacktestEngine in the
pipeline:

    Bar → Features → Decision → Risk → Execution → TradeJournalDB
                                                       │
                          BacktestEngine ◀─────────────┘
                          FittingProposalEngine ◀──── ReviewAgent (LLM)
                                                       │
                                                status machine
                                          (proposed → backtested
                                            → approved / rejected)

The review layer NEVER touches the live executor. It produces
*hypotheses* for the operator to consider.

Invariants
----------
* **I-1:** this module does NOT import ``MetaTrader5``. It
  exchanges Pydantic data with the journal + the LLM.
* **I-4:** the LLM output is constrained to *categorical hypotheses*
  (categories, observation/hypothesis strings, validation-test
  descriptions). It does NOT emit position size, SL, or TP. The
  FittingProposal state machine (``approved``) is a *human* signal —
  no code reads ``status == 'approved'`` and mutates live settings
  automatically.
* **extra='forbid'** on every persisted / validated schema: a
  missing/extra field is a bug, not a courtesy.

Schema stability
----------------
* :class:`ReviewRequest` / :class:`ReviewProposal` /
  :class:`ReviewOutput` are the LLM contract — any change is a
  prompt-update + dual-commit.
* :class:`FittingProposal` is the persisted record. The ``status``
  field is stable; new statuses are forward-compatible (append-only).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xauusd_bot.common.schemas.decision import EntryType, ScoreBand

# ---------------------------------------------------------------- helpers


def _ensure_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC).")
    return v.astimezone(tz=timezone.utc)


# ---------------------------------------------------------------- lightweight summaries
#
# These are the "what the reviewer sees" versions of the Block-5a /
# Block-6 records. We deliberately DO NOT embed the full
# TradeRecord / FeatureSnapshotRecord because:
#
# 1. The reviewer only needs a flat summary — the engine doesn't
#    reconstruct the trade, it proposes changes.
# 2. Smaller payloads = cheaper LLM calls + less prompt drift.
# 3. PIT integrity is preserved: the trade / snapshot are still in
#    the journal, the summary is a *view* of them at write time.


class TradeSummary(BaseModel):
    """A flat summary of one :class:`TradeRecord` for the reviewer.

    Numeric fields are :class:`float` (not Decimal) — the reviewer
    doesn't need exact accounting precision, only the shape of the
    distribution.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    timestamp_open: datetime = Field(description="Open time (UTC).")
    timestamp_close: datetime | None = Field(default=None, description="Close time (UTC).")
    symbol: str = Field(default="XAUUSD")
    side: Literal["long", "short"]
    score: float = Field(ge=0, le=100)
    band: ScoreBand
    entry_type: EntryType
    session: str = Field(default="closed", description="Session tag at open.")
    structure_at_entry: Literal["up", "down", "range"] = "range"
    pnl_realized: float | None = Field(default=None, description="Realized PnL in USD.")
    r_multiple: float | None = Field(default=None, description="R-multiple at close.")
    exit_reason: str | None = Field(default=None)
    slippage_pips: float | None = None
    engine_source: Literal["rule", "ai"] = "rule"

    @field_validator("timestamp_open", "timestamp_close")
    @classmethod
    def _validate_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return _ensure_utc(v)


class FeatureSnapshotLite(BaseModel):
    """A flat summary of one :class:`FeatureSnapshotRecord` for the reviewer.

    The reviewer doesn't see the full feature bundle — only the
    fields that drive setup classification (session, structure,
    news-blackout flag, ATR, score band snapshot).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    bar_time: datetime = Field(description="Bar close time (UTC).")
    session: str | None = None
    structure_trend: Literal["up", "down", "range"] | None = None
    in_blackout: bool | None = None
    atr: float | None = Field(default=None, ge=0)
    score: float | None = Field(default=None, ge=0, le=100)
    band: ScoreBand | None = None
    engine_source: Literal["rule", "ai"] = "rule"

    @field_validator("bar_time")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


class KPISummary(BaseModel):
    """KPI snapshot for the reviewer — Block-5a aggregations, distilled.

    Field names mirror :class:`BacktestStats` where possible, but
    this is a *reviewer-friendly* flat shape, not a backtest
    contract.
    """

    model_config = ConfigDict(extra="forbid")

    n_trades: int = Field(ge=0, description="Total trades in the period.")
    n_closed: int = Field(ge=0)
    n_wins: int = Field(ge=0)
    n_losses: int = Field(ge=0)
    winrate: float = Field(ge=0, le=1)
    avg_r: float
    total_r: float
    profit_factor: float = Field(ge=0)
    sharpe: float
    sortino: float
    max_drawdown: float = Field(ge=0, description="Peak-to-trough USD.")
    total_pnl: float = Field(description="Realized PnL in USD.")
    # Per-bucket breakdowns (key names match journal.queries.py)
    setup_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)
    session_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)
    score_band_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)
    r_distribution: dict[str, int] = Field(default_factory=dict)


class LLMFallbackDiscrepancyLite(BaseModel):
    """A flat summary of one LLM↔RuleBasedFallback discrepancy."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    decision_id: UUID
    score: float = Field(ge=0, le=100)
    rule_decision: str = Field(description="RuleBasedFallback action.")
    llm_decision: str | None = Field(default=None, description="LLM action (None if disabled).")
    fallback_reason: str | None = Field(
        default=None,
        description="Why the fallback was used (timeout / validation_error / etc).",
    )
    llm_raw_response: str | None = Field(default=None)

    @field_validator("timestamp")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ---------------------------------------------------------------- ReviewRequest / Output


# Canonical review categories. Used for the LLM contract AND for
# :class:`FittingProposalFilter` so the operator can query by
# category.
ReviewCategory = Literal[
    "score_threshold",
    "news_blackout",
    "level_usage",
    "bin_size",
    "value_area",
    "entry_type",
    "session_filter",
    "sl_tp",
    "execution",
    "other",
]

OverfittingRisk = Literal["low", "medium", "high"]

DataSufficiency = Literal["sufficient", "marginal", "insufficient"]


class ReviewRequest(BaseModel):
    """The input payload sent to the Reviewer LLM.

    Built by :class:`xauusd_bot.review.engine.ReviewEngine` from a
    journal query. The reviewer never sees raw bars / PII — only
    the flat summaries above.
    """

    model_config = ConfigDict(extra="forbid")

    period_start: datetime = Field(description="Period start (UTC, inclusive).")
    period_end: datetime = Field(description="Period end (UTC, exclusive).")
    period_kind: Literal["daily", "weekly"] = Field(description="Period granularity.")
    trades: list[TradeSummary] = Field(default_factory=list)
    snapshots_sample: list[FeatureSnapshotLite] = Field(
        default_factory=list,
        description="Sampled feature snapshots (max ~200 per period by engine convention).",
    )
    kpis: KPISummary = Field(description="Aggregate KPIs for the period.")
    discrepancies: list[LLMFallbackDiscrepancyLite] = Field(
        default_factory=list,
        description="LLM↔RuleBasedFallback disagreements (sampled to ≤50 per Caveat 4i.7).",
    )
    min_sample_size_for_proposals: int = Field(
        default=30,
        ge=1,
        description="Hard floor: under this many trades, output zero proposals and data_sufficiency='insufficient'.",
    )

    @field_validator("period_start", "period_end")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


class ReviewProposal(BaseModel):
    """A single numbered proposal from the reviewer.

    The LLM emits a list of these inside :class:`ReviewOutput`. They
    are *hypotheses*, not commands. A :class:`FittingProposal`
    wraps each one with a state-machine status.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_number: int = Field(ge=1, description="Sequential number within the review.")
    category: ReviewCategory
    observation: str = Field(
        description="Beobachtung + zugrundeliegende Kennzahl + Stichprobengröße.",
        min_length=1,
    )
    hypothesis: str = Field(description="Konkrete Hypothese für eine Regeländerung.", min_length=1)
    validation_test: str = Field(
        description=(
            "Konkreter Backtest-Setup-String (z.B. 'score_threshold=70, IS=4w, OOS=1w'). "
            "Wird vom BacktestSpec-Parser interpretiert — see xauusd_bot.review.backtest_spec_parser."
        ),
        min_length=1,
    )
    overfitting_risk: OverfittingRisk
    overfitting_rationale: str = Field(
        description="Begründung des Overfitting-Risikos.", min_length=1,
    )


class ReviewOutput(BaseModel):
    """The full LLM output for a single review period."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[ReviewProposal] = Field(default_factory=list)
    overall_assessment: str = Field(
        description="Gesamteinschätzung: Reicht die Datenlage für belastbare Schlüsse?",
        min_length=1,
    )
    data_sufficiency: DataSufficiency = Field(
        description=(
            "'sufficient' = belastbare Aussagen möglich, "
            "'marginal' = Trends erkennbar aber nicht bestätigungsfähig, "
            "'insufficient' = Stichprobe zu klein."
        ),
    )
    summary: str = Field(
        description="1-3 Sätze Zusammenfassung für den Operator.", min_length=1,
    )


# ---------------------------------------------------------------- FittingProposal
#
# The persistent record. Status transitions are explicit (see
# xauusd_bot.review.fitting_proposal.FittingProposalEngine): only
# the operator (via CLI / future Block-9 dashboard) can transition
# the state. NO code path reads ``status == 'approved'`` and
# mutates live settings.


FittingProposalStatus = Literal["proposed", "backtested", "approved", "rejected"]


class FittingProposal(BaseModel):
    """A persisted fitting proposal with an explicit state machine.

    Lifecycle::

        proposed ──► backtested ──► approved
            │           │
            └──► approved / rejected (operator shortcut)
            └──► rejected (operator)

    The transitions are encoded in
    :class:`xauusd_bot.review.fitting_proposal.FittingProposalEngine`
    and gated by unit tests. ``approved`` / ``rejected`` are
    terminal states.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
        description="Wall-clock time the proposal was persisted (UTC).",
    )
    period_start: datetime
    period_end: datetime
    proposal_number: int = Field(ge=1)
    category: ReviewCategory
    observation: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    validation_test: str = Field(min_length=1)
    overfitting_risk: OverfittingRisk
    overfitting_rationale: str = Field(min_length=1)
    status: FittingProposalStatus = Field(default="proposed")
    backtest_result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Free-form backtest stats dict once status='backtested'. "
            "Schema is the BacktestStats block of BacktestResult (see "
            "xauusd_bot.common.schemas.backtest.BacktestStats)."
        ),
    )
    decided_at: datetime | None = Field(
        default=None,
        description="Set when status transitions to approved or rejected.",
    )
    decided_by: str | None = Field(
        default=None,
        description="Operator-Name (later aus Settings/Env).",
    )
    decision_note: str | None = Field(
        default=None,
        description="Optional free-form note from the operator.",
    )
    # Block-5c provenance fields — useful for the dashboard in Block 9.
    review_id: UUID | None = Field(
        default=None,
        description="Optional FK to the ReviewRun that produced this proposal.",
    )
    source_period_kind: Literal["daily", "weekly"] | None = Field(default=None)

    @field_validator("period_start", "period_end", "created_at", "decided_at")
    @classmethod
    def _validate_utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return _ensure_utc(v)


class FittingProposalFilter(BaseModel):
    """Filter for :meth:`FittingProposalEngine.list_proposals`.

    Empty / ``None`` filter fields mean "no constraint".
    """

    model_config = ConfigDict(extra="forbid")

    status: list[FittingProposalStatus] | None = Field(
        default=None, description="Status values to include. None = all."
    )
    category: list[ReviewCategory] | None = Field(
        default=None, description="Categories to include. None = all."
    )
    overfitting_risk: list[OverfittingRisk] | None = Field(
        default=None, description="Overfitting-risk levels to include. None = all."
    )
    min_period: date | None = Field(
        default=None, description="Minimum period_start date (inclusive). None = no lower bound."
    )
    max_period: date | None = Field(
        default=None, description="Maximum period_start date (inclusive). None = no upper bound."
    )


# ---------------------------------------------------------------- ReviewRun (in-memory)
#
# The :class:`xauusd_bot.review.engine.ReviewEngine` returns a
# ReviewRun from ``run_daily`` / ``run_weekly``. It's a *value*
# type — the engine never mutates it after creation. The
# FittingProposalEngine consumes it via ``from_review``.


class ReviewRun(BaseModel):
    """The output of one DailyReview / WeeklyReview run.

    Not a persisted record — just a value type returned to the
    caller (CLI / dashboard). The journal's persistence path is
    :meth:`JournalStore.add_fitting_proposal` /
    :meth:`JournalStore.update_fitting_proposal`, not this object.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    period_start: datetime
    period_end: datetime
    period_kind: Literal["daily", "weekly"]
    insufficient_data: bool = Field(
        default=False,
        description=(
            "True when the period had fewer than ``min_sample_size`` trades. "
            "In that case ``output`` may be None and no proposals are persisted."
        ),
    )
    min_sample_size: int = Field(default=10, ge=1)
    trade_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    discrepancy_count: int = Field(ge=0)
    # Cross-day pattern detection (weekly review only). Empty for daily.
    setup_breakdown_over_days: dict[str, dict[str, float]] = Field(default_factory=dict)
    score_band_drift: dict[str, dict[str, float]] = Field(default_factory=dict)
    discrepancy_summary: dict[str, int] = Field(default_factory=dict)
    output: ReviewOutput | None = Field(
        default=None,
        description="The LLM output. None when insufficient_data=True.",
    )
    error: str | None = Field(
        default=None,
        description="Reviewer error message if the LLM call failed.",
    )

    @field_validator("period_start", "period_end")
    @classmethod
    def _validate_utc(cls, v: datetime) -> datetime:
        return _ensure_utc(v)


# ---------------------------------------------------------------- re-exports


__all__ = [
    "DataSufficiency",
    "FeatureSnapshotLite",
    "FittingProposal",
    "FittingProposalFilter",
    "FittingProposalStatus",
    "KPISummary",
    "LLMFallbackDiscrepancyLite",
    "OverfittingRisk",
    "ReviewCategory",
    "ReviewOutput",
    "ReviewProposal",
    "ReviewRequest",
    "ReviewRun",
    "TradeSummary",
]