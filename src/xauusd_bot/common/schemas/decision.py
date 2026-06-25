"""Pydantic schemas for the Decision layer (Block 3).

The decision layer transforms a :class:`FeatureSnapshotBundle` (Block 2)
into a :class:`TradeQualification` (Block 3 output) without ever
computing position size, SL, or TP — those are Block 4 (Execution).

Pipeline
--------
1. :class:`FeatureAggregator` consumes a ``FeatureSnapshotBundle`` and
   emits an :class:`AggregatedFeatures` (per-engine subscores 0-100,
   percentile ranks, conflict log).
2. :class:`ScoringEngine` consumes ``AggregatedFeatures`` and emits a
   :class:`Score` (weighted total 0-100, band, reasoning, direction).
3. :class:`RuleBasedFallback` consumes ``Score`` + ``AggregatedFeatures``
   + ``AccountInfo`` and emits a :class:`Decision` (entry_long /
   entry_short / no_trade, with entry_type and block_reason).
4. :class:`TradeQualificationEngine` consumes ``Decision`` +
   ``AggregatedFeatures`` + ``AccountInfo`` + ``Settings`` and emits a
   :class:`TradeQualification` (final action, all block_reasons, UUID).

I-4: Brain vs Hands
-------------------
None of the decision-layer schemas carry ``volume``, ``sl``, ``tp``,
or ``position_size`` fields. Anything that would compute those belongs
in :mod:`xauusd_bot.execution` (Block 4).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    NewsContextOutput,
    SessionName,
    StructureEventType,
    ValueAreaStatus,
)

# ---------------------------------------------------------------- enums


class ScoreBand(str, Enum):
    """Discrete 0-100 score band → entry-type gate.

    * ``below_55``     — no_trade (score < 55)
    * ``observe_55_64`` — observe only (no entry)
    * ``prepare_65_74`` — scout / prepare (entry_type = "scout")
    * ``reduced_75_84`` — reduced position (entry_type = "reduced")
    * ``full_85_plus``  — full position (entry_type = "full")
    """

    BELOW_55 = "below_55"
    OBSERVE_55_64 = "observe_55_64"
    PREPARE_65_74 = "prepare_65_74"
    REDUCED_75_84 = "reduced_75_84"
    FULL_85_PLUS = "full_85_plus"


class DecisionAction(str, Enum):
    """Final action emitted by the trade-qualification engine."""

    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    NO_TRADE = "no_trade"


class EntryType(str, Enum):
    """Sizing intent (NOT a volume — see I-4).

    * ``scout``   — minimal risk, validate the setup.
    * ``reduced`` — half the normal risk.
    * ``full``    — full normal risk (still capped by Block 4 risk engine).
    """

    SCOUT = "scout"
    REDUCED = "reduced"
    FULL = "full"


# ---------------------------------------------------------------- aggregator


class EngineSubscore(BaseModel):
    """One engine's contribution to the aggregate.

    * ``raw`` is the engine-native value (e.g. ``session_risk_factor``,
      ``value_status`` enum, ``in_blackout_flag``) — kept for debugging
      and the journal.
    * ``value`` is a 0-100 score, NaN-free, deterministic.
    * ``percentile`` is a 0-100 rank vs the same engine's last 30
      comparable observations (e.g. last 30 Asia sessions for
      session-relative fields). None for stateless engines.
    * ``weight`` is the engine's weight in the total score (sums to
      100 across engines).
    * ``reasoning`` is a deterministic one-liner explaining how the
      raw value was mapped to the 0-100 score.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Engine name, e.g. 'session', 'vwap', 'htf_volume_profile'.")
    raw: float | str | bool | None = Field(default=None, description="Engine-native value.")
    value: float = Field(ge=0, le=100, description="0-100 subscore for this engine.")
    percentile: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="0-100 percentile rank vs the same engine's last 30 comparable observations.",
    )
    weight: float = Field(ge=0, le=100, description="Engine weight in the total score.")
    direction_bias: Literal[-1, 0, 1] = Field(
        default=0,
        description="-1 = short, 0 = neutral, +1 = long. Aggregated into the final direction.",
    )
    reasoning: str = Field(default="", description="Short, deterministic explanation of the score.")


class ConflictEntry(BaseModel):
    """One engine-pair conflict in the snapshot.

    Examples
    --------
    * ``session`` says "long bias", ``news`` says "no-trade" → conflict.
    * ``htf_volume_profile`` says "above_value (long)", ``structure`` says
      "downtrend" → conflict.
    """

    model_config = ConfigDict(extra="forbid")

    engine_a: str
    engine_b: str
    description: str
    severity: Literal["info", "warning", "block"] = Field(
        default="warning",
        description=(
            "'info' = informational, 'warning' = inconsistency, 'block' = mutually exclusive signals."
        ),
    )


class AggregatedFeatures(BaseModel):
    """Output of :class:`xauusd_bot.decision.aggregator.FeatureAggregator`.

    Contract
    --------
    * ``ts`` = ``current_t`` of the snapshot. PIT-verifiable.
    * ``subscores`` MUST contain exactly one entry per engine the
      decision stack knows about, regardless of whether the engine
      actually had data. Engines with no data get a neutral 50
      subscore with an empty raw value and a reasoning string of
      "no_data".
    * ``weights`` sum to 100 (verified by ``model_validator``).
    * ``total_score`` ∈ [0, 100], deterministic given the same
      subscores + weights.
    * ``dominant_engine`` = the engine with the highest contribution
      (``value × weight``) — useful for the reasoning list.
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime = Field(description="Current time (PIT cursor) of the snapshot.")
    symbol: str = Field(default="XAUUSD")
    subscores: dict[str, EngineSubscore] = Field(
        default_factory=dict,
        description="Per-engine 0-100 subscores, each with weight, direction_bias, and reasoning.",
    )
    conflicts: list[ConflictEntry] = Field(
        default_factory=list,
        description="List of inter-engine conflicts (news-blackout, structure-vs-volume, etc.).",
    )
    dominant_engine: str | None = Field(
        default=None,
        description="Engine name with the highest value×weight contribution. None if no engines.",
    )
    has_data: bool = Field(
        default=False,
        description=(
            "False if the input FeatureSnapshotBundle had no usable data. "
            "Downstream consumers should emit no_trade when False."
        ),
    )

    @property
    def total_score(self) -> float:
        """Sum of (value × weight / 100) over all engines. Always in [0, 100]."""

        return float(sum(s.value * (s.weight / 100.0) for s in self.subscores.values()))

    def to_source_snapshot(self, bundle: FeatureSnapshotBundle) -> dict[str, object]:
        """Compact view of the source :class:`FeatureSnapshotBundle` for the journal."""

        return {
            "ts": bundle.ts.isoformat(),
            "session": bundle.session.current_session.value if bundle.session else None,
            "session_risk_factor": bundle.session.session_risk_factor if bundle.session else None,
            "atr": bundle.atr,
            "structure_trend": bundle.structure.trend if bundle.structure else None,
            "news_in_blackout": bundle.news.in_blackout_flag if bundle.news else None,
        }


# ---------------------------------------------------------------- scoring


class Score(BaseModel):
    """Output of :class:`xauusd_bot.decision.scoring.ScoringEngine`.

    Contract
    --------
    * ``total_score`` ∈ [0, 100].
    * ``subscores`` is a flat ``{engine_name: 0..100}`` dict for fast
      consumers (and for the journal). The per-engine full
      :class:`EngineSubscore` lives on :class:`AggregatedFeatures`.
    * ``band`` is the discrete gate. Mapping: <55 below_55 · 55-64
      observe · 65-74 prepare · 75-84 reduced · ≥85 full.
    * ``reasoning`` is a deterministic list of short strings —
      one per contributing engine and one per detected conflict.
    * ``direction`` is the aggregate bias across all engines'
      ``direction_bias`` values.
    """

    model_config = ConfigDict(extra="forbid")

    total_score: float = Field(ge=0, le=100)
    subscores: dict[str, float] = Field(
        default_factory=dict, description="Flat per-engine 0-100 scores (e.g. 'h1_zone': 78)."
    )
    band: ScoreBand
    reasoning: list[str] = Field(default_factory=list, description="Deterministic explanation lines.")
    direction: Literal["long", "short", "neutral"]
    timestamp: datetime

    @staticmethod
    def band_for(score: float) -> ScoreBand:
        """Map a total score to its band. Public so RuleBasedFallback can reuse."""

        if score < 55:
            return ScoreBand.BELOW_55
        if score < 65:
            return ScoreBand.OBSERVE_55_64
        if score < 75:
            return ScoreBand.PREPARE_65_74
        if score < 85:
            return ScoreBand.REDUCED_75_84
        return ScoreBand.FULL_85_PLUS


# ---------------------------------------------------------------- rule fallback


class LLMIntent(BaseModel):
    """The LLM's *intent* carried to the executor (Phase C — "AI leads SL/TP").

    The AI is the brain: it proposes WHERE (entry zone), WHEN-it's-dead
    (invalidation prices) and HOW-far (TP R-multiples + runner target).
    The executor (the hands) turns that intent into concrete SL/TP prices
    and lots, **always clamped by the Phase-A deterministic floors/caps**
    (SL floor, max-risk cap). I-4 holds: the LLM never sets a lot size or
    an absolute SL/TP that bypasses the floors.

    All fields optional — a sparse LLM response (or the rule path) just
    leaves them ``None`` and the executor falls back to its deterministic
    structure SL + liquidity/R TP exactly as before.
    """

    model_config = ConfigDict(extra="forbid")

    entry_min: float | None = Field(default=None, description="LLM entry_zone.price_min (USD).")
    entry_max: float | None = Field(default=None, description="LLM entry_zone.price_max (USD).")
    invalidations: list[str] = Field(
        default_factory=list,
        description="Free-form 'trade is dead if X' strings; the executor extracts an SL price hint.",
    )
    tp1_rr: float | None = Field(default=None, description="LLM TP1 in R-multiples.")
    tp2_rr: float | None = Field(default=None, description="LLM TP2 in R-multiples.")
    runner_to: str | None = Field(default=None, description="LLM runner destination label (HTF level).")
    confidence: int | None = Field(default=None, ge=0, le=100, description="LLM confidence (advisory).")


class Decision(BaseModel):
    """Output of :class:`xauusd_bot.decision.rule_fallback.RuleBasedFallback`.

    Contract
    --------
    * If ``action != DecisionAction.NO_TRADE`` then ``entry_type`` is
      set (``scout``/``reduced``/``full``) and ``block_reason`` is None.
    * If ``action == DecisionAction.NO_TRADE`` then ``entry_type`` is
      None and ``block_reason`` is one of the stable strings
      documented in :mod:`xauusd_bot.decision.rule_fallback`.
    """

    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    entry_type: EntryType | None = None
    block_reason: str | None = None
    source_score: float = Field(ge=0, le=100, description="Score.total_score at decision time.")
    source_band: ScoreBand
    source_direction: Literal["long", "short", "neutral"]
    source_engine: Literal["ai", "rule"] = Field(
        default="rule",
        description=(
            "Which engine produced this decision — 'ai' when the LLM decided "
            "(orchestrator _llm_to_decision), 'rule' for the deterministic fallback. "
            "Propagated to the order tag so journal_trades.engine_source is accurate."
        ),
    )
    llm_intent: LLMIntent | None = Field(
        default=None,
        description=(
            "The LLM's entry-zone / invalidation / TP-R / runner intent (Phase C). "
            "None for rule-path decisions. The executor consumes it within the "
            "Phase-A floors/caps; it never overrides a safety floor."
        ),
    )
    timestamp: datetime


# ---------------------------------------------------------------- qualification


class TradeQualification(BaseModel):
    """Output of :class:`xauusd_bot.decision.qualification.TradeQualificationEngine`.

    The final Block 3 artifact. Block 4 (Execution) consumes this and
    adds position size, SL, TP, etc. — never the other way around.

    * ``qualified`` = True ⇔ ``final_action != NO_TRADE`` ⇔
      ``block_reasons`` is empty.
    * ``qualification_id`` is a fresh UUID per call, used by the
      journal (Block 5) to link the qualification to the trade.
    """

    model_config = ConfigDict(extra="forbid")

    qualified: bool
    qualification_id: UUID = Field(default_factory=uuid4)
    final_action: DecisionAction
    final_entry_type: EntryType | None = None
    block_reasons: list[str] = Field(default_factory=list)
    final_direction: Literal["long", "short", "neutral"]
    source_score: float = Field(ge=0, le=100)
    source_band: ScoreBand
    timestamp: datetime

    @classmethod
    def from_decision(
        cls,
        decision: Decision,
        score: Score,
        extra_block_reasons: list[str] | None = None,
    ) -> "TradeQualification":
        """Build a TradeQualification from an existing Decision.

        If ``extra_block_reasons`` is non-empty, ``final_action`` is
        forced to ``NO_TRADE`` and the reasons are appended. This is
        the standard pipeline: ``Decision`` first, then
        ``TradeQualificationEngine`` either confirms it or vetoes it
        with extra reasons.
        """

        extra = list(extra_block_reasons or [])
        if extra:
            return cls(
                qualified=False,
                final_action=DecisionAction.NO_TRADE,
                final_entry_type=None,
                block_reasons=extra,
                final_direction="neutral",
                source_score=score.total_score,
                source_band=score.band,
                timestamp=decision.timestamp,
            )
        qualified = decision.action != DecisionAction.NO_TRADE
        return cls(
            qualified=qualified,
            final_action=decision.action,
            final_entry_type=decision.entry_type,
            block_reasons=[decision.block_reason] if decision.block_reason else [],
            final_direction=score.direction if qualified else "neutral",
            source_score=score.total_score,
            source_band=score.band,
            timestamp=decision.timestamp,
        )


# ---------------------------------------------------------------- re-exports

# Re-export some feature-enums for convenience so callers can
# `from xauusd_bot.common.schemas.decision import SessionName`.
__all__ = [
    "AggregatedFeatures",
    "ConflictEntry",
    "Decision",
    "DecisionAction",
    "EngineSubscore",
    "EntryType",
    "LLMIntent",
    "Score",
    "ScoreBand",
    "TradeQualification",
    # re-exports
    "SessionName",
    "StructureEventType",
    "ValueAreaStatus",
    "NewsContextOutput",
]
