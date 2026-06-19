"""RuleBasedFallback — Block 3 Phase 2.

Sicherheitsautoritativ (per AGENTS.md §3 I-4). Consumes a
:class:`Score` + :class:`AggregatedFeatures` + :class:`AccountInfo`
and emits a :class:`Decision`.

Rule order (all evaluated, first blocker wins)
---------------------------------------------
1. ``news_blackout`` — news.in_blackout_flag → no_trade.
2. ``risk_limit_reached`` — daily_pnl / weekly_pnl breached → no_trade.
3. ``spread_too_wide`` — current_spread > spread_max_pips × 10 → no_trade.
4. ``score_below_65`` — score < 65 → no_trade (the band gate).
5. ``no_clear_direction`` — |long_dir - short_dir| weighted < 10 → no_trade.
6. Otherwise: action = enter_long / enter_short (from Score.direction),
   entry_type = full | reduced | scout (from Score.band).

I-4: Brain vs Hands
-------------------
RuleBasedFallback is allowed to consume :class:`AccountInfo` and
:class:`Settings` (they are safety primitives) but it does NOT
compute position size, SL, or TP. Those are Block 4.

I-3: Point-in-Time
------------------
The :class:`Score` and :class:`AggregatedFeatures` were both built
from a PIT-filtered snapshot. This module does no time filtering
itself.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    Decision,
    DecisionAction,
    EntryType,
    Score,
    ScoreBand,
)
from xauusd_bot.connectors.schemas import AccountInfo

log = structlog.get_logger(__name__)


# Stable block-reason strings. Tests assert on these exact values.
REASON_NEWS_BLACKOUT = "news_blackout"
REASON_RISK_LIMIT_REACHED = "risk_limit_reached"
REASON_SPREAD_TOO_WIDE = "spread_too_wide"
REASON_SCORE_BELOW_65 = "score_below_65"
REASON_NO_CLEAR_DIRECTION = "no_clear_direction"
REASON_NEUTRAL_DIRECTION = "neutral_direction"
REASON_BAND_OBSERVE = "band_observe_no_entry"
REASON_BAND_BELOW = "band_below_55_no_entry"

# Default long-vs-short direction spread threshold.
# |weighted_long_score - weighted_short_score| < 10 → no_clear_direction.
# A 10-point spread is the "barely different" floor.
_DEFAULT_DIRECTION_SPREAD = 10.0


class RuleBasedFallback:
    """Deterministic, safety-authoritative decision engine.

    Parameters
    ----------
    settings:
        The :class:`Settings` instance — used to read
        ``risk_max_daily``, ``risk_max_weekly``, ``spread_max_pips``.
    direction_spread_threshold:
        Minimum weighted |long - short| score to call a direction
        "clear". Default 10.
    """

    def __init__(
        self,
        settings: Settings,
        direction_spread_threshold: float = _DEFAULT_DIRECTION_SPREAD,
    ) -> None:
        self._settings = settings
        self._direction_spread = direction_spread_threshold

    def decide(
        self,
        score: Score,
        agg: AggregatedFeatures,
        account: AccountInfo | None = None,
    ) -> Decision:
        """Produce a :class:`Decision` from ``score`` + ``agg`` + ``account``.

        ``account`` is optional: if it's None, the daily/weekly PnL
        and spread checks degrade gracefully (they never block when
        the data is missing — the principle is "no info → no block",
        matching the optional fields on :class:`AccountInfo`).
        """

        ts: datetime = score.timestamp
        # -- (a) News blackout
        if agg.subscores.get("news") is not None and agg.subscores["news"].reasoning == "in_blackout":
            return Decision(
                action=DecisionAction.NO_TRADE,
                entry_type=None,
                block_reason=REASON_NEWS_BLACKOUT,
                source_score=score.total_score,
                source_band=score.band,
                source_direction=score.direction,
                timestamp=ts,
            )

        # -- (b) Risk limits
        if account is not None:
            block = _risk_limit_breached(account, self._settings)
            if block is not None:
                return Decision(
                    action=DecisionAction.NO_TRADE,
                    entry_type=None,
                    block_reason=REASON_RISK_LIMIT_REACHED,
                    source_score=score.total_score,
                    source_band=score.band,
                    source_direction=score.direction,
                    timestamp=ts,
                )

        # -- (c) Spread
        if account is not None and account.current_spread is not None:
            max_points = Decimal(str(self._settings.spread_max_pips)) * Decimal("10")
            if account.current_spread > max_points:
                return Decision(
                    action=DecisionAction.NO_TRADE,
                    entry_type=None,
                    block_reason=REASON_SPREAD_TOO_WIDE,
                    source_score=score.total_score,
                    source_band=score.band,
                    source_direction=score.direction,
                    timestamp=ts,
                )

        # -- (d) Band gate
        if score.band == ScoreBand.BELOW_55:
            return Decision(
                action=DecisionAction.NO_TRADE,
                entry_type=None,
                block_reason=REASON_BAND_BELOW,
                source_score=score.total_score,
                source_band=score.band,
                source_direction=score.direction,
                timestamp=ts,
            )
        if score.band == ScoreBand.OBSERVE_55_64:
            return Decision(
                action=DecisionAction.NO_TRADE,
                entry_type=None,
                block_reason=REASON_BAND_OBSERVE,
                source_score=score.total_score,
                source_band=score.band,
                source_direction=score.direction,
                timestamp=ts,
            )

        # -- (e) Direction clarity
        long_w, short_w = _weighted_direction_scores(agg)
        if abs(long_w - short_w) < self._direction_spread:
            return Decision(
                action=DecisionAction.NO_TRADE,
                entry_type=None,
                block_reason=REASON_NO_CLEAR_DIRECTION,
                source_score=score.total_score,
                source_band=score.band,
                source_direction=score.direction,
                timestamp=ts,
            )

        if score.direction == "neutral":
            return Decision(
                action=DecisionAction.NO_TRADE,
                entry_type=None,
                block_reason=REASON_NEUTRAL_DIRECTION,
                source_score=score.total_score,
                source_band=score.band,
                source_direction=score.direction,
                timestamp=ts,
            )

        # -- (f) Map band → entry_type, direction → action
        entry_type = _band_to_entry_type(score.band)
        action = (
            DecisionAction.ENTER_LONG if score.direction == "long" else DecisionAction.ENTER_SHORT
        )
        return Decision(
            action=action,
            entry_type=entry_type,
            block_reason=None,
            source_score=score.total_score,
            source_band=score.band,
            source_direction=score.direction,
            timestamp=ts,
        )


# ---------------------------------------------------------------- helpers


def _risk_limit_breached(account: AccountInfo, settings: Settings) -> str | None:
    """Return a non-None reason string if the daily/weekly PnL limit is breached.

    Daily / weekly limits are interpreted as **fractions of the
    current balance** (mirroring ``Settings.risk_max_daily`` /
    ``Settings.risk_max_weekly``). A loss of
    ``-risk_max_daily * balance`` is the "daily limit reached"
    threshold.
    """

    balance = float(account.balance)
    if balance <= 0:
        return None  # defensive: never block on a zero/negative balance
    if account.daily_pnl is not None:
        threshold = -settings.risk_max_daily * balance
        if float(account.daily_pnl) <= threshold:
            return "daily"
    if account.weekly_pnl is not None:
        threshold = -settings.risk_max_weekly * balance
        if float(account.weekly_pnl) <= threshold:
            return "weekly"
    return None


def _weighted_direction_scores(agg: AggregatedFeatures) -> tuple[float, float]:
    """Sum of (engine_weight × direction_bias == +1) and (engine_weight × direction_bias == -1).

    Returns (long_score, short_score) — both non-negative. Used by
    the direction-clarity check.
    """

    long_score = 0.0
    short_score = 0.0
    for sub in agg.subscores.values():
        if sub.direction_bias == 1:
            long_score += sub.weight
        elif sub.direction_bias == -1:
            short_score += sub.weight
    return long_score, short_score


def _band_to_entry_type(band: ScoreBand) -> EntryType:
    """Map :class:`ScoreBand` → :class:`EntryType`."""

    if band == ScoreBand.FULL_85_PLUS:
        return EntryType.FULL
    if band == ScoreBand.REDUCED_75_84:
        return EntryType.REDUCED
    return EntryType.SCOUT
