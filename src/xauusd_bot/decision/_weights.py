"""Engine-weight configuration for the decision layer (Block 3).

Single source of truth for the Plan §8 weights. The aggregator
constructs one :class:`EngineSubscore` per key here, and the
:class:`ScoringEngine` consumes the weights as-is.

The sum MUST be 100. This is enforced at import time
(:func:`assert_weights_sum_to_100`) and in unit tests.

Why a separate module?
-----------------------
* The aggregator and the scoring engine need the *same* weight table.
* A future tuning loop (Block 5+ walk-forward) will mutate weights
  via this module's public surface, not by hard-coding numbers in
  the scoring engine.
* The :class:`Settings` could eventually override these for live
  tuning, but the default is the plan-specified startwert.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------- weights


# Engine name → weight. Sum must be 100.
#
# Plan §8 / 04_decision_scoring.md §Deliverables 2:
#   H1-Zone 20  | M5 15 | TripleVWAP 15
#   HTF Volume Profile 20 (Yearly 8 / Monthly 5 / Weekly 4 / Acceptance 3)
#   Session/Liquidity 10 | News 10 | Momentum 10
#
# HTF VP is implemented as a single engine with one total weight of
# 20; the "Yearly 8 / Monthly 5 / Weekly 4 / Acceptance 3" sub-split
# is a *breakdown* inside the engine's own scoring (see
# :func:`_score_htf_volume_profile` in :mod:`xauusd_bot.decision.aggregator`).
#
# H1-Zone (20) is realised by the FVG engine's H1 zones (the
# 3-bar-pattern zones that matter for multi-hour trades per Plan §8).
# M5 (15) is the FVG engine's M5 zones (better entry timing per
# Plan §8). The split between H1 and M5 is exposed in the FVG
# engine's own subscore.
#
# ``liquidity`` here is the Liquidity engine output (TP-target
# clusters, SL protection zones). The Plan's "Session/Liquidity 10"
# bundles session AND liquidity into one weight; we keep them as
# separate engines (cleaner per-engine percentile history) but their
# weights add up to 10.

ENGINE_WEIGHTS: Final[dict[str, float]] = {
    "h1_zone": 20.0,
    "m5_zone": 15.0,
    "triple_vwap": 15.0,
    "htf_volume_profile": 20.0,
    "session_liquidity": 10.0,  # 5 for session + 5 for liquidity
    "news": 10.0,
    "momentum": 10.0,
}
assert abs(sum(ENGINE_WEIGHTS.values()) - 100.0) < 1e-9, (
    f"ENGINE_WEIGHTS must sum to 100, got {sum(ENGINE_WEIGHTS.values())}"
)


# Internal sub-split for the bundled engines. These are used by the
# aggregator when building the per-engine subscore. They are NOT
# independently scored — they feed back into the parent engine's
# single 0-100 value.

_HTF_VP_SUB_WEIGHTS: Final[dict[str, float]] = {
    "yearly": 8.0,
    "monthly": 5.0,
    "weekly": 4.0,
    "acceptance": 3.0,
}
assert abs(sum(_HTF_VP_SUB_WEIGHTS.values()) - 20.0) < 1e-9

_SESSION_LIQUIDITY_SPLIT: Final[dict[str, float]] = {
    "session": 5.0,
    "liquidity": 5.0,
}
assert abs(sum(_SESSION_LIQUIDITY_SPLIT.values()) - 10.0) < 1e-9


# ---------------------------------------------------------------- helpers


def assert_weights_sum_to_100() -> None:
    """Re-assert the weight invariant. Public for test / health-check use."""

    total = sum(ENGINE_WEIGHTS.values())
    if abs(total - 100.0) > 1e-9:
        raise AssertionError(f"ENGINE_WEIGHTS must sum to 100, got {total}")


def get_weight(engine_name: str) -> float:
    """Return the weight for ``engine_name``. Defaults to 0 for unknown keys."""

    return ENGINE_WEIGHTS.get(engine_name, 0.0)
