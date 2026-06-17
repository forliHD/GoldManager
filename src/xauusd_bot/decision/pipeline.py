"""Decision pipeline factory — aggregate → score → decide → qualify.

Extracted from the in-process decision/journal smokes so the
decision-engine *service* and the smokes share one source of truth.

The AI layer is wired in only when it can actually run (master switch
on, ``OPENROUTER_API_KEY`` present, and the ``decision_agent.md`` prompt
file reachable). Otherwise the pipeline uses :class:`RuleBasedFallback`
directly. Either way the rule fallback stays safety-authoritative — the
orchestrator itself short-circuits to it on every gate (disabled, no
key, score below threshold, news blackout, LLM error). See
``00_FINAL_PLAN.md`` §7.2.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import Decision, Score, TradeQualification
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.decision.aggregator import FeatureAggregator
from xauusd_bot.decision.ai_layer import AIDecisionLayer, default_zones_provider
from xauusd_bot.decision.ai_orchestrator import AIDecisionOrchestrator
from xauusd_bot.decision.openrouter_client import OpenRouterClient
from xauusd_bot.decision.qualification import TradeQualificationEngine
from xauusd_bot.decision.rule_fallback import RuleBasedFallback
from xauusd_bot.decision.scoring import ScoringEngine

log = structlog.get_logger(__name__)

DEFAULT_PROMPT_PATH = Path("decision_agent.md")


class DecisionPipeline:
    """Aggregator + Scoring + (optional AI orchestrator) + Qualification."""

    def __init__(
        self,
        settings: Settings,
        *,
        journal_store: object | None = None,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
    ) -> None:
        self.settings = settings
        self.aggregator = FeatureAggregator()
        self.scoring = ScoringEngine()
        self.fallback = RuleBasedFallback(settings=settings)
        self.qualifier = TradeQualificationEngine(settings=settings)
        self._orchestrator = self._maybe_build_orchestrator(journal_store, prompt_path)

    @property
    def ai_enabled(self) -> bool:
        """True when the AI orchestrator was wired in (vs rule-only)."""

        return self._orchestrator is not None

    def _maybe_build_orchestrator(
        self, journal_store: object | None, prompt_path: Path
    ) -> AIDecisionOrchestrator | None:
        if not self.settings.ai_layer_enabled:
            log.info("decision_pipeline_ai_disabled")
            return None
        if self.settings.openrouter_api_key is None:
            log.info("decision_pipeline_ai_no_api_key")
            return None
        if not prompt_path.exists():
            log.warning("decision_pipeline_ai_prompt_missing", path=str(prompt_path))
            return None
        try:
            client = OpenRouterClient(settings=self.settings, prompt_path=prompt_path)
            ai_layer = AIDecisionLayer(
                openrouter_client=client,
                snapshot_zones_provider=default_zones_provider,
                settings=self.settings,
            )
            orchestrator = AIDecisionOrchestrator(
                ai_layer=ai_layer,
                rule_fallback=self.fallback,
                settings=self.settings,
                journal_store=journal_store,
            )
            log.info("decision_pipeline_ai_enabled", model=self.settings.openrouter_model)
            return orchestrator
        except Exception as exc:  # noqa: BLE001 - never let AI wiring crash the service
            log.warning("decision_pipeline_ai_build_failed", error=str(exc))
            return None

    async def decide(
        self,
        bundle: FeatureSnapshotBundle,
        *,
        account: object | None = None,
    ) -> tuple[Decision, Score, TradeQualification]:
        """Run the full decision stack for one feature bundle.

        Returns ``(decision, score, qualification)``. The orchestrator
        path is async; the rule-only path is sync but wrapped so the
        caller always awaits a single coroutine.
        """

        agg = self.aggregator.aggregate(bundle)
        score = self.scoring.score(agg)
        if self._orchestrator is not None:
            decision = await self._orchestrator.decide(
                feature_snapshot=bundle, score=score, account=account, agg=agg
            )
        else:
            decision = self.fallback.decide(score=score, agg=agg, account=account)
        qualification = self.qualifier.qualify(decision, score, agg, bundle, account=account)
        return decision, score, qualification


__all__ = ["DecisionPipeline"]
