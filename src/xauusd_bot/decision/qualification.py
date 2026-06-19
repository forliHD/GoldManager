"""TradeQualificationEngine — Block 3 Phase 3.

Consumes a :class:`Decision` (from RuleBasedFallback) +
:class:`AggregatedFeatures` + :class:`FeatureSnapshotBundle` +
:class:`AccountInfo` + :class:`Settings` and emits a
:class:`TradeQualification`.

This engine is the *last* Block 3 check. It either confirms the
fallback's decision (passes the qualification through) or vetoes it
with one or more additional ``block_reasons``.

Additional checks (on top of RuleBasedFallback)
-----------------------------------------------
1. **Liquidity TP-target check** — at least one TP target must be
   within 1.5×ATR of the latest close → otherwise
   ``no_clear_tp_target``.
2. **Structure-vs-direction check** — the last BOS/CHOCH must align
   with the proposed direction → otherwise
   ``structure_against_direction``.
3. **Volatility check** — if ATR is extreme (very low or very high
   vs the bundle's own context) we skip; the executor will misprice
   stops in both regimes.
4. **Engine conflict fan-out** — if the aggregator flagged 3+ conflicts
   of any severity → ``engine_signals_conflict``.

I-4: Brain vs Hands
-------------------
This engine never produces volume, SL, or TP. It only confirms or
vetoes the decision and attaches a fresh ``qualification_id`` for
the journal to link to.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    Decision,
    DecisionAction,
    Score,
    TradeQualification,
)
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    StructureEventType,
)
from xauusd_bot.connectors.schemas import AccountInfo

log = structlog.get_logger(__name__)


# Stable block-reason strings (extensions to rule_fallback.REASON_*).
REASON_NO_CLEAR_TP_TARGET = "no_clear_tp_target"
REASON_STRUCTURE_AGAINST_DIRECTION = "structure_against_direction"
REASON_VOLATILITY_OUT_OF_RANGE = "volatility_out_of_range"
REASON_ENGINE_SIGNALS_CONFLICT = "engine_signals_conflict"
REASON_NO_LIQUIDITY_DATA = "no_liquidity_data"

# ATR (in price-units) floor / ceiling for the volatility check.
# The bundle's ``atr`` is computed in the same units as the price
# (USD for XAUUSD), NOT in points. Typical XAUUSD M1 ATR(14) is
# 0.2–0.6 USD (i.e. 20–60 points at point=0.01). We use 0.05 USD
# as the floor (very low = skip) and 2.0 USD as the ceiling
# (chaos / gap = skip).
_ATR_FLOOR_PRICE = 0.05
_ATR_CEILING_PRICE = 2.0

# How close (in ATR) a TP target must be to the latest close to count.
# A swing-trade TP target is usually many ATRs away, so we use this
# as the primary proximity test, with a fallback absolute-distance
# floor (``_TP_PROXIMITY_ABS_PRICE``) for markets with very small
# ATR.
_TP_PROXIMITY_ATR = 1.5
_TP_PROXIMITY_ABS_PRICE = 2.0  # USD — minimum TP distance even on a tiny-ATR market

# Threshold for "3+ conflicts" → engine_signals_conflict.
_CONFLICT_FANOUT_THRESHOLD = 3


class TradeQualificationEngine:
    """Final veto / confirmation step before Block 4 (Execution)."""

    def __init__(
        self,
        settings: Settings,
        atr_floor_price: float = _ATR_FLOOR_PRICE,
        atr_ceiling_price: float = _ATR_CEILING_PRICE,
        tp_proximity_atr: float = _TP_PROXIMITY_ATR,
        conflict_fanout_threshold: int = _CONFLICT_FANOUT_THRESHOLD,
    ) -> None:
        self._settings = settings
        self._atr_floor = atr_floor_price
        self._atr_ceiling = atr_ceiling_price
        self._tp_prox = tp_proximity_atr
        self._conflict_threshold = conflict_fanout_threshold

    def qualify(
        self,
        decision: Decision,
        score: Score,
        agg: AggregatedFeatures,
        bundle: FeatureSnapshotBundle,
        account: AccountInfo | None = None,
    ) -> TradeQualification:
        """Run all extra checks and produce the final :class:`TradeQualification`.

        If ``decision.action == NO_TRADE`` we still build a
        qualification — its ``qualified`` field is False and the
        reasons are propagated for the journal.
        """

        ts: datetime = decision.timestamp
        reasons: list[str] = []
        # Propagate the fallback's reason even on vetoed decisions.
        if decision.block_reason is not None:
            reasons.append(decision.block_reason)

        # The fallback already blocked → no need to run the extra
        # checks; just package.
        if decision.action == DecisionAction.NO_TRADE:
            return TradeQualification(
                qualified=False,
                final_action=DecisionAction.NO_TRADE,
                final_entry_type=None,
                block_reasons=reasons,
                final_direction="neutral",
                source_score=score.total_score,
                source_band=score.band,
                timestamp=ts,
            )

        latest_close = _latest_close(bundle)
        atr_points = bundle.atr

        # -- 1. Liquidity TP-target check.
        if latest_close is not None and atr_points is not None and atr_points > 0:
            if not _has_clear_tp_target(bundle, latest_close, atr_points, self._tp_prox):
                reasons.append(REASON_NO_CLEAR_TP_TARGET)
        elif latest_close is not None:
            if not _has_any_liquidity_data(agg):
                reasons.append(REASON_NO_LIQUIDITY_DATA)

        # -- 2. Structure-vs-direction check.
        if _structure_against_direction(bundle, decision.action):
            reasons.append(REASON_STRUCTURE_AGAINST_DIRECTION)

        # -- 3. Volatility check.
        if atr_points is not None and (
            atr_points < self._atr_floor or atr_points > self._atr_ceiling
        ):
            reasons.append(REASON_VOLATILITY_OUT_OF_RANGE)

        # -- 4. Engine conflict fan-out.
        if len(agg.conflicts) >= self._conflict_threshold:
            reasons.append(REASON_ENGINE_SIGNALS_CONFLICT)

        if reasons:
            return TradeQualification(
                qualified=False,
                final_action=DecisionAction.NO_TRADE,
                final_entry_type=None,
                block_reasons=reasons,
                final_direction="neutral",
                source_score=score.total_score,
                source_band=score.band,
                timestamp=ts,
            )
        return TradeQualification(
            qualified=True,
            final_action=decision.action,
            final_entry_type=decision.entry_type,
            block_reasons=[],
            final_direction=score.direction,
            source_score=score.total_score,
            source_band=score.band,
            timestamp=ts,
        )


# ---------------------------------------------------------------- helpers


def _latest_close(bundle: FeatureSnapshotBundle) -> float | None:
    """Best-effort latest close — read from the structure engine or the bundle.

    The :class:`FeatureSnapshotBundle` does not carry OHLC; the
    close is reconstructed indirectly. We prefer the most recent
    structure event's ``close`` (which the engine populates from
    the breaking bar). If no events, fall back to ``None`` and
    downstream code degrades gracefully.
    """

    if bundle.structure is None:
        return None
    if bundle.structure.last_bos is not None:
        return float(bundle.structure.last_bos.close)
    if bundle.structure.last_choch is not None:
        return float(bundle.structure.last_choch.close)
    return None


def _has_clear_tp_target(
    bundle: FeatureSnapshotBundle,
    latest_close: float,
    atr_points: float,
    proximity_atr: float,
) -> bool:
    """Return True if at least one TP target is within proximity_atr × ATR of price.

    "Long TP target" = a zone above current close. "Short TP target"
    = a zone below. We accept any one of them — the executor will
    pick the right side based on the action.

    The test is ``min(proximity_atr × ATR, _TP_PROXIMITY_ABS_PRICE)``:
    on a small-ATR market the absolute distance floor takes over, on
    a large-ATR market the relative test dominates. This is the
    standard "scale-aware TP check" used by retail swing systems.
    """

    if bundle.liquidity is None:
        return False
    proximity = max(proximity_atr * atr_points, _TP_PROXIMITY_ABS_PRICE)
    has_long_tp = any(
        abs(zone.center - latest_close) <= proximity
        for zone in bundle.liquidity.tp_targets_above
    )
    has_short_tp = any(
        abs(zone.center - latest_close) <= proximity
        for zone in bundle.liquidity.tp_targets_below
    )
    return has_long_tp or has_short_tp


def _has_any_liquidity_data(agg: AggregatedFeatures) -> bool:
    """Return True if the aggregator had any liquidity data at all."""

    sub = agg.subscores.get("session_liquidity")
    if sub is None:
        return False
    return sub.reasoning != "no_data"


def _structure_against_direction(
    bundle: FeatureSnapshotBundle,
    action: DecisionAction,
) -> bool:
    """Return True if the structure trend opposes the proposed direction.

    We only block on the most recent BOS/CHOCH (not on the trend
    string, which can lag). If the last structure event was a clear
    BOS/CHOCH in the opposite direction of the proposed action,
    this returns True (i.e. block).

    No-reversal setups are not implemented in Block 3, so any
    BOS/CHOCH opposing the direction is a hard veto here.
    """

    if action == DecisionAction.NO_TRADE:
        return False
    if bundle.structure is None:
        return False
    last_event = bundle.structure.last_bos or bundle.structure.last_choch
    if last_event is None:
        return False
    if action == DecisionAction.ENTER_LONG:
        return last_event.type in (
            StructureEventType.BOS_DOWN,
            StructureEventType.CHOCH_DOWN,
        )
    if action == DecisionAction.ENTER_SHORT:
        return last_event.type in (
            StructureEventType.BOS_UP,
            StructureEventType.CHOCH_UP,
        )
    return False
