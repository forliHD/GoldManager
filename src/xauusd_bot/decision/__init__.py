"""Decision layer — Block 3.

Public API
----------
* :class:`FeatureAggregator` — :mod:`xauusd_bot.decision.aggregator`
* :class:`ScoringEngine` — :mod:`xauusd_bot.decision.scoring`
* :class:`RuleBasedFallback` — :mod:`xauusd_bot.decision.rule_fallback`
* :class:`TradeQualificationEngine` — :mod:`xauusd_bot.decision.qualification`

Pydantic schemas live in :mod:`xauusd_bot.common.schemas.decision`.

I-3: Point-in-Time — never read bars / connector state. Inputs are
PIT-filtered before they reach this layer.

I-4: Brain vs Hands — no module here computes position size, SL, or
TP. That is Block 4 (Execution).
"""

from xauusd_bot.decision.aggregator import FeatureAggregator
from xauusd_bot.decision.qualification import TradeQualificationEngine
from xauusd_bot.decision.rule_fallback import RuleBasedFallback
from xauusd_bot.decision.scoring import ScoringEngine

__all__ = [
    "FeatureAggregator",
    "RuleBasedFallback",
    "ScoringEngine",
    "TradeQualificationEngine",
]
