"""Pydantic schemas for the Execution layer (Block 4).

Block 4 is the **deterministic "Hands"** layer — it owns position
sizing, stop-loss / take-profit, order management, and emergency
control. The decision layer (Block 3) is the "Brain" and never
computes volume, SL, or TP; this is the layer that does. See
``AGENTS.md`` §3 I-4 for the formal contract.

Conventions
-----------
* Money / size fields use ``Decimal`` — no float drift in money.
* Prices use ``Decimal`` — same reason.
* Time fields are timezone-aware UTC ``datetime`` objects.
* All schemas are ``extra='forbid'`` — a missing/extra field is a bug.
* Each schema carries a ``timestamp`` for log/journal correlation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
    TradeQualification,
)
from xauusd_bot.connectors.schemas import (
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
)

# ----------------------------------------------------------------- enums


class RiskBand(str, Enum):
    """Score-band → risk-per-trade mapping.

    The RiskManager is the **only** place that translates a score band
    into a concrete risk fraction. The decision layer (Block 3) only
    knows about the abstract :class:`EntryType` (``scout`` /
    ``reduced`` / ``full``); this is where it becomes a number.
    """

    SCOUT = "scout"           # 65-74 → 0.5% per trade
    REDUCED = "reduced"       # 75-84 → 1.0% per trade
    FULL = "full"             # ≥85   → 2.0% per trade


class SizingRoundingMode(str, Enum):
    """How the PositionSizer rounded the calculated lot size.

    * ``exact``       — result already matched the lot step.
    * ``rounded_down`` — rounded down to the nearest lot step.
    * ``below_min``   — calculated < volume_min → snapped to volume_min.
    * ``above_max``   — calculated > volume_max → capped at volume_max.
    """

    EXACT = "exact"
    ROUNDED_DOWN = "rounded_down"
    BELOW_MIN = "below_min"
    ABOVE_MAX = "above_max"


class OrderTag(str, Enum):
    """Stable tags used to attribute orders in the journal / slippage analysis."""

    RULE_BASED = "rule"        # Block 3 RuleBasedFallback decided
    AI_ASSISTED = "ai"         # Block 6 LLM assisted (Block 4 schema reserved)


class TrailingMode(str, Enum):
    """Trailing-stop mode after the entry has been validated."""

    FIXED = "fixed"            # SL stays at initial level.
    BREAK_EVEN = "break_even"  # Moved to entry + spread after TP1 hit.
    STRUCTURE_TRAIL = "structure_trail"  # Trail behind new M5 BOS, min 1 ATR.


class ExitReason(str, Enum):
    """Why a position was closed / flattened."""

    STOP_HIT = "stop_hit"
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    TP3_HIT = "tp3_hit"
    TRAILED = "trailed"
    EMERGENCY = "emergency"
    MANUAL = "manual"
    EXPIRED = "expired"
    STRUCTURE_BREAK = "structure_break"


# ----------------------------------------------------------------- risk


class RiskVerdict(BaseModel):
    """Output of :class:`xauusd_bot.execution.risk.RiskManager.approve`.

    A ``RiskVerdict`` is a *veto or approval* — the executor may not
    open a position unless ``approved`` is True. Every block reason
    here is final; RiskManager is safety-authoritative (AGENTS.md I-4).
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    risk_band: RiskBand | None = Field(
        default=None,
        description="Score band that drove the risk percentage (None when blocked before sizing).",
    )
    risk_per_trade_pct: float = Field(
        ge=0,
        le=1,
        description="Risk for THIS trade as a fraction of equity (0.005 / 0.01 / 0.02).",
    )
    risk_amount: Decimal = Field(
        ge=0,
        description="Absolute risk amount in account currency (USD).",
    )
    blocked_reason: str | None = Field(
        default=None,
        description="Stable block-reason string (None when approved).",
    )
    # --- state the risk layer maintains (exposed for the journal/UI)
    daily_pnl_running: Decimal = Field(
        default=Decimal("0"),
        description="Cumulative PnL since 00:00 UTC (negative = loss).",
    )
    weekly_pnl_running: Decimal = Field(
        default=Decimal("0"),
        description="Cumulative PnL since Monday 00:00 UTC (negative = loss).",
    )
    equity: Decimal = Field(
        default=Decimal("0"),
        description="Equity snapshot used to size the trade.",
    )
    open_positions: int = Field(
        default=0,
        ge=0,
        description="Open-position count observed at decision time.",
    )
    trades_today: int = Field(default=0, ge=0, description="Trades opened today (count).")
    timestamp: datetime


# Stable block-reason strings for RiskManager. Mirrors the convention
# from decision.rule_fallback + decision.qualification.
REASON_NEWS_BLACKOUT = "news_blackout"
REASON_DAILY_LOSS_LIMIT = "daily_loss_limit"
REASON_WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
REASON_MAX_OPEN_EXPOSURE = "max_open_exposure"
REASON_MAX_TRADES_PER_SESSION = "max_trades_per_session"
REASON_OPPOSITE_POSITION = "opposite_position_open"
REASON_RISK_BAND_UNKNOWN = "risk_band_unknown"
REASON_NOT_QUALIFIED = "decision_not_qualified"
REASON_INVALID_QUALIFICATION = "invalid_qualification"


# ----------------------------------------------------------------- sizing


class SizingResult(BaseModel):
    """Output of :class:`xauusd_bot.execution.sizer.PositionSizer.size`.

    The executor should use ``volume_lots`` as the request volume; the
    rounding_mode tells the journal whether the size is a "natural"
    result of the formula or a snap-to-limits.
    """

    model_config = ConfigDict(extra="forbid")

    volume_lots: Decimal = Field(ge=0, description="Order volume in lots.")
    risk_per_lot: Decimal = Field(
        ge=0,
        description="USD at risk per 1.0 lot, given the SL distance and contract size.",
    )
    formula_used: str = Field(
        description="Human-readable formula string the sizer applied (e.g. 'lots = risk / (sl_dist * contract)')."
    )
    rounding_mode: SizingRoundingMode = Field(
        default=SizingRoundingMode.EXACT,
        description="How the calculated size was adjusted against min/max/step.",
    )
    sl_distance: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="SL distance in price units (USD/oz for XAUUSD), as supplied.",
    )
    risk_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Risk amount (USD) supplied to the formula.",
    )
    timestamp: datetime


# ----------------------------------------------------------------- stops / TPs


class StopsAndTPs(BaseModel):
    """Output of the Stop- and TakeProfit-Manager pair.

    The executor attaches ``sl_price`` and ``tp1_price`` / ``tp2_price`` /
    ``tp3_price`` to the :class:`~xauusd_bot.connectors.schemas.OrderRequest`
    on entry. ``trail_active`` tells the bot whether to start trailing
    immediately (False until the first structure break / TP1 hit).
    ``partial_close_plan`` is a list of "when price reaches X, close Y%"
    instructions the order manager will dispatch on TP hits.
    """

    model_config = ConfigDict(extra="forbid")

    sl_price: Decimal | None = Field(default=None, description="Initial SL price.")
    tp1_price: Decimal | None = Field(default=None, description="TP1 price (first partial close).")
    tp2_price: Decimal | None = Field(default=None, description="TP2 price (second partial close).")
    tp3_price: Decimal | None = Field(default=None, description="TP3 / runner price (final partial).")
    trail_active: bool = Field(default=False, description="True once trailing has been armed.")
    trailing_mode: TrailingMode = Field(
        default=TrailingMode.FIXED,
        description="Which trailing rule to apply after entry.",
    )
    partial_close_plan: list[dict[str, object]] = Field(
        default_factory=list,
        description=(
            "List of {level: 'tp1'|'tp2'|'tp3', price: Decimal, pct: float} entries. "
            "Sums to 100% across TP1+TP2+TP3 when fully armed."
        ),
    )
    reasoning: list[str] = Field(
        default_factory=list,
        description="Short deterministic strings explaining the SL/TP choices.",
    )
    timestamp: datetime


# ----------------------------------------------------------------- order management


class OrderEnvelope(BaseModel):
    """A submitted order + its connector result + lifecycle trace.

    The OrderManager constructs this around every :class:`OrderRequest`
    it forwards. The :class:`PendingOrderManager` tracks them by
    ``client_order_id`` (the idempotency key).
    """

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(description="Idempotency key. Same key → no double order.")
    setup_id: UUID = Field(description="TradeQualification.qualification_id this order belongs to.")
    strategy_version: str = Field(
        default="block4-v1",
        description="Identifier of the executor build (for journal analytics).",
    )
    engine_source: OrderTag = Field(
        default=OrderTag.RULE_BASED,
        description="Which decision source produced this trade (rule-based vs AI).",
    )
    symbol: str
    side: OrderSide
    type: OrderType
    requested_volume: Decimal = Field(ge=0)
    requested_price: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    state: Literal["pending", "submitted", "filled", "rejected", "cancelled", "expired"] = (
        "pending"
    )
    order_id: str | None = None
    filled_volume: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    slippage_points: Decimal | None = Field(
        default=None,
        description="(fill_price - requested_price) in points — for slippage analysis.",
    )
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    cancel_reason: str | None = Field(
        default=None,
        description="Why the PendingOrderManager cancelled this order (None if still alive).",
    )


class PendingSweepResult(BaseModel):
    """Outcome of one :meth:`PendingOrderManager.sweep` call."""

    model_config = ConfigDict(extra="forbid")

    swept_at: datetime
    examined: int = Field(ge=0, description="Pending orders inspected in this sweep.")
    kept: int = Field(ge=0, description="Pending orders that were kept alive.")
    cancelled: int = Field(ge=0, description="Pending orders that were cancelled.")
    cancel_reasons: dict[str, int] = Field(
        default_factory=dict,
        description="Count of cancellations per reason (e.g. {'structure_against': 2}).",
    )


# ----------------------------------------------------------------- emergency


class EmergencyStopState(BaseModel):
    """Persisted state of an active emergency-stop pause.

    Written to ``emergency_stop_state.json`` so that after a crash the
    :class:`EmergencyStopManager` can detect that the pause is still
    in effect and refuse to trade.
    """

    model_config = ConfigDict(extra="forbid")

    active: bool
    reason: str = Field(description="Stable reason string (e.g. 'volatility_spike', 'broker_disconnect').")
    triggered_at: datetime
    paused_until: datetime = Field(description="Pause expires at this UTC time.")
    triggered_by: Literal["auto", "manual"] = "auto"
    details: dict[str, str] = Field(default_factory=dict)


class EmergencyTrigger(str, Enum):
    """Stable trigger identifiers — used in :class:`EmergencyStopState.reason`."""

    SYSTEM_ERROR = "system_error"
    BROKER_DISCONNECT = "broker_disconnect"
    VOLATILITY_SPIKE = "volatility_spike"
    SLIPPAGE_SPIKE = "slippage_spike"
    MANUAL_KILL_SWITCH = "manual_kill_switch"


# ----------------------------------------------------------------- lifecycle


class ExecutionPhaseResult(BaseModel):
    """One phase in the trade-lifecycle (lifecycle-demo CLI output)."""

    model_config = ConfigDict(extra="forbid")

    phase: str = Field(description="Phase name, e.g. 'risk_approve', 'position_size', 'order_send'.")
    ok: bool
    detail: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    timestamp: datetime


class ExecutionLifecycleReport(BaseModel):
    """Top-level lifecycle report (one trade) — emitted by the lifecycle demo CLI."""

    model_config = ConfigDict(extra="forbid")

    setup_id: UUID
    qualification: TradeQualification
    risk: RiskVerdict | None = None
    sizing: SizingResult | None = None
    stops: StopsAndTPs | None = None
    order: OrderEnvelope | None = None
    pending: PendingSweepResult | None = None
    trail: StopsAndTPs | None = None
    partial_close: dict[str, object] | None = None
    exit: dict[str, object] | None = None
    phases: list[ExecutionPhaseResult] = Field(default_factory=list)
    timestamp: datetime


# ----------------------------------------------------------------- re-exports

__all__ = [
    "EmergencyStopState",
    "EmergencyTrigger",
    "ExecutionLifecycleReport",
    "ExecutionPhaseResult",
    "ExitReason",
    "OrderEnvelope",
    "OrderTag",
    "PendingSweepResult",
    "REASON_DAILY_LOSS_LIMIT",
    "REASON_INVALID_QUALIFICATION",
    "REASON_MAX_OPEN_EXPOSURE",
    "REASON_MAX_TRADES_PER_SESSION",
    "REASON_NEWS_BLACKOUT",
    "REASON_NOT_QUALIFIED",
    "REASON_OPPOSITE_POSITION",
    "REASON_RISK_BAND_UNKNOWN",
    "REASON_WEEKLY_LOSS_LIMIT",
    "RiskBand",
    "RiskVerdict",
    "SizingResult",
    "SizingRoundingMode",
    "StopsAndTPs",
    "TrailingMode",
    # re-exports
    "DecisionAction",
    "EntryType",
    "Position",
    "ScoreBand",
    "SymbolSpec",
]
