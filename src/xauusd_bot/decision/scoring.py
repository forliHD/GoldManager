"""ScoringEngine — Block 3 Phase 1.

Consumes an :class:`xauusd_bot.common.schemas.decision.AggregatedFeatures`
and emits a :class:`xauusd_bot.common.schemas.decision.Score`.

The total score is the weighted sum of per-engine 0-100 subscores
(weights from Plan §8, see :mod:`xauusd_bot.decision._weights`).
The band is the discrete gate. The reasoning list contains one
deterministic line per engine and one per conflict (max 1 line per
conflict to keep the output bounded).

I-3: Point-in-Time
------------------
The aggregator already received a PIT-filtered
:class:`FeatureSnapshotBundle`. We never look at the connector; we
operate purely on the aggregator's pre-cutoff output. Re-scoring
the same bundle at the same ``ts`` always produces the same score
(deterministic).

I-4: Brain vs Hands
-------------------
The :class:`Score` schema does NOT carry volume, SL, or TP fields.
The scoring engine never sees the AccountInfo or Settings — those
are the RuleBasedFallback's job.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import structlog

from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    EngineSubscore,
    Score,
    ScoreBand,
)
from xauusd_bot.decision._weights import ENGINE_WEIGHTS

log = structlog.get_logger(__name__)


class ScoringEngine:
    """Deterministic, side-effect-free weighted-summation scoring."""

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        # Allow tests to pass a custom weight table; default to the
        # Plan §8 weights. The default is asserted at import time
        # by :mod:`xauusd_bot.decision._weights`.
        self._weights = dict(weights) if weights is not None else dict(ENGINE_WEIGHTS)
        total = sum(self._weights.values())
        if abs(total - 100.0) > 1e-9:
            raise ValueError(f"weights must sum to 100, got {total}")

    def score(self, agg: AggregatedFeatures) -> Score:
        """Compute a :class:`Score` from an :class:`AggregatedFeatures`."""

        if not agg.has_data:
            # Graceful "no data" path: every subscore = 50, total = 50,
            # band = observe_55_64, direction = neutral. The downstream
            # RuleBasedFallback translates <65 → no_trade.
            return Score(
                total_score=50.0,
                subscores={k: 50.0 for k in self._weights},
                band=ScoreBand.OBSERVE_55_64,
                reasoning=["no_data: all engines returned no_data; no_trade downstream"],
                direction="neutral",
                timestamp=agg.ts,
            )

        subscores: dict[str, float] = {}
        total = 0.0
        for engine_name, weight in self._weights.items():
            sub: EngineSubscore | None = agg.subscores.get(engine_name)
            if sub is None:
                # Unknown engine for this aggregator: neutral 50.
                sub_value = 50.0
            else:
                sub_value = sub.value
            subscores[engine_name] = sub_value
            # Weight is a 0-100 percentage (the sum of all weights = 100).
            # We normalize the contribution to a fraction by dividing by 100
            # so the total_score lands in [0, 100].
            total += sub_value * (weight / 100.0)

        # Clamp the total to [0, 100]. With weights summing to 100 and
        # each subscore in [0, 100], the total is in [0, 100] by
        # construction — but a defensive clamp guards against future
        # weight overrides.
        total = max(0.0, min(100.0, total))

        # Direction: aggregate the per-engine direction_bias.
        direction = _aggregate_direction(agg)

        # Reasoning: deterministic lines, capped at 12 to keep
        # the JSON / journal tidy.
        reasoning = _build_reasoning(agg, subscores, direction)

        return Score(
            total_score=round(total, 2),
            subscores={k: round(v, 2) for k, v in subscores.items()},
            band=Score.band_for(total),
            reasoning=reasoning,
            direction=direction,
            timestamp=agg.ts,
        )


# ---------------------------------------------------------------- helpers


def _aggregate_direction(agg: AggregatedFeatures) -> Literal["long", "short", "neutral"]:
    """Aggregate the per-engine direction_bias into one of long/short/neutral.

    Rule: weighted by engine weight. If weighted sum > 0 → long; < 0 →
    short; == 0 → neutral. We require |sum| > 5 (i.e. > 5% of the
    total weight) to call it "long" / "short" — anything weaker is
    "neutral" because the engines don't agree.
    """

    weighted_sum = 0.0
    for sub in agg.subscores.values():
        weighted_sum += sub.direction_bias * sub.weight
    if weighted_sum > 5.0:
        return "long"
    if weighted_sum < -5.0:
        return "short"
    return "neutral"


def _build_reasoning(
    agg: AggregatedFeatures,
    subscores: dict[str, float],
    direction: Literal["long", "short", "neutral"],
) -> list[str]:
    """Compose the deterministic reasoning list."""

    out: list[str] = []
    # 1. Per-engine lines, sorted by contribution desc, capped at 6.
    contributions = sorted(
        agg.subscores.values(),
        key=lambda s: s.value * s.weight,
        reverse=True,
    )
    for sub in contributions[:6]:
        out.append(
            f"{sub.name} score {sub.value:.1f} (w={sub.weight:.0f}): {sub.reasoning}"
        )
    # 2. Dominant engine summary.
    if agg.dominant_engine:
        out.append(f"dominant: {agg.dominant_engine}")
    # 3. Conflict lines, capped at 4.
    for c in agg.conflicts[:4]:
        if c.severity == "block":
            tag = "BLOCK"
        elif c.severity == "warning":
            tag = "WARN"
        else:
            tag = "info"
        out.append(f"[{tag}] {c.engine_a}↔{c.engine_b}: {c.description}")
    # 4. Direction summary.
    if direction != "neutral":
        out.append(f"direction={direction}")
    return out
