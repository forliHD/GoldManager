"""Decision layer — Block 3 + Block 6.

Public API
----------
* :class:`FeatureAggregator` — :mod:`xauusd_bot.decision.aggregator`
* :class:`ScoringEngine` — :mod:`xauusd_bot.decision.scoring`
* :class:`RuleBasedFallback` — :mod:`xauusd_bot.decision.rule_fallback`
* :class:`TradeQualificationEngine` — :mod:`xauusd_bot.decision.qualification`
* :class:`OpenRouterClient` — :mod:`xauusd_bot.decision.openrouter_client`
* :class:`AIDecisionLayer` — :mod:`xauusd_bot.decision.ai_layer`
* :class:`AIDecisionOrchestrator` — :mod:`xauusd_bot.decision.ai_orchestrator`

Pydantic schemas live in :mod:`xauusd_bot.common.schemas.decision` and
:mod:`xauusd_bot.common.schemas.ai_decision`.

I-3: Point-in-Time — never read bars / connector state. Inputs are
PIT-filtered before they reach this layer.

I-4: Brain vs Hands — no module here computes position size, SL, or
TP. That is Block 4 (Execution).
"""

from xauusd_bot.decision._weights import ENGINE_WEIGHTS
from xauusd_bot.decision.aggregator import FeatureAggregator
from xauusd_bot.decision.ai_layer import AIDecisionLayer
from xauusd_bot.decision.ai_orchestrator import AIDecisionOrchestrator
from xauusd_bot.decision.openrouter_client import (
    LLMCallError,
    OpenRouterClient,
)
from xauusd_bot.decision.qualification import TradeQualificationEngine
from xauusd_bot.decision.rule_fallback import RuleBasedFallback
from xauusd_bot.decision.scoring import ScoringEngine

__all__ = [
    "AIDecisionLayer",
    "AIDecisionOrchestrator",
    "ENGINE_WEIGHTS",
    "FeatureAggregator",
    "LLMCallError",
    "OpenRouterClient",
    "RuleBasedFallback",
    "ScoringEngine",
    "TradeQualificationEngine",
]
