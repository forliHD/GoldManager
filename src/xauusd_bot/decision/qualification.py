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
# The bundle's ``atr`` is computed in the same units as the price (USD for
# XAUUSD). The old 2.0 USD ceiling was mis-calibrated: gold routinely runs
# ATR(14) ≈ 1.5–3 USD in clean trending moves (not chaos), and clipping at 2.0
# vetoed exactly the volatile setups we want. 5.0 USD only rejects true gaps.
_ATR_FLOOR_PRICE = 0.05
_ATR_CEILING_PRICE = 5.0

# TP-target REACH (not proximity). A take-profit lives at a *distance* in the
# trade direction — that distance IS the profit — so the qualification just
# confirms a directional liquidity target exists at a usable range:
#   min(reach) = max(_TP_MIN_ATR × ATR, _TP_MIN_ABS_PRICE)   # far enough to be worth it
#   max(reach) = _TP_MAX_ATR × ATR                            # sanity cap (not the moon)
# The old check required a target WITHIN ~2 USD of price, which rejected every
# real swing target (e.g. a pool 8 USD away) → chronic ``no_clear_tp_target``.
_TP_MIN_ATR = 1.0
_TP_MIN_ABS_PRICE = 2.0  # USD floor on tiny-ATR markets
_TP_MAX_ATR = 40.0

# Threshold for "3+ conflicts" → engine_signals_conflict.
_CONFLICT_FANOUT_THRESHOLD = 3


class TradeQualificationEngine:
    """Final veto / confirmation step before Block 4 (Execution)."""

    def __init__(
        self,
        settings: Settings,
        atr_floor_price: float = _ATR_FLOOR_PRICE,
        atr_ceiling_price: float = _ATR_CEILING_PRICE,
        tp_min_atr: float = _TP_MIN_ATR,
        tp_min_abs_price: float = _TP_MIN_ABS_PRICE,
        tp_max_atr: float = _TP_MAX_ATR,
        conflict_fanout_threshold: int = _CONFLICT_FANOUT_THRESHOLD,
    ) -> None:
        self._settings = settings
        self._atr_floor = atr_floor_price
        self._atr_ceiling = atr_ceiling_price
        self._tp_min_atr = tp_min_atr
        self._tp_min_abs = tp_min_abs_price
        self._tp_max_atr = tp_max_atr
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

        # -- 1. Liquidity TP-target check (directional reach, not proximity).
        if latest_close is not None and atr_points is not None and atr_points > 0:
            if not _has_clear_tp_target(
                bundle, latest_close, atr_points, decision.action,
                min_atr=self._tp_min_atr, min_abs=self._tp_min_abs, max_atr=self._tp_max_atr,
            ):
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
    price: float,
    atr_points: float,
    action: DecisionAction,
    *,
    min_atr: float,
    min_abs: float,
    max_atr: float,
) -> bool:
    """Return True if a take-profit target exists IN THE TRADE DIRECTION at usable reach.

    A TP is the *profit*, so it lives at a distance — not glued to price. For a
    SHORT we look at liquidity pools BELOW price; for a LONG, pools ABOVE. A
    target qualifies if its distance is within ``[max(min_atr·ATR, min_abs),
    max_atr·ATR]`` — far enough to be worth taking, near enough to be reachable.
    The executor then aims at the nearest qualifying pool.

    (The old check required a pool WITHIN ~2 USD of price and ignored direction,
    which rejected every real swing target — chronic ``no_clear_tp_target``.)
    """

    if bundle.liquidity is None:
        return False
    if action == DecisionAction.ENTER_SHORT:
        targets = bundle.liquidity.tp_targets_below  # take profit below
    elif action == DecisionAction.ENTER_LONG:
        targets = bundle.liquidity.tp_targets_above  # take profit above
    else:
        return True  # NO_TRADE is handled upstream; nothing to validate
    min_dist = max(min_atr * atr_points, min_abs)
    max_dist = max_atr * atr_points
    return any(min_dist <= abs(zone.center - price) <= max_dist for zone in targets)


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
