"""AIDecisionOrchestrator — Block 6 Phase 3.

The orchestrator sits between the Block-3 decision stack and the
:class:`AIDecisionLayer`. It is the single entry point for the rest
of the engine (Block-3 → Block-4) to consult for a decision.

Pipeline
--------
1. **Score gate** — if ``score.total < settings.ai_layer_score_threshold``
   (default 65), the LLM is NOT consulted. The
   :class:`RuleBasedFallback` decides directly. This saves cost
   and latency on the majority of bars (where score < 65).
2. **API-key gate** — if ``settings.openrouter_api_key`` is None,
   the AI layer is disabled and the fallback decides.
3. **Master switch** — if ``settings.ai_layer_enabled`` is False,
   the fallback decides.
4. **News-blackout gate** — if the snapshot or score says
   ``news_in_blackout``, the fallback decides (hard rule, the
   LLM has no chance).
5. **LLM call** — invoke :meth:`AIDecisionLayer.decide` with a
   single retry on validation / zone / hard-rule errors. On
   the second failure, log a :class:`LLMFallbackDiscrepancy` to
   the journal (if a store is available) and return the
   fallback's :class:`Decision`.

Mapping LLM decision → Block-3 Decision
---------------------------------------
The LLM emits one of six values. The orchestrator maps:

* ``"no_trade"``         → :class:`DecisionAction.NO_TRADE`
* ``"watch"`` / ``"prepare"`` → :class:`DecisionAction.NO_TRADE` with
  ``block_reason="llm_watch_or_prepare"`` (the LLM is saying
  "no actionable setup *yet*"; we keep this as no_trade for
  Block 4's safety).
* ``"scout"``            → :class:`DecisionAction.ENTER_LONG/_SHORT`
  with :class:`EntryType.SCOUT`
* ``"reduced_entry"``    → :class:`DecisionAction.ENTER_LONG/_SHORT`
  with :class:`EntryType.REDUCED`
* ``"full_entry"``       → :class:`DecisionAction.ENTER_LONG/_SHORT`
  with :class:`EntryType.FULL`

LLM veto semantics
------------------
The LLM is allowed to VETO a setup the fallback would have
approved (e.g. the LLM says ``"no_trade"`` for a confluence
reason that the deterministic stack missed). Per I-4, the
fallback is safety-authoritative — it can VETO the LLM, but the
LLM can also say "no" on top of a "yes" from the fallback. The
:class:`LLMFallbackDiscrepancy` record captures the disagreement.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import asyncio

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.ai_decision import (
    LLMDecision,
    REASON_HARD_RULE_VIOLATION,
    REASON_NEWS_BLACKOUT,
    REASON_NO_API_KEY,
    REASON_SCORE_BELOW_THRESHOLD,
    REASON_TIMEOUT,
    REASON_VALIDATION_ERROR,
    REASON_ZONE_VIOLATION,
)
from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    Decision,
    DecisionAction,
    EntryType,
    Score,
    ScoreBand,
)
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.common.schemas.journal import (
    DiscrepancyResolutionTag,
    LLMFallbackDiscrepancy,
    LLMFallbackDiscrepancyV2,
)
from xauusd_bot.connectors.schemas import AccountInfo
from xauusd_bot.decision.ai_layer import (
    AIDecisionError,
    AIDecisionLayer,
    LLMHardRuleViolation,
    LLMZoneViolation,
)
from xauusd_bot.decision.openrouter_client import (
    LLMCallError,
    LLMServerError,
    LLMTimeoutError,
    LLMValidationError,
)
from xauusd_bot.decision.rule_fallback import RuleBasedFallback
from xauusd_bot.journal.store import JournalStore

log = structlog.get_logger(__name__)


# Stable reason strings (orchestrator-emitted). The test suite asserts
# on these; do not rename.
REASON_LLM_WATCH_OR_PREPARE = "llm_watch_or_prepare"
REASON_LLM_NO_TRADE = "llm_no_trade"
REASON_LLM_DISABLED = "ai_layer_disabled"


# ---------------------------------------------------------------- orchestrator


class AIDecisionOrchestrator:
    """Top-level decision dispatcher — LLM with safety-authoritative fallback.

    Parameters
    ----------
    ai_layer:
        An :class:`AIDecisionLayer` (or compatible duck-typed
        object with an ``async def decide(...)`` method).
    rule_fallback:
        The :class:`RuleBasedFallback` for the safety-authoritative
        path. Required.
    settings:
        :class:`Settings` — reads ``ai_layer_enabled``,
        ``ai_layer_score_threshold``, ``openrouter_api_key``.
    journal_store:
        Optional :class:`JournalStore` — when provided, every
        LLM ↔ fallback disagreement is logged as an
        :class:`LLMFallbackDiscrepancy`. When ``None``, the
        discrepancy is still returned in the in-memory
        ``last_discrepancy`` for tests / observability.
    """

    def __init__(
        self,
        ai_layer: AIDecisionLayer,
        rule_fallback: RuleBasedFallback,
        settings: Settings,
        journal_store: JournalStore | None = None,
    ) -> None:
        self._ai_layer = ai_layer
        self._rule_fallback = rule_fallback
        self._settings = settings
        self._journal_store = journal_store
        # In-memory cache of the most recent discrepancy (test helper).
        self.last_discrepancy: LLMFallbackDiscrepancy | None = None
        # Block-6 spec-exact variant of the same record.
        self.last_discrepancy_v2: LLMFallbackDiscrepancyV2 | None = None

    # ============================================================ public

    async def decide(
        self,
        feature_snapshot: FeatureSnapshotBundle,
        score: Score,
        account: AccountInfo | None = None,
        agg: AggregatedFeatures | None = None,
    ) -> Decision:
        """Produce the final :class:`Decision`.

        ``agg`` is optional: it's only used to populate
        :class:`LLMFallbackDiscrepancy` records with the rule side
        context. The orchestrator runs the rule fallback itself
        for the rule-side context; ``agg`` is just an optimization
        for callers that already have it (saves one
        :class:`RuleBasedFallback.decide` call).
        """

        ts = score.timestamp

        # ---- 1. Score gate: skip the LLM below the threshold.
        if not self._settings.ai_layer_enabled:
            log.debug("orchestrator_ai_layer_disabled", score=score.total_score)
            decision = self._rule_decision(score, agg, account, reason=REASON_LLM_DISABLED)
            await self._log_discrepancy(
                ts=ts, score=score, rule_decision=None, llm_decision=None,
                reason=REASON_LLM_DISABLED, final_decision=decision, llm_raw=None,
            )
            return decision

        if score.total_score < self._settings.ai_layer_score_threshold:
            log.debug(
                "orchestrator_score_below_threshold",
                score=score.total_score,
                threshold=self._settings.ai_layer_score_threshold,
            )
            decision = self._rule_decision(
                score, agg, account, reason=REASON_SCORE_BELOW_THRESHOLD
            )
            await self._log_discrepancy(
                ts=ts, score=score, rule_decision=None, llm_decision=None,
                reason=REASON_SCORE_BELOW_THRESHOLD, final_decision=decision, llm_raw=None,
            )
            return decision

        # ---- 2. API-key gate.
        if self._settings.openrouter_api_key is None:
            log.debug("orchestrator_no_api_key")
            decision = self._rule_decision(score, agg, account, reason=REASON_NO_API_KEY)
            await self._log_discrepancy(
                ts=ts, score=score, rule_decision=None, llm_decision=None,
                reason=REASON_NO_API_KEY, final_decision=decision, llm_raw=None,
            )
            return decision

        # ---- 3. News-blackout gate: hard rule, LLM has no chance.
        if (
            feature_snapshot.news is not None
            and feature_snapshot.news.in_blackout_flag
        ):
            log.debug("orchestrator_news_blackout", score=score.total_score)
            decision = self._rule_decision(score, agg, account, reason=REASON_NEWS_BLACKOUT)
            await self._log_discrepancy(
                ts=ts, score=score, rule_decision=None, llm_decision=None,
                reason=REASON_NEWS_BLACKOUT, final_decision=decision, llm_raw=None,
            )
            return decision

        # ---- 4. LLM call (with one retry).
        rule_decision = self._rule_fallback.decide(score=score, agg=agg, account=account) if agg else None
        try:
            llm_decision, attempts = await self._call_with_retry(feature_snapshot, score, account)
        except AIDecisionError as exc:
            # LLMHardRuleViolation / LLMZoneViolation — we got a
            # response, but it violated a hard rule. Override to
            # ``no_trade`` and return the rule fallback's decision
            # (which already vetoed on the same rule).
            reason = (
                REASON_HARD_RULE_VIOLATION
                if isinstance(exc, LLMHardRuleViolation)
                else REASON_ZONE_VIOLATION
            )
            log.warning(
                "orchestrator_llm_hard_rule_violation",
                reason=reason,
                error=str(exc),
            )
            decision = self._rule_decision(score, agg, account, reason=reason)
            await self._log_discrepancy(
                ts=ts,
                score=score,
                rule_decision=rule_decision,
                llm_decision=None,
                reason=reason,
                final_decision=decision,
                llm_raw=None,
            )
            return decision
        except LLMCallError as exc:
            # Timeout / 5xx / parse error — same as above: fall back
            # and log.
            reason = (
                REASON_TIMEOUT
                if isinstance(exc, LLMTimeoutError)
                else REASON_VALIDATION_ERROR
                if isinstance(exc, LLMValidationError)
                else "openrouter_server_error"
                if isinstance(exc, LLMServerError)
                else "openrouter_error"
            )
            log.warning(
                "orchestrator_llm_call_failed",
                reason=reason,
                error=str(exc),
            )
            decision = self._rule_decision(score, agg, account, reason=reason)
            await self._log_discrepancy(
                ts=ts,
                score=score,
                rule_decision=rule_decision,
                llm_decision=None,
                reason=reason,
                final_decision=decision,
                llm_raw=None,
            )
            return decision

        # ---- 5. Map LLM decision → Block-3 Decision.
        decision = self._llm_to_decision(score, llm_decision)
        # If the LLM was vetoed by the post-flight hard rules, the
        # LLMHardRuleViolation path above already handled it. Here
        # the LLM output passed all hard rules.

        # ---- 6. Discrepancy bookkeeping.
        if rule_decision is not None and rule_decision.action != decision.action:
            resolution = _classify_resolution(rule_decision, decision)
            await self._log_discrepancy(
                ts=ts,
                score=score,
                rule_decision=rule_decision,
                llm_decision=llm_decision,
                reason=None,  # no fallback reason — both answered
                final_decision=decision,
                llm_raw=llm_decision.model_dump_json(),
                resolution=resolution,
            )
        return decision

    # ============================================================ internals

    def _rule_decision(
        self,
        score: Score,
        agg: AggregatedFeatures | None,
        account: AccountInfo | None,
        *,
        reason: str,
    ) -> Decision:
        """Return the rule fallback's decision, with ``reason`` attached as block_reason.

        Used by the short-circuit gates (score / key / news / LLM
        failures). The reason is recorded in the Decision's
        ``block_reason`` so the journal / smoke can trace *why* the
        LLM was bypassed.
        """

        if agg is None:
            # No aggregated features available — degrade to a no_trade.
            return Decision(
                action=DecisionAction.NO_TRADE,
                entry_type=None,
                block_reason=reason,
                source_score=score.total_score,
                source_band=score.band,
                source_direction=score.direction,
                timestamp=score.timestamp,
            )
        d = self._rule_fallback.decide(score=score, agg=agg, account=account)
        if d.action == DecisionAction.NO_TRADE:
            # Preserve the gate reason for observability.
            return d.model_copy(update={"block_reason": reason})
        # Fallback said "enter" but the gate vetoed → no_trade.
        return Decision(
            action=DecisionAction.NO_TRADE,
            entry_type=None,
            block_reason=reason,
            source_score=score.total_score,
            source_band=score.band,
            source_direction=score.direction,
            timestamp=score.timestamp,
        )

    def _llm_to_decision(self, score: Score, llm: LLMDecision) -> Decision:
        """Map :class:`LLMDecision` → :class:`Decision`.

        The orchestrator trusts the LLM's direction / entry_type at
        the score band passed (≥ 65). If the LLM emits "watch" /
        "prepare", the action is ``no_trade`` with a stable
        ``block_reason`` for the journal.
        """

        decision_map = {
            "no_trade": (DecisionAction.NO_TRADE, None, REASON_LLM_NO_TRADE),
            "watch": (DecisionAction.NO_TRADE, None, REASON_LLM_WATCH_OR_PREPARE),
            "prepare": (DecisionAction.NO_TRADE, None, REASON_LLM_WATCH_OR_PREPARE),
            "scout": (DecisionAction.ENTER_LONG, EntryType.SCOUT, None),
            "reduced_entry": (DecisionAction.ENTER_LONG, EntryType.REDUCED, None),
            "full_entry": (DecisionAction.ENTER_LONG, EntryType.FULL, None),
        }
        action, entry_type, block_reason = decision_map[llm.decision]
        # Map entry_side → action. ENTER_LONG is the default above;
        # override to ENTER_SHORT for short calls.
        if action == DecisionAction.ENTER_LONG and llm.entry_side == "short":
            action = DecisionAction.ENTER_SHORT

        return Decision(
            action=action,
            entry_type=entry_type,
            block_reason=block_reason,
            source_score=score.total_score,
            source_band=score.band,
            source_direction=score.direction,
            source_engine="ai",
            timestamp=score.timestamp,
        )

    async def _call_with_retry(
        self,
        feature_snapshot: FeatureSnapshotBundle,
        score: Score,
        account: AccountInfo | None,
    ) -> tuple[LLMDecision, int]:
        """Call :meth:`AIDecisionLayer.decide`, retrying transient errors.

        Retries on validation / empty-body / zone / timeout errors (a brief
        linear backoff between tries, same provider — no ZDR change), up to
        ``settings.ai_layer_max_attempts``. Server / auth errors are NOT
        retried. On final failure the last exception bubbles to
        :meth:`decide` for rule fallback. Returns ``(decision, attempts)``.
        """

        max_attempts = max(1, int(self._settings.ai_layer_max_attempts))
        backoff = float(self._settings.ai_layer_retry_backoff_seconds)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                llm_decision = await self._ai_layer.decide(
                    feature_snapshot=feature_snapshot,
                    score=score,
                    account=account,
                )
                return llm_decision, attempt
            except (LLMValidationError, LLMZoneViolation, LLMTimeoutError) as exc:
                last_exc = exc
                log.warning(
                    "orchestrator_llm_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if attempt < max_attempts and backoff > 0:
                    await asyncio.sleep(backoff * attempt)  # 0.4s, 0.8s, ...
                continue
            except LLMCallError:
                # Server / auth — do NOT retry; bubble up.
                raise
        # All attempts failed → bubble the last exception.
        assert last_exc is not None
        raise last_exc

    async def _log_discrepancy(
        self,
        *,
        ts: datetime,
        score: Score,
        rule_decision: Decision | None,
        llm_decision: LLMDecision | None,
        reason: str | None,
        final_decision: Decision,
        llm_raw: str | None,
        resolution: DiscrepancyResolutionTag = DiscrepancyResolutionTag.AGREEMENT,
    ) -> None:
        """Write a :class:`LLMFallbackDiscrepancy` to the journal (best-effort).

        ``reason`` is a free-form string (one of the REASON_*
        constants). ``resolution`` is computed by the caller when
        the LLM and rule both answered. When ``rule_decision`` or
        ``llm_decision`` is None, ``resolution`` defaults to
        ``AGREEMENT`` (no comparison happened) but the call still
        records the fallback reason for audit.
        """

        decision_id = uuid4()
        rule_action = rule_decision.action if rule_decision else DecisionAction.NO_TRADE
        llm_action = _llm_decision_to_action(llm_decision) if llm_decision else None
        # --- Block-5a superset record.
        rec = LLMFallbackDiscrepancy(
            timestamp=ts,
            decision_id=decision_id,
            rule_action=rule_action,
            rule_score=score.total_score,
            rule_band=score.band,
            rule_block_reasons=(
                [rule_decision.block_reason] if rule_decision and rule_decision.block_reason else []
            ),
            llm_action=llm_action,
            llm_score=(int(llm_decision.confidence) if llm_decision else None),
            llm_reasoning=llm_decision.comment if llm_decision else (llm_raw or reason),
            final_action=final_decision.action,
            final_source=("llm" if llm_action is not None and llm_action == final_decision.action else "rule"),
            resolution=resolution,
        )
        # --- Block-6 spec-exact record (V2).
        v2_reason = _map_reason_to_v2(reason)
        v2_rec = LLMFallbackDiscrepancyV2(
            timestamp=ts,
            decision_id=decision_id,
            score=score.total_score,
            llm_raw_response=llm_raw,
            fallback_reason=v2_reason,
            rule_decision=rule_action.value,
            llm_decision=llm_action.value if llm_action is not None else None,
        )
        self.last_discrepancy = rec
        self.last_discrepancy_v2 = v2_rec
        if self._journal_store is not None:
            try:
                await self._journal_store.write_discrepancy(rec)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning("orchestrator_journal_write_v1_failed", error=str(exc))
            try:
                await self._journal_store.write_discrepancy_v2(v2_rec)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning("orchestrator_journal_write_v2_failed", error=str(exc))


# ---------------------------------------------------------------- helpers


# Stable mapping from the orchestrator's REASON_* string to the
# V2 discrepancy schema's `fallback_reason` literal. Both vocabularies
# happen to match, but this explicit map keeps the boundary clear.
_V2_FALLBACK_REASON_MAP: dict[str, str] = {
    REASON_TIMEOUT: "timeout",
    REASON_VALIDATION_ERROR: "validation_error",
    REASON_ZONE_VIOLATION: "zone_violation",
    REASON_HARD_RULE_VIOLATION: "hard_rule_violation",
    REASON_SCORE_BELOW_THRESHOLD: "score_below_threshold",
    REASON_LLM_DISABLED: "openrouter_disabled",
    REASON_NO_API_KEY: "openrouter_disabled",
    REASON_NEWS_BLACKOUT: "hard_rule_violation",
}


def _map_reason_to_v2(reason: str | None) -> str:
    """Map the orchestrator's REASON_* to the V2 ``fallback_reason`` literal.

    Unknown reasons (e.g. ``None`` when no fallback reason was
    specified, or a new REASON_* that hasn't been mapped) default
    to ``"validation_error"`` — the catch-all bucket in the V2
    schema's literal.
    """

    if reason is None:
        return "validation_error"
    return _V2_FALLBACK_REASON_MAP.get(reason, "validation_error")


def _llm_decision_to_action(llm: LLMDecision) -> DecisionAction:
    if llm.decision in ("scout", "reduced_entry", "full_entry"):
        if llm.entry_side == "short":
            return DecisionAction.ENTER_SHORT
        return DecisionAction.ENTER_LONG
    return DecisionAction.NO_TRADE


def _classify_resolution(
    rule_decision: Decision,
    llm_decision: Decision,
) -> DiscrepancyResolutionTag:
    """Tag the rule-vs-llm disagreement.

    * AGREEMENT   — both same action.
    * RULE_VETOED — rule no_trade, llm enter → rule wins.
    * LLM_VETOED  — rule enter, llm no_trade → llm wins (allowed).
    * RULE_RELAXED — both enter but with different entry_types.
      Conservative: treat as llm_vetoed (LLM's more-cautious sizing
      wins). This is rare.
    """

    rule_enter = rule_decision.action != DecisionAction.NO_TRADE
    llm_enter = llm_decision.action != DecisionAction.NO_TRADE
    if rule_enter == llm_enter:
        if rule_enter and rule_decision.entry_type != llm_decision.entry_type:
            return DiscrepancyResolutionTag.RULE_RELAXED
        return DiscrepancyResolutionTag.AGREEMENT
    if rule_enter and not llm_enter:
        return DiscrepancyResolutionTag.LLM_VETOED
    # rule=no_trade, llm=enter → rule wins.
    return DiscrepancyResolutionTag.RULE_VETOED


# ---------------------------------------------------------------- re-exports

__all__ = [
    "AIDecisionOrchestrator",
    "REASON_LLM_DISABLED",
    "REASON_LLM_NO_TRADE",
    "REASON_LLM_WATCH_OR_PREPARE",
]
