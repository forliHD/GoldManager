"""Pydantic schemas for the backtest layer (Block 5b).

These are the *output* contracts for the BacktestEngine and
WalkForwardEngine. They are deliberately separate from the
:class:`TradeRecord` journal schema (Block 5a) because:

* They carry aggregate statistics that are *computed* at the end of
  a backtest (Sharpe, max drawdown, R-distribution, etc.) — values
  that never get persisted into the trade-by-trade journal.
* The WalkForward windowing is a backtest-only concept.

Conventions
-----------
* Money / size fields use ``Decimal`` (no float drift).
* All numeric stats use ``float`` (Pydantic-friendly, plot-friendly,
  may be NaN-free by construction — see :func:`_safe_float` helpers
  in the engine).
* Time fields are timezone-aware UTC ``datetime``.
* All schemas are ``extra='forbid'`` — a missing/extra field is a bug.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from xauusd_bot.common.schemas.decision import EntryType, ScoreBand

# ----------------------------------------------------------------- helpers


# Stable ordering for breakdowns. Mirrors journal.queries convention.
_BAND_ORDER: list[ScoreBand] = [
    ScoreBand.BELOW_55,
    ScoreBand.OBSERVE_55_64,
    ScoreBand.PREPARE_65_74,
    ScoreBand.REDUCED_75_84,
    ScoreBand.FULL_85_PLUS,
]

_ENTRY_TYPE_ORDER: list[EntryType] = [EntryType.SCOUT, EntryType.REDUCED, EntryType.FULL]

_SESSION_ORDER: list[str] = ["asia", "london", "ny", "overlap", "closed"]


# ----------------------------------------------------------------- core stats


class BacktestStats(BaseModel):
    """Aggregate performance statistics from a backtest run.

    Every metric is computed by the engine from the closed-trade
    stream; nothing here is re-derived by the consumer. Empty
    (zero-trade) runs return 0.0 for every numeric field — never
    NaN — so JSON serialization stays clean.
    """

    model_config = ConfigDict(extra="forbid")

    n_trades: int = Field(ge=0, description="Total trades opened (open + closed).")
    n_closed: int = Field(ge=0, description="Number of trades with a realized PnL.")
    n_wins: int = Field(ge=0)
    n_losses: int = Field(ge=0)
    n_breakeven: int = Field(ge=0)
    winrate: float = Field(ge=0, le=1, description="wins / closed.")
    avg_r: float = Field(description="Mean R-multiple over closed trades.")
    total_r: float = Field(description="Sum of R-multiples (proxy for total expectancy).")
    profit_factor: float = Field(
        ge=0,
        description="Sum of positive PnL / sum of |negative PnL|. ∞ (no losers) → 0 by convention (no DB NaN).",
    )
    expectancy: float = Field(
        description="avg_r — mean R per trade. Empty input → 0.0.",
    )
    sharpe: float = Field(description="Annualized Sharpe (Block-5b default periods_per_year=252*8).")
    sortino: float = Field(description="Annualized Sortino (downside-deviation based).")
    max_drawdown: float = Field(ge=0, description="Peak-to-trough equity drop in USD.")
    max_drawdown_duration_bars: int = Field(
        ge=0,
        description="Bars from peak to trough (no recovery tracking — that's the equity-curve scanner's job).",
    )
    total_pnl: float = Field(description="Sum of realized PnL in USD.")
    final_equity: float = Field(description="Starting balance + total_pnl.")


# ----------------------------------------------------------------- breakdowns


class BreakdownEntry(BaseModel):
    """Per-bucket aggregate (used for setup / session / score-band breakdowns)."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    closed: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    breakeven: int = Field(ge=0)
    winrate: float = Field(ge=0, le=1)
    avg_r: float = Field(description="Mean R-multiple over closed trades.")
    total_r: float
    total_pnl: float


# ----------------------------------------------------------------- window


class WalkForwardWindow(BaseModel):
    """One (in_sample, out_of_sample) pair from a WalkForward run.

    Each window reports both segments' stats. ``oos_degradation_pct``
    is the simple relative drop in Sharpe from IS → OOS (positive =
    degradation, negative = OOS outperformed IS).
    """

    model_config = ConfigDict(extra="forbid")

    window_index: int = Field(ge=0)
    start_in: datetime
    end_in: datetime
    start_oos: datetime
    end_oos: datetime
    in_sample_stats: BacktestStats
    out_of_sample_stats: BacktestStats
    oos_degradation_pct: float = Field(
        description=(
            "(sharpe_is - sharpe_oos) / max(|sharpe_is|, 1.0) * 100. "
            ">30% = suspect overfitting, 0-30% = OK, <0% = OOS beat IS."
        ),
    )
    in_sample_sharpe: float
    out_of_sample_sharpe: float


# ----------------------------------------------------------------- top-level results


class BacktestResult(BaseModel):
    """The output of one :class:`BacktestEngine.run` call.

    All breakdowns use the same shape (:class:`BreakdownEntry`) and
    the same fixed key order so the JSON output is byte-stable for
    CI snapshot tests.
    """

    model_config = ConfigDict(extra="forbid")

    n_bars_processed: int = Field(ge=0)
    n_trades: int = Field(ge=0)
    start_date: datetime
    end_date: datetime
    runtime_seconds: float = Field(ge=0)

    equity_curve: list[tuple[datetime, Decimal]] = Field(
        default_factory=list,
        description=(
            "Full realized-PnL equity curve (in USD, starting from 0). The engine reports the "
            "raw sequence; consumers should sample for display. Empty list → no trades."
        ),
    )
    equity_curve_sample: list[tuple[datetime, Decimal]] = Field(
        default_factory=list,
        description="At most 20 evenly-spaced points from ``equity_curve`` for compact JSON.",
    )
    r_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="R-multiple histogram, fixed buckets: -3, -2, -1, 0, 1, 2, 3+.",
    )
    stats: BacktestStats
    setup_breakdown: dict[str, BreakdownEntry] = Field(default_factory=dict)
    session_breakdown: dict[str, BreakdownEntry] = Field(default_factory=dict)
    score_band_breakdown: dict[str, BreakdownEntry] = Field(default_factory=dict)
    tags: dict[str, str] = Field(
        default_factory=dict,
        description="Free-form metadata (slippage model name, spread model name, version tag).",
    )


class WalkForwardResult(BaseModel):
    """The output of one :class:`WalkForwardEngine.run` call.

    The ``robustness_matrix`` is a 2-column matrix of
    ``(in_sample_sharpe, out_of_sample_sharpe)`` per window — the
    canonical "IS vs OOS" scatter. The diagonal of the implied
    scatter is the "expected" line (IS == OOS); rows below the
    diagonal are degraded.
    """

    model_config = ConfigDict(extra="forbid")

    windows: list[WalkForwardWindow] = Field(default_factory=list)
    robustness_matrix: list[list[float]] = Field(
        default_factory=list,
        description=(
            "Per-window [is_sharpe, oos_sharpe] rows. Length == len(windows). Used "
            "to plot the IS-vs-OOS scatter for the OOS-Degradation report."
        ),
    )
    mean_oos_sharpe: float
    std_oos_sharpe: float
    oos_sharpe_degradation: float = Field(
        description=(
            "Aggregate OOS degradation = (mean IS Sharpe - mean OOS Sharpe) / "
            "max(|mean IS Sharpe|, 1.0) * 100. >30% = overfit flag."
        ),
    )
    is_overfit: bool = Field(
        description="True iff oos_sharpe_degradation > 30% (heuristic, see AGENTS.md §3 I-4 spirit).",
    )
    runtime_seconds: float = Field(ge=0)
    start_date: datetime
    end_date: datetime
    n_bars_processed: int = Field(ge=0)


# ----------------------------------------------------------------- enums


class BacktestPhase(str, Enum):
    """Internal phase label for engine logging / instrumentation.

    Exposed as a :class:`str`-Enum so it can be serialised to JSON
    and compared against plain strings in tests.
    """

    WARMUP = "warmup"
    DECISION = "decision"
    FILL = "fill"
    SETTLE = "settle"
    DONE = "done"


# ----------------------------------------------------------------- re-exports


__all__ = [
    "BacktestResult",
    "BacktestStats",
    "BacktestPhase",
    "BreakdownEntry",
    "WalkForwardResult",
    "WalkForwardWindow",
    # re-exports for downstream convenience
    "EntryType",
    "ScoreBand",
    # internal ordering (for test snapshot stability)
    "_BAND_ORDER",
    "_ENTRY_TYPE_ORDER",
    "_SESSION_ORDER",
]
