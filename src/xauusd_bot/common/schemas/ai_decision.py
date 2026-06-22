"""Pydantic schemas for the AI Decision Layer (Block 6).

The :class:`LLMDecision` schema is the *contract* between OpenRouter
and the rest of the bot. The LLM is required to emit exactly one
JSON object matching this shape; the OpenRouter client (Block 6)
parses + validates it before the orchestrator (Block 6) uses it.

I-4: Brain vs Hands
-------------------
The LLM produces *direction, entry intent, invalidation criteria,
management hints in R-multiples* — never position size, never
absolute stop-loss or take-profit prices. The execution engine
(Block 4) computes those from the LLM's R-multiple hints + the
FeatureSnapshotBundle's zones.

The :class:`EntryZone` here is a *price range* drawn from the
SnapshotBundle's :class:`xauusd_bot.common.schemas.features.FVGZone`
list. The :class:`AIDecisionLayer` validates that
``entry_zone.price_min`` and ``entry_zone.price_max`` are inside one
of the supplied zones — if not, the call is rejected
(:class:`LLMZoneViolation`).

Validation contract
-------------------
* ``extra="forbid"`` everywhere: a missing/extra field is a bug,
  not a courtesy.
* All literal fields are strict enums via :class:`typing.Literal` —
  the LLM cannot emit a typo without :class:`pydantic.ValidationError`.
* All numeric ranges are bounded (e.g. ``confidence: 0..100``,
  ``comment: 0..1500``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Some models (notably minimax-m3) serialize a JSON null as the *string* "null"
# for optional fields — e.g. ``"entry_type": "null"``, ``"vwap_mode": "null"``.
# That fails the strict Literal validation and drops the whole decision to the
# rule fallback. Coerce these stringy-nulls back to None before field validation.
_NULLISH = {"null", "none", "nil", "n/a", "na", "undefined", ""}


def _coerce_nullish(v: object) -> object:
    if isinstance(v, str) and v.strip().lower() in _NULLISH:
        return None
    return v


# ---------------------------------------------------------------- entry zone


class EntryZone(BaseModel):
    """The LLM's proposed entry price range, expressed in price units.

    The :class:`AIDecisionLayer` requires ``price_min <= price_max``
    when both are set. Either bound may be ``None`` (open-ended
    zone) — common for breakout setups that target only the
    "min" (entry trigger) or the "max" (worst-case fill).

    Both fields are ``None`` is allowed: the LLM is signaling
    "I have a direction but no specific zone" (e.g. momentum
    continuation). The orchestrator treats that as ``decision=watch``.

    NaN / Inf are rejected via ``allow_inf_nan=False`` — a price
    of ``float('inf')`` would silently bypass the AIDecisionLayer's
    zone-range check downstream, so we block it at the schema
    boundary.
    """

    model_config = ConfigDict(extra="forbid")

    price_min: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="Lower bound of the entry zone (USD). None = no lower bound.",
    )
    price_max: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="Upper bound of the entry zone (USD). None = no upper bound.",
    )

    _nullish = field_validator("price_min", "price_max", mode="before")(_coerce_nullish)


# ---------------------------------------------------------------- management


class ManagementBlock(BaseModel):
    """Management hints in *R-multiples* (NOT absolute prices — see I-4).

    ``tp1_rr`` / ``tp2_rr`` are R-multiples measured from the
    proposed entry zone's midpoint to the take-profit level, divided
    by the same distance to the stop-loss. Typical values:
    1.0 (1:1 R:R) for tp1, 2.0 (1:2) for tp2.

    ``runner_to`` is a *free-form* destination label that the
    executor (Block 4) maps to a concrete HTF volume-profile level
    (e.g. ``"prev_week.vah"``, ``"monthly.vpoc"``). The label is
    opaque to the AI layer — only the executor knows how to resolve
    it. The schema only forbids empty strings.

    ``protect_before_news_min`` is a minutes-lookahead: if a
    high-impact event is within N minutes, the executor will
    flatten or tighten stops. ``None`` = "no special protection".
    """

    model_config = ConfigDict(extra="forbid")

    tp1_rr: float | None = Field(
        default=None,
        ge=0,
        description="TP1 in R-multiples (≥ 0). None = LLM did not propose a TP1.",
    )
    tp2_rr: float | None = Field(
        default=None,
        ge=0,
        description="TP2 in R-multiples (≥ 0). None = LLM did not propose a TP2.",
    )
    runner_to: str | None = Field(
        default=None,
        description=(
            "Free-form destination label for the runner (e.g. 'prev_week.vah'). "
            "Executor resolves to a real level. None = no runner proposed."
        ),
    )
    protect_before_news_min: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Minutes before a high-impact event at which the executor should "
            "tighten stops. None = no special protection."
        ),
    )

    _nullish = field_validator(
        "tp1_rr", "tp2_rr", "runner_to", "protect_before_news_min", mode="before"
    )(_coerce_nullish)


# ---------------------------------------------------------------- confluence


class ConfluenceBlock(BaseModel):
    """Auditable breakdown of the entry-validation checklist (v2 prompt).

    The v2 ``decision_agent.md`` asks the LLM to walk Joshua's entry
    sequence and report *why* it decided — so a human (or the journal)
    can see whether the model actually checked: are we in the zone, how
    many confluent zones, where in the H1 fib retracement, the VWAP mode,
    and whether volume confirmed. All fields are advisory / observational
    — they never override the deterministic gates. The block is optional
    (``default_factory``) so a pre-v2 prompt that omits it still validates.
    """

    model_config = ConfigDict(extra="forbid")

    in_zone: bool = Field(
        default=False,
        description="Is price currently in/at an H1/M5 demand-supply zone or FVG?",
    )
    zones_at_entry: int = Field(
        default=0,
        ge=0,
        description="Count of confluent zones at the entry (H1 zone + M1 FVG + golden pocket …).",
    )
    fib_zone: str | None = Field(
        default=None,
        description=(
            "Which fib bracket the price sits in (echoes the fib engine's price_zone: "
            "shallow / 0.236 / 0.382 / golden_pocket / deep / extended). Advisory free-text — "
            "kept permissive so an unexpected value never drops the whole decision to fallback."
        ),
    )
    h1_trend: Literal["strong", "weak", "none"] = Field(
        default="none",
        description="Strength of the last H1 trend (informs the expected retracement depth).",
    )
    deeper_fvg_pending: bool = Field(
        default=False,
        description="An unmitigated deeper FVG may be run first (a FACTOR, not a hard gate).",
    )
    vwap_mode: Literal["pullback", "trend"] | None = Field(
        default=None,
        description="Pullback-to-VWAP reversal vs trend-continuation after recross. None = unclear.",
    )
    volume_confirms: bool | None = Field(
        default=None,
        description="Did volume + candle print confirm the reaction? None = not assessable yet.",
    )

    _nullish = field_validator(
        "fib_zone", "vwap_mode", "volume_confirms", mode="before"
    )(_coerce_nullish)


# ---------------------------------------------------------------- main LLMDecision


class LLMDecision(BaseModel):
    """The full AI-Decision payload — what OpenRouter must emit.

    Field-by-field contract
    -----------------------
    * ``decision`` — one of the canonical :class:`xauusd_bot.common.schemas.decision.DecisionAction`
      values plus two AI-only intermediate states (``watch`` /
      ``prepare``). The orchestrator maps these to the Block-3
      ``DecisionAction`` enum.

      * ``"no_trade"`` — LLM explicitly says "skip this bar".
      * ``"watch"``    — LLM sees potential but no clean setup.
      * ``"prepare"``  — LLM wants to be ready for a setup that
        hasn't triggered yet.
      * ``"scout"``    — minimal-risk entry (entry_type=scout).
      * ``"reduced_entry"`` — half-risk entry (entry_type=reduced).
      * ``"full_entry"``    — full-risk entry (entry_type=full).

    * ``entry_type`` — *how* to enter (``confirmation`` /
      ``pullback`` / ``breakout_retest`` / ``None``). Independent
      from ``decision``: ``decision=prepare`` may have
      ``entry_type=breakout_retest`` (waiting for the breakout).
    * ``entry_side`` — ``"long"`` / ``"short"`` / ``None``.
    * ``entry_zone`` — :class:`EntryZone` from the SnapshotBundle
      zones; validated by :class:`AIDecisionLayer`.
    * ``invalidations`` — free-form list of "this trade is dead if
      X" criteria (e.g. ``"close < 2370.00"``,
      ``"vwap_loss"``). The executor pattern-matches these.
    * ``management`` — :class:`ManagementBlock` of R-multiple hints.
    * ``confidence`` — LLM's 0-100 confidence score. Advisory only.
    * ``comment`` — free-form rationale. Max 1500 chars.

    The schema is ``extra="forbid"`` — any extra key the LLM emits
    causes :class:`pydantic.ValidationError` and triggers the
    orchestrator's retry/fallback path.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal[
        "no_trade", "watch", "prepare", "scout", "reduced_entry", "full_entry"
    ] = Field(description="Final AI decision (see class docstring).")
    entry_type: Literal["confirmation", "pullback", "breakout_retest"] | None = Field(
        default=None,
        description="How to enter. None = no entry intent (no_trade / watch / prepare).",
    )
    entry_side: Literal["long", "short"] | None = Field(
        default=None,
        description="Trade side. None = no direction (no_trade / watch).",
    )

    _nullish = field_validator("entry_type", "entry_side", mode="before")(_coerce_nullish)
    entry_zone: EntryZone = Field(
        default_factory=EntryZone,
        description="Proposed entry price range, validated against the snapshot zones.",
    )
    invalidations: list[str] = Field(
        default_factory=list,
        description="Free-form invalidation criteria (e.g. 'close < 2370.00').",
    )
    management: ManagementBlock = Field(
        default_factory=ManagementBlock,
        description="Management hints in R-multiples (NOT absolute prices — see I-4).",
    )
    confluence: ConfluenceBlock = Field(
        default_factory=ConfluenceBlock,
        description="Auditable entry-validation breakdown (v2 prompt). Advisory only.",
    )
    confidence: int = Field(
        ge=0,
        le=100,
        description="LLM's 0-100 confidence score. Advisory only.",
    )
    comment: str = Field(
        default="",
        max_length=1500,
        description="Free-form rationale (≤ 1500 chars). Was 500, which clipped the model's reasoning mid-sentence.",
    )


# ---------------------------------------------------------------- helper for orchestrator


# Stable reason strings for the orchestrator. Tests assert on these.
REASON_LLM_DISABLED = "openrouter_disabled"
REASON_SCORE_BELOW_THRESHOLD = "score_below_threshold"
REASON_NEWS_BLACKOUT = "news_blackout"
REASON_NO_API_KEY = "openrouter_api_key_missing"
REASON_TIMEOUT = "timeout"
REASON_VALIDATION_ERROR = "validation_error"
REASON_ZONE_VIOLATION = "zone_violation"
REASON_HARD_RULE_VIOLATION = "hard_rule_violation"

__all__ = [
    "ConfluenceBlock",
    "EntryZone",
    "LLMDecision",
    "ManagementBlock",
    "REASON_HARD_RULE_VIOLATION",
    "REASON_LLM_DISABLED",
    "REASON_NEWS_BLACKOUT",
    "REASON_NO_API_KEY",
    "REASON_SCORE_BELOW_THRESHOLD",
    "REASON_TIMEOUT",
    "REASON_VALIDATION_ERROR",
    "REASON_ZONE_VIOLATION",
]
