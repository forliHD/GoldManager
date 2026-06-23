"""AIDecisionOrchestrator tests — Block 6 Phase 3.

All tests mock the :class:`AIDecisionLayer` (so the OpenRouter HTTP
path is not exercised). The real :class:`RuleBasedFallback` and
:class:`FeatureAggregator` are used (they are deterministic).

Covers all the rules from the task spec:

* score < threshold → rule fallback (no LLM call).
* openrouter_api_key is None → rule fallback.
* news_in_blackout → rule fallback.
* LLM call succeeds → return LLM-decided TradeDecision.
* LLM call 1st attempt ValidationError → 1 retry.
* LLM call 1st attempt Timeout → 1 retry.
* LLM call both attempts fail → rule fallback + LLMFallbackDiscrepancy.
* LLM-Decision entry_zone out of range → override to no_trade.
* LLM-Decision decision=scout at news_in_blackout → override.
* LLMFallbackDiscrepancy is written to the journal.
* LLMHardRuleViolation is written to the journal.
* score=75 with LLM "no_trade" → return LLM "no_trade".
* score=68 (between threshold and reduced) with LLM "full_entry" → respect LLM.
* score=92 with LLM "full_entry" → respect LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.ai_decision import LLMDecision
from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    Score,
    ScoreBand,
)
from xauusd_bot.common.schemas.features import NewsContextOutput
from xauusd_bot.decision.ai_layer import (
    AIDecisionLayer,
    LLMHardRuleViolation,
    LLMZoneViolation,
)
from xauusd_bot.decision.ai_orchestrator import (
    AIDecisionOrchestrator,
    REASON_LLM_DISABLED,
    REASON_LLM_NO_TRADE,
    REASON_LLM_WATCH_OR_PREPARE,
    REASON_NEWS_BLACKOUT,
    REASON_NO_API_KEY,
    REASON_SCORE_BELOW_THRESHOLD,
    REASON_TIMEOUT,
    REASON_VALIDATION_ERROR,
    REASON_ZONE_VIOLATION,
)
from xauusd_bot.decision.openrouter_client import (
    LLMTimeoutError,
    LLMValidationError,
)
from xauusd_bot.decision.rule_fallback import RuleBasedFallback
from xauusd_bot.journal.store import InMemoryJournalStore

from tests.decision.conftest import make_aggregated, make_bundle


# ---------------------------------------------------------------- fixtures


def _settings(**overrides) -> Settings:
    base = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        "environment": "test",
        "openrouter_api_key": "sk-or-v1-test",
        "ai_layer_enabled": True,
        "ai_layer_score_threshold": 65,
        "ai_layer_zdr": True,
        # Keep the retry tests at the historical 2 attempts (1 retry) and no
        # backoff sleep; production now defaults to 3.
        "ai_layer_max_attempts": 2,
        "ai_layer_retry_backoff_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)


def _score(*, total: float = 70.0, band: ScoreBand = ScoreBand.PREPARE_65_74) -> Score:
    return Score(
        total_score=total,
        subscores={"h1_zone": 70.0},
        band=band,
        reasoning=["h1_zone score 70 (w=20)"],
        direction="long",
        timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )


def _valid_llm(**overrides) -> LLMDecision:
    base = {
        "decision": "scout",
        "entry_type": "pullback",
        "entry_side": "long",
        "entry_zone": {"price_min": 2373.0, "price_max": 2375.0},
        "invalidations": [],
        "management": {"tp1_rr": 1.0, "tp2_rr": 2.0, "runner_to": None, "protect_before_news_min": None},
        "confidence": 70,
        "comment": "ok",
    }
    base.update(overrides)
    return LLMDecision.model_validate(base)


def _orchestrator(
    *,
    settings: Settings | None = None,
    ai_layer: AIDecisionLayer | None = None,
    rule_fallback: RuleBasedFallback | None = None,
    journal_store: InMemoryJournalStore | None = None,
) -> AIDecisionOrchestrator:
    s = settings or _settings()
    layer = ai_layer or _mock_ai_layer(_valid_llm())
    fallback = rule_fallback or RuleBasedFallback(settings=s)
    return AIDecisionOrchestrator(
        ai_layer=layer,
        rule_fallback=fallback,
        settings=s,
        journal_store=journal_store,
    )


def _mock_ai_layer(return_value: Any | None = None, side_effect: Any | None = None) -> AIDecisionLayer:
    """Build a mock :class:`AIDecisionLayer` whose ``decide`` is an AsyncMock."""

    layer = AsyncMock(spec=AIDecisionLayer)
    if side_effect is not None:
        layer.decide.side_effect = side_effect
    else:
        layer.decide.return_value = return_value if return_value is not None else _valid_llm()
    return layer


# ---------------------------------------------------------------- score gate


class TestScoreGate:
    @pytest.mark.asyncio
    async def test_score_below_threshold_skips_llm(self):
        layer = _mock_ai_layer()
        orch = _orchestrator(ai_layer=layer, settings=_settings(ai_layer_score_threshold=65))
        agg = make_aggregated()
        # Score 64 < threshold 65.
        decision = await orch.decide(
            feature_snapshot=make_bundle(),
            score=_score(total=64.0, band=ScoreBand.OBSERVE_55_64),
            agg=agg,
        )
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_SCORE_BELOW_THRESHOLD
        layer.decide.assert_not_called()

    @pytest.mark.asyncio
    async def test_score_at_threshold_calls_llm(self):
        layer = _mock_ai_layer(_valid_llm(decision="scout", entry_type="pullback", entry_side="long"))
        orch = _orchestrator(ai_layer=layer)
        agg = make_aggregated()
        decision = await orch.decide(
            feature_snapshot=make_bundle(),
            score=_score(total=65.0, band=ScoreBand.PREPARE_65_74),
            agg=agg,
        )
        layer.decide.assert_called_once()
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.SCOUT


# ---------------------------------------------------------------- key gate


class TestApiKeyGate:
    @pytest.mark.asyncio
    async def test_no_api_key_uses_rule_fallback(self):
        layer = _mock_ai_layer()
        orch = _orchestrator(
            ai_layer=layer, settings=_settings(openrouter_api_key=None)
        )
        agg = make_aggregated()
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=agg
        )
        layer.decide.assert_not_called()
        assert decision.block_reason == REASON_NO_API_KEY


# ---------------------------------------------------------------- master switch


class TestMasterSwitch:
    @pytest.mark.asyncio
    async def test_ai_layer_disabled_uses_rule_fallback(self):
        layer = _mock_ai_layer()
        orch = _orchestrator(
            ai_layer=layer, settings=_settings(ai_layer_enabled=False)
        )
        agg = make_aggregated()
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=agg
        )
        layer.decide.assert_not_called()
        assert decision.block_reason == REASON_LLM_DISABLED


# ---------------------------------------------------------------- news gate


class TestNewsGate:
    @pytest.mark.asyncio
    async def test_news_in_blackout_uses_rule_fallback(self):
        layer = _mock_ai_layer()
        orch = _orchestrator(ai_layer=layer)
        bundle = make_bundle(
            news=NewsContextOutput(
                minutes_until_next_high_impact=5,
                in_blackout_flag=True,
                next_high_impact=None,
                upcoming_events=[],
                surprise_score=0.0,
            )
        )
        decision = await orch.decide(
            feature_snapshot=bundle, score=_score(total=70.0), agg=make_aggregated()
        )
        layer.decide.assert_not_called()
        assert decision.block_reason == REASON_NEWS_BLACKOUT


# ---------------------------------------------------------------- happy path


class TestLLMCallSuccess:
    @pytest.mark.asyncio
    async def test_llm_scout_decision_returns_enter_long_scout(self):
        layer = _mock_ai_layer(
            _valid_llm(decision="scout", entry_type="pullback", entry_side="long")
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        layer.decide.assert_called_once()
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.SCOUT
        # The entry came from the LLM → tagged 'ai' so journal_trades.engine_source
        # is accurate (every order was previously mis-tagged 'rule').
        assert decision.source_engine == "ai"

    @pytest.mark.asyncio
    async def test_llm_reduced_entry_returns_reduced(self):
        layer = _mock_ai_layer(
            _valid_llm(decision="reduced_entry", entry_type="confirmation", entry_side="long")
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=80.0), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.REDUCED

    @pytest.mark.asyncio
    async def test_llm_full_entry_returns_full(self):
        layer = _mock_ai_layer(
            _valid_llm(decision="full_entry", entry_type="confirmation", entry_side="long")
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=92.0, band=ScoreBand.FULL_85_PLUS), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.FULL

    @pytest.mark.asyncio
    async def test_llm_short_entry_returns_enter_short(self):
        layer = _mock_ai_layer(
            _valid_llm(decision="scout", entry_type="pullback", entry_side="short")
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(),
            score=Score(
                total_score=70.0,
                subscores={},
                band=ScoreBand.PREPARE_65_74,
                reasoning=[],
                direction="short",
                timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            ),
            agg=make_aggregated(),
        )
        assert decision.action == DecisionAction.ENTER_SHORT

    @pytest.mark.asyncio
    async def test_llm_watch_returns_no_trade(self):
        layer = _mock_ai_layer(
            _valid_llm(decision="watch", entry_type=None, entry_side=None,
                       entry_zone={"price_min": None, "price_max": None})
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_LLM_WATCH_OR_PREPARE

    @pytest.mark.asyncio
    async def test_llm_no_trade_at_score_75_returns_no_trade(self):
        # LLM is allowed to veto a setup the score says is OK.
        layer = _mock_ai_layer(
            _valid_llm(decision="no_trade", entry_type=None, entry_side=None,
                       entry_zone={"price_min": None, "price_max": None})
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=75.0), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_LLM_NO_TRADE


# ---------------------------------------------------------------- retry / fallback


class TestRetryAndFallback:
    @pytest.mark.asyncio
    async def test_validation_error_triggers_retry(self):
        # First call: validation error. Second: success.
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = [
            LLMValidationError("bad json"),
            _valid_llm(decision="scout", entry_type="pullback", entry_side="long"),
        ]
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert layer.decide.await_count == 2
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.SCOUT

    @pytest.mark.asyncio
    async def test_zone_violation_triggers_retry(self):
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = [
            LLMZoneViolation("zone out of range"),
            _valid_llm(decision="scout", entry_type="pullback", entry_side="long"),
        ]
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert layer.decide.await_count == 2
        assert decision.action == DecisionAction.ENTER_LONG

    @pytest.mark.asyncio
    async def test_timeout_triggers_retry_then_falls_back(self):
        # Per the Block-6 spec: "LLM-Call 1. Versuch Timeout → 1 Retry".
        # The orchestrator retries once on TimeoutError, then falls
        # back to RuleBasedFallback.
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMTimeoutError("timeout")
        orch = _orchestrator(ai_layer=layer, journal_store=InMemoryJournalStore())
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert layer.decide.await_count == 2  # 1 retry
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_TIMEOUT
        assert orch.last_discrepancy is not None

    @pytest.mark.asyncio
    async def test_both_attempts_validation_error_falls_back(self):
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMValidationError("bad json")
        orch = _orchestrator(ai_layer=layer, journal_store=InMemoryJournalStore())
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert layer.decide.await_count == 2
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_VALIDATION_ERROR
        assert orch.last_discrepancy is not None

    @pytest.mark.asyncio
    async def test_max_attempts_is_configurable(self):
        # A transient empty-body error retried up to ai_layer_max_attempts, then
        # one success on the final attempt → the decision is taken (no fallback).
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = [LLMValidationError("empty"), LLMValidationError("empty"), _valid_llm()]
        orch = _orchestrator(
            ai_layer=layer,
            settings=_settings(ai_layer_max_attempts=3, ai_layer_retry_backoff_seconds=0.0),
        )
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert layer.decide.await_count == 3
        assert decision.action == DecisionAction.ENTER_LONG

    @pytest.mark.asyncio
    async def test_both_attempts_zone_violation_falls_back(self):
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMZoneViolation("nope")
        orch = _orchestrator(ai_layer=layer, journal_store=InMemoryJournalStore())
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert layer.decide.await_count == 2
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_ZONE_VIOLATION
        assert orch.last_discrepancy is not None


# ---------------------------------------------------------------- hard-rule override


class TestHardRuleOverride:
    @pytest.mark.asyncio
    async def test_llm_scout_during_news_blackout_is_overridden(self):
        # The AIDecisionLayer's own post-flight check raises
        # LLMHardRuleViolation. The orchestrator catches it and
        # returns no_trade.
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMHardRuleViolation("news blackout veto")
        orch = _orchestrator(ai_layer=layer, journal_store=InMemoryJournalStore())
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        # Hard-rule violation is NOT retried (it's a domain error).
        assert layer.decide.await_count == 1
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == "hard_rule_violation"


# ---------------------------------------------------------------- journal / discrepancy


class TestJournalDiscrepancy:
    @pytest.mark.asyncio
    async def test_discrepancy_written_to_journal_on_timeout(self):
        store = InMemoryJournalStore()
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMTimeoutError("timeout")
        orch = _orchestrator(ai_layer=layer, journal_store=store)
        await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        # Verify the discrepancy was written to the journal (both V1 and V2).
        recs = await store.list_discrepancies()
        assert len(recs) == 1
        assert recs[0].llm_action is None
        assert recs[0].final_action == DecisionAction.NO_TRADE
        # V2 record also written.
        v2_recs = await store.list_discrepancies_v2()
        assert len(v2_recs) == 1
        assert v2_recs[0].fallback_reason == "timeout"
        assert v2_recs[0].llm_decision is None
        assert v2_recs[0].rule_decision == "no_trade"
        assert v2_recs[0].score == 70.0

    @pytest.mark.asyncio
    async def test_discrepancy_written_to_journal_on_hard_rule_violation(self):
        store = InMemoryJournalStore()
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMHardRuleViolation("news blackout")
        orch = _orchestrator(ai_layer=layer, journal_store=store)
        await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        recs = await store.list_discrepancies()
        assert len(recs) == 1
        assert recs[0].llm_action is None
        assert recs[0].final_action == DecisionAction.NO_TRADE
        # V2 record has hard_rule_violation.
        v2_recs = await store.list_discrepancies_v2()
        assert len(v2_recs) == 1
        assert v2_recs[0].fallback_reason == "hard_rule_violation"

    @pytest.mark.asyncio
    async def test_no_journal_no_crash(self):
        layer = AsyncMock(spec=AIDecisionLayer)
        layer.decide.side_effect = LLMTimeoutError("timeout")
        orch = _orchestrator(ai_layer=layer, journal_store=None)
        # Must not crash even without a journal store.
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=70.0), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.NO_TRADE
        assert orch.last_discrepancy is not None  # in-memory cache still populated
        assert orch.last_discrepancy_v2 is not None  # V2 cache also populated

    @pytest.mark.asyncio
    async def test_v2_record_written_on_score_gate(self):
        # Score-gate short-circuits to fallback without LLM call —
        # V2 record should still be written with score_below_threshold.
        store = InMemoryJournalStore()
        orch = _orchestrator(ai_layer=AsyncMock(spec=AIDecisionLayer), journal_store=store)
        await orch.decide(
            feature_snapshot=make_bundle(),
            score=_score(total=50.0, band=ScoreBand.OBSERVE_55_64),
            agg=make_aggregated(),
        )
        v2_recs = await store.list_discrepancies_v2()
        assert len(v2_recs) == 1
        assert v2_recs[0].fallback_reason == "score_below_threshold"
        assert v2_recs[0].score == 50.0


# ---------------------------------------------------------------- score-band LLM respect


class TestLLMScoreBandRespect:
    @pytest.mark.asyncio
    async def test_llm_full_entry_at_score_68_respected(self):
        # Score is between threshold and reduced band (68); LLM says
        # "full_entry". The orchestrator trusts the LLM (LLM has
        # final word in the score band, only hard rules block).
        layer = _mock_ai_layer(
            _valid_llm(decision="full_entry", entry_type="confirmation", entry_side="long")
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=68.0), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.FULL

    @pytest.mark.asyncio
    async def test_llm_full_entry_at_score_92_respected(self):
        layer = _mock_ai_layer(
            _valid_llm(decision="full_entry", entry_type="confirmation", entry_side="long")
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(),
            score=_score(total=92.0, band=ScoreBand.FULL_85_PLUS),
            agg=make_aggregated(),
        )
        assert decision.action == DecisionAction.ENTER_LONG
        assert decision.entry_type == EntryType.FULL

    @pytest.mark.asyncio
    async def test_llm_no_trade_at_score_75_respected(self):
        # LLM is allowed to veto even at high score.
        layer = _mock_ai_layer(
            _valid_llm(decision="no_trade", entry_type=None, entry_side=None,
                       entry_zone={"price_min": None, "price_max": None})
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(), score=_score(total=75.0), agg=make_aggregated()
        )
        assert decision.action == DecisionAction.NO_TRADE


# ---------------------------------------------------------------- LLM veto (LLM says no, fallback says yes)


class TestLLMVeto:
    @pytest.mark.asyncio
    async def test_llm_no_trade_veto_overrides_rule(self):
        # Rule fallback says "enter" (high score, good news, clear
        # direction), LLM says "no_trade". LLM wins (veto allowed).
        from tests.decision.conftest import make_aggregated, make_subscore
        from xauusd_bot.decision._weights import ENGINE_WEIGHTS

        # Build an aggregated that the rule fallback approves.
        sub = {
            name: make_subscore(
                name,
                value=80.0,
                weight=ENGINE_WEIGHTS[name],
                direction_bias=1,
                reasoning="test",
            )
            for name in ENGINE_WEIGHTS
        }
        agg = make_aggregated(subscores=sub)
        layer = _mock_ai_layer(
            _valid_llm(decision="no_trade", entry_type=None, entry_side=None,
                       entry_zone={"price_min": None, "price_max": None})
        )
        orch = _orchestrator(ai_layer=layer)
        decision = await orch.decide(
            feature_snapshot=make_bundle(),
            score=_score(total=85.0, band=ScoreBand.FULL_85_PLUS),
            agg=agg,
        )
        # LLM veto wins.
        assert decision.action == DecisionAction.NO_TRADE
        assert decision.block_reason == REASON_LLM_NO_TRADE
