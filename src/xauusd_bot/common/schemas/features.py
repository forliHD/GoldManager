"""Pydantic schemas for the Feature-Engine outputs.

Each engine in :mod:`xauusd_bot.features` emits a strongly-typed output
schema defined here (or co-located with the engine if it's a single-engine
specific type). The aggregator in Block 3 will combine these into a
:class:`xauusd_bot.common.schemas.events.FeatureSnapshot` and emit it
downstream.

Conventions
-----------
* All numeric fields that represent prices use ``Decimal`` (no float drift).
* All numeric fields that represent derived/composite metrics (scores,
  distances, ratios) use ``float`` (Pydantic-friendly, plotting-friendly).
* Time fields are timezone-aware UTC ``datetime``.
* ``extra='forbid'`` everywhere — a missing/extra field is a bug, not a
  convenience.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- session


class SessionName(str, Enum):
    """Trading-session classification (UTC)."""

    ASIA = "asia"
    LONDON = "london"
    NY = "ny"
    OVERLAP = "overlap"
    CLOSED = "closed"


class SessionEngineOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.session.SessionEngine`.

    Notes
    -----
    * ``session_open/high/low`` are computed **only from bars with
      ``close_time <= current_t``** (point-in-time). Anything still
      in-progress is not used.
    * ``session_progress_pct`` is ``0.0`` outside the session and
      ``100.0`` at the end. Strictly clamped to [0, 100].
    * ``is_session_sweep`` is True if the latest bar's ``high`` swept
      the session high (or ``low`` swept the session low) and the bar
      *closed back inside* — a textbook liquidity sweep.
    * ``equal_highs/equal_lows_flag`` is True if the session has at
      least two swing points within ``0.5 * atr`` of each other (a
      double-top / double-bottom setup).
    """

    model_config = ConfigDict(extra="forbid")

    current_session: SessionName
    session_start: datetime
    session_end: datetime
    session_open: Decimal | None = Field(default=None, description="Open of the first bar of the session.")
    session_high: Decimal | None = Field(default=None, description="Max high so far this session.")
    session_low: Decimal | None = Field(default=None, description="Min low so far this session.")
    session_progress_pct: float = Field(ge=0, le=100)
    is_session_sweep: bool = Field(default=False, description="True if latest bar swept session H/L and reversed.")
    equal_highs_flag: bool = Field(default=False, description="Two+ swing highs within 0.5*ATR.")
    equal_lows_flag: bool = Field(default=False, description="Two+ swing lows within 0.5*ATR.")
    session_risk_factor: float = Field(
        ge=0,
        le=2,
        description="Risk multiplier for the session (Asia=0.5, London/NY=1.0, Overlap=0.7, Closed=0.3).",
    )


# ---------------------------------------------------------------- vwap


class VWAPLevel(str, Enum):
    """Which of the three anchored VWAPs."""

    UTC00 = "utc00"
    UTC07 = "utc07"
    UTC12 = "utc12"


class VWAPLevelOutput(BaseModel):
    """Single anchored VWAP and its derived features."""

    model_config = ConfigDict(extra="forbid")

    level: VWAPLevel
    value: float | None = Field(default=None, description="Current VWAP value (None if no bars yet).")
    distance_points: float | None = Field(default=None, description="close - vwap in points.")
    distance_atr: float | None = Field(default=None, description="(close - vwap) / ATR.")
    distance_percentile_30d: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Where this distance sits vs the last 30 days of distances (percentile).",
    )
    cross_up: bool = Field(default=False, description="True if the latest bar closed above VWAP after being below.")
    cross_down: bool = Field(default=False, description="True if the latest bar closed below VWAP after being above.")
    reclaim: bool = Field(default=False, description="Close > VWAP after a cross-down.")
    loss: bool = Field(default=False, description="Close < VWAP after a cross-up.")
    n_bars_anchored: int = Field(ge=0, description="Number of bars contributing to this VWAP.")


class TripleVWAPOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.vwap.TripleVWAPEngine`."""

    model_config = ConfigDict(extra="forbid")

    levels: dict[str, VWAPLevelOutput] = Field(
        default_factory=dict, description="Keys: 'utc00', 'utc07', 'utc12'."
    )
    cluster_within_atr: float = Field(
        default=1.5,
        ge=0,
        description="ATR multiplier for the 'cluster' test (default 1.5).",
    )
    is_cluster: bool = Field(default=False, description="All 3 VWAPs within 1.5x ATR of each other.")
    cluster_center: float | None = Field(default=None, description="Mean of the 3 VWAPs (None if any missing).")


# ---------------------------------------------------------------- volume range


class VolumeProfileName(str, Enum):
    """Higher-timeframe volume profile."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class VolumeProfileState(str, Enum):
    """locked (period complete) vs developing (period still open)."""

    LOCKED = "locked"
    DEVELOPING = "developing"
    EMPTY = "empty"  # no bars in this period yet


class ValueAreaStatus(str, Enum):
    """Where the current price sits relative to the value area."""

    BELOW_VALUE = "below_value"
    WITHIN_VALUE = "within_value"
    ABOVE_VALUE = "above_value"


class VolumeProfileOutput(BaseModel):
    """Output of a single profile (weekly/monthly/yearly)."""

    model_config = ConfigDict(extra="forbid")

    name: VolumeProfileName
    state: VolumeProfileState
    period_start: datetime
    period_end: datetime
    bin_size: float = Field(gt=0, description="Gold-points per bin (e.g. 1.0 for monthly).")
    vah: float | None = Field(default=None, description="Value Area High.")
    vpoc: float | None = Field(default=None, description="Volume Point of Control.")
    val: float | None = Field(default=None, description="Value Area Low.")
    value_area_pct: float = Field(default=0.70, ge=0.0, le=1.0, description="Value-area share (default 0.70).")
    distance_to_vah_points: float | None = Field(default=None)
    distance_to_vah_atr: float | None = Field(default=None)
    distance_to_val_points: float | None = Field(default=None)
    distance_to_val_atr: float | None = Field(default=None)
    distance_to_vpoc_points: float | None = Field(default=None)
    distance_to_vpoc_atr: float | None = Field(default=None)
    value_status: ValueAreaStatus | None = Field(
        default=None, description="Where current price sits vs the value area."
    )
    acceptance_count: int = Field(default=0, ge=0, description="Closes within the value area in the period.")
    rejection_count: int = Field(default=0, ge=0, description="Closes outside the value area in the period.")
    rotation: bool = Field(default=False, description="Price migrated through the value area without breaking out.")
    breakout: bool = Field(default=False, description="Price closed decisively outside the value area.")
    n_bars: int = Field(default=0, ge=0, description="Number of M1 bars contributing to this profile.")


class VolumeRangeOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.volume_range.FixedVolumeRangeEngine`."""

    model_config = ConfigDict(extra="forbid")

    weekly: VolumeProfileOutput
    monthly: VolumeProfileOutput
    yearly: VolumeProfileOutput
    daily: VolumeProfileOutput | None = Field(
        default=None, description="Developing current-day profile (state developing)."
    )
    prev_day: VolumeProfileOutput | None = Field(
        default=None, description="Frozen previous-day profile (yesterday, state locked)."
    )
    prev_week: VolumeProfileOutput | None = Field(
        default=None, description="Frozen previous-week profile (state always locked)."
    )
    prev_month: VolumeProfileOutput | None = Field(default=None)
    prev_year: VolumeProfileOutput | None = Field(default=None)
    cluster_within_atr: float = Field(default=0.5, ge=0, description="ATR multiplier for the lock↔develop test.")
    # Map of (current profile level) → list of (locked-profile, level name) matches within cluster_within_atr.
    # Example key: "weekly.vpoc", example value: [{"matches": "prev_month.val", "distance_atr": 0.21}].
    developing_vs_locked_clusters: dict[str, list[dict[str, object]]] = Field(default_factory=dict)


# ---------------------------------------------------------------- fvg


class FVGType(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class FVGStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_MITIGATED = "partially_mitigated"
    MITIGATED = "mitigated"


class FVGZone(BaseModel):
    """A single Fair Value Gap zone."""

    model_config = ConfigDict(extra="forbid")

    tf: Literal["H1", "M5", "M1"]
    type: FVGType
    top: float
    bottom: float
    size_points: float = Field(gt=0, description="top - bottom (in gold points).")
    created_at: datetime
    age_seconds: int = Field(ge=0)
    displacement_atr: float = Field(
        default=0.0,
        ge=0,
        description="ATR multiple of the displacement bar at zone creation.",
    )
    status: FVGStatus
    mitigation_pct: float = Field(default=0.0, ge=0, le=100, description="How much of the zone has been filled (0..100).")
    rank_score: float = Field(default=0.0, ge=0, description="Composite rank (size × freshness × displacement).")
    extended_bottom: float | None = Field(
        default=None,
        description=(
            "Demand zones (bullish H1 FVG): the zone bottom extended DOWN to the "
            "impulse-origin fractal — the H1 swing low if the origin candle forms a "
            "fractal, otherwise the M5 swing low that launched the impulse. None = no "
            "extension; the raw FVG bottom is the zone edge. The effective demand range "
            "is [extended_bottom or bottom, top]."
        ),
    )
    extended_top: float | None = Field(
        default=None,
        description=(
            "Supply zones (bearish H1 FVG): the zone top extended UP to the "
            "impulse-origin fractal (H1 swing high, else the M5 swing high). None = no "
            "extension. The effective supply range is [bottom, extended_top or top]."
        ),
    )
    extension_tf: Literal["H1", "M5"] | None = Field(
        default=None,
        description=(
            "Timeframe of the fractal the zone was extended to: 'H1' when the impulse "
            "origin is itself an H1 fractal, 'M5' when the H1 origin is only a wick and "
            "the precise fractal was found by dropping to M5."
        ),
    )


class FVGOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.fvg.FVGEngine`."""

    model_config = ConfigDict(extra="forbid")

    zones: list[FVGZone] = Field(default_factory=list)
    top_zones: list[FVGZone] = Field(default_factory=list, description="Top-3 by rank_score, sorted desc.")


# ---------------------------------------------------------------- market structure


class StructureEventType(str, Enum):
    BOS_UP = "bos_up"
    BOS_DOWN = "bos_down"
    CHOCH_UP = "choch_up"
    CHOCH_DOWN = "choch_down"


class SwingPoint(BaseModel):
    """A swing high or low."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["high", "low"]
    price: float
    time: datetime
    bar_index: int = Field(ge=0)
    is_external: bool = Field(default=True, description="External = the dominant swing on the current TF.")


class StructureEvent(BaseModel):
    """A single BOS/CHOCH event."""

    model_config = ConfigDict(extra="forbid")

    type: StructureEventType
    level: float = Field(description="The swing level that was broken.")
    time: datetime
    bar_index: int = Field(ge=0)
    close: float
    distance_atr: float = Field(ge=0, description="Distance from level to breaking close, in ATR.")


class LiquidityPool(BaseModel):
    """An un-tested or freshly-swept liquidity pool."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["high", "low"]
    price: float
    created_at: datetime
    swept: bool = Field(default=False, description="True if the pool has been swept (price wicked through and reversed).")
    sweep_time: datetime | None = Field(default=None)


class MarketStructureOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.structure.MarketStructureEngine`."""

    model_config = ConfigDict(extra="forbid")

    swings: list[SwingPoint] = Field(default_factory=list)
    last_bos: StructureEvent | None = Field(default=None)
    last_choch: StructureEvent | None = Field(default=None)
    liquidity_pools: list[LiquidityPool] = Field(default_factory=list)
    trend: Literal["up", "down", "range"] = Field(default="range")
    fractal_n: int = Field(ge=1, description="N-bar fractal period used for swing detection.")


# ---------------------------------------------------------------- momentum


class CandleMomentumPerBar(BaseModel):
    """Quantitative candle-momentum features for a single bar."""

    model_config = ConfigDict(extra="forbid")

    body_size_atr: float = Field(description="abs(close-open) / ATR.")
    wick_body_ratio: float = Field(ge=0, description="(high-low - body) / body, or 0 if body is 0.")
    close_position: float = Field(ge=0, le=1, description="(close-low) / (high-low), or 0.5 if doji.")
    displacement: bool = Field(default=False, description="body > 2x ATR or body > 1.5x median body.")
    impulsive_follow_through: int = Field(default=0, ge=0, description="Consecutive bars in the same direction.")
    tick_volume_percentile: float = Field(
        default=50.0,
        ge=0,
        le=100,
        description="This bar's tick_volume percentile vs last 100 bars (relative, AGENTS.md I-5).",
    )
    tick_volume: float = Field(
        default=0.0,
        ge=0,
        description="This bar's RAW tick_volume (absolute participation). A percentile of 0 means "
        "'quietest of the last 100 bars', NOT zero volume — read this to tell low participation "
        "(price drifting level-to-level) from a genuine reaction.",
    )


class CandleMomentumOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.momentum.CandleMomentumEngine`."""

    model_config = ConfigDict(extra="forbid")

    by_tf: dict[str, CandleMomentumPerBar] = Field(default_factory=dict)
    score: float = Field(ge=0, le=100, description="Aggregate 0-100 momentum score (weighted mean).")


# ---------------------------------------------------------------- liquidity


class LiquidityZone(BaseModel):
    """A clustered liquidity pool (TP target or SL trap)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["high", "low"]
    price_low: float = Field(description="Lower bound of the zone.")
    price_high: float = Field(description="Upper bound of the zone.")
    center: float = Field(description="Midpoint of the zone.")
    pool_count: int = Field(ge=1)
    is_sl_trap: bool = Field(default=False, description="Cluster of likely SL stops.")


class LiquidityEngineOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.liquidity.LiquidityEngine`."""

    model_config = ConfigDict(extra="forbid")

    tp_targets_above: list[LiquidityZone] = Field(default_factory=list)
    tp_targets_below: list[LiquidityZone] = Field(default_factory=list)
    sl_protection_zones: list[LiquidityZone] = Field(default_factory=list)


# ---------------------------------------------------------------- news


class NewsImpact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NewsEvent(BaseModel):
    """A single news event (calendar entry)."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    currency: str = Field(description="ISO 4217 currency code (e.g. 'USD').")
    title: str
    impact: NewsImpact
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None


class NewsContextOutput(BaseModel):
    """Output of :class:`xauusd_bot.features.news.NewsContextEngine`."""

    model_config = ConfigDict(extra="forbid")

    minutes_until_next_high_impact: float | None = Field(default=None, ge=0)
    in_blackout_flag: bool = Field(default=False)
    next_high_impact: NewsEvent | None = Field(default=None)
    upcoming_events: list[NewsEvent] = Field(default_factory=list)
    surprise_score: float = Field(default=0.0, ge=-100, le=100, description="Placeholder for live-data surprise score.")


# ---------------------------------------------------------------- master snapshot


class FibRetracementOutput(BaseModel):
    """Fibonacci retracement of the last H1 impulse leg.

    The strategy's structural entry filter (decision_agent.md §2): where does
    the current price sit inside the last H1 impulse? Preferred reaction is the
    golden pocket (0.5–0.618), best when it overlaps an FVG + supply/demand.
    Shallow reactions (0.382 / 0.236) suit strong / extreme trends; pullbacks
    deeper than 0.618 raise the odds of a trend change.

    ``retracement_pct`` is how far price retraced into the leg: 0.0 = at the
    impulse extreme (no pullback), 1.0 = all the way back to the leg's origin.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Literal["up", "down", "none"] = Field(
        default="none", description="Impulse leg direction (up = low→high)."
    )
    leg_low: float | None = Field(default=None, description="Low of the last H1 impulse leg.")
    leg_high: float | None = Field(default=None, description="High of the last H1 impulse leg.")
    fib_236: float | None = Field(default=None)
    fib_382: float | None = Field(default=None)
    fib_500: float | None = Field(default=None)
    fib_618: float | None = Field(default=None)
    retracement_pct: float | None = Field(
        default=None, description="How far price retraced into the leg (0=extreme, 1=origin)."
    )
    price_zone: Literal["shallow", "0.382", "golden_pocket", "deep", "extended", "none"] = Field(
        default="none", description="Which fib bracket the current price sits in."
    )
    in_golden_pocket: bool = Field(
        default=False, description="Price within 0.5–0.618 of the last H1 impulse."
    )
    trend_strength: Literal["strong", "weak", "none"] = Field(
        default="none", description="Impulse leg size vs H1 ATR (strong = >= strong_atr_mult)."
    )


class VolumeTrendOutput(BaseModel):
    """Tick-volume trend on M1 — for the AI's volume-confirmation step.

    Captures the strategy's volume read: a *weakening* volume slope into a
    zone/consolidation, then a *spike* on the reaction/breakout candle.

    Settings note (validated on real XAUUSD M1, 2026-06-20): the MA9/MA20
    crossover is too noisy on M1 to be a regime signal (~120 flips/day), so
    the trend uses the **slope of the fast MA** over a short lookback, and the
    spike uses **last_volume / MA20 > spike_mult** (≈ 3 genuine spikes/day at
    2.0×). MA9 + MA20 are still exposed because they match the operator's MT5
    chart overlay.
    """

    model_config = ConfigDict(extra="forbid")

    ma_fast: float | None = Field(default=None, description="Fast MA of tick_volume (default 9).")
    ma_slow: float | None = Field(default=None, description="Slow MA of tick_volume (default 20).")
    last_volume: float | None = Field(default=None, description="Most recent bar's tick_volume.")
    spike_ratio: float | None = Field(
        default=None, description="last_volume / ma_slow (>1 = above the 20-bar average)."
    )
    is_spike: bool = Field(default=False, description="spike_ratio > spike_mult (default 2.0).")
    trend: Literal["rising", "falling", "flat"] = Field(
        default="flat", description="Slope of the fast MA over the lookback (falling = weakening volume)."
    )
    slope_pct: float | None = Field(
        default=None, description="% change of the fast MA over the slope lookback."
    )


class FeatureSnapshotBundle(BaseModel):
    """All engine outputs combined into one snapshot (Phase 10 smoke output)."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    session: SessionEngineOutput | None = None
    vwap: TripleVWAPOutput | None = None
    volume_range: VolumeRangeOutput | None = None
    fvg: FVGOutput | None = None
    structure: MarketStructureOutput | None = None
    momentum: CandleMomentumOutput | None = None
    liquidity: LiquidityEngineOutput | None = None
    news: NewsContextOutput | None = None
    volume_trend: VolumeTrendOutput | None = None
    fib: FibRetracementOutput | None = None
    atr: float | None = Field(default=None, ge=0, description="ATR(M1, 14) — the runtime ATR used by all engines.")
    price: float | None = Field(
        default=None,
        description="Latest M1 close at snapshot time — lets the AI layer judge 'are we in the zone?' precisely.",
    )
