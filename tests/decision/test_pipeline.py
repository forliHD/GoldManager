"""DecisionPipeline — runtime AI gating via ``use_ai``.

These tests pass ``_env_file=None`` when building Settings so a developer
``.env`` (which may carry a real OPENROUTER_API_KEY) cannot leak into the
test and trigger a live LLM call.
"""

from __future__ import annotations

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.decision.pipeline import DecisionPipeline


def _settings(**overrides):
    base = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://u:p@h:5432/d",
        "environment": "test",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_ai_unavailable_without_key_all_paths_use_rule(default_bundle) -> None:
    pipeline = DecisionPipeline(_settings(openrouter_api_key=None))
    assert pipeline.ai_available is False
    # Every use_ai value must yield a complete, valid decision via the
    # rule fallback (no orchestrator, no network).
    for use_ai in (None, True, False):
        decision, score, qualification = await pipeline.decide(default_bundle, use_ai=use_ai)
        assert decision is not None
        assert score is not None
        assert qualification is not None


@pytest.mark.asyncio
async def test_ai_available_but_use_ai_false_forces_rule(default_bundle, tmp_path) -> None:
    # A reachable key + prompt makes the orchestrator available, but
    # use_ai=False must still take the rule path (no LLM call).
    prompt = tmp_path / "decision_agent.md"
    prompt.write_text("# system\nYou are a test prompt.\n")
    pipeline = DecisionPipeline(
        _settings(openrouter_api_key="sk-or-v1-test-key", ai_layer_enabled=True),
        prompt_path=prompt,
    )
    assert pipeline.ai_available is True
    # use_ai=False short-circuits to RuleBasedFallback — no network.
    decision, score, qualification = await pipeline.decide(default_bundle, use_ai=False)
    assert decision is not None
    assert qualification is not None
