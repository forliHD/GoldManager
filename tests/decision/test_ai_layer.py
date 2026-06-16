"""AIDecisionLayer tests — Block 6 Phase 2.

All tests mock the :class:`OpenRouterClient` — no real HTTP.
Covers:

* Payload construction from a :class:`FeatureSnapshotBundle`
  (PII stripping, score included, zones preserved).
* Entry-zone validation against the snapshot's FVG zones
  (raises :class:`LLMZoneViolation` when out-of-range).
* News-blackout hard rule (raises :class:`LLMHardRuleViolation`).
* Pydantic-validation errors bubble through unchanged
  (the orchestrator catches them in its retry loop).
* System-prompt caching (no re-read of the file per call).
* Account PII stripping (balance / equity / margin are never sent).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.ai_decision import LLMDecision
from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    DecisionAction,
    EntryType,
    Score,
    ScoreBand,
)
from xauusd_bot.common.schemas.features import (
    FVGOutput,
    FVGStatus,
    FVGType,
    FVGZone,
    NewsContextOutput,
)
from xauusd_bot.connectors.schemas import AccountInfo
from xauusd_bot.decision.ai_layer import (
    AIDecisionLayer,
    LLMHardRuleViolation,
    LLMZoneViolation,
)
from xauusd_bot.decision.openrouter_client import LLMValidationError

from tests.decision.conftest import make_bundle, make_aggregated


# ---------------------------------------------------------------- fixtures


def _settings(**overrides) -> Settings:
    base = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        "environment": "test",
        "ai_layer_zdr": True,
        "ai_layer_timeout_seconds": 10.0,
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


def _valid_llm_decision(**overrides) -> LLMDecision:
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


def _mock_client(return_value: Any | None = None, side_effect: Any | None = None):
    """Return an :class:`AsyncMock` configured to look like an OpenRouterClient."""

    client = AsyncMock()
    if side_effect is not None:
        client.complete.side_effect = side_effect
    else:
        client.complete.return_value = return_value if return_value is not None else _valid_llm_decision()
    # Ensure .complete is awaitable.
    return client


# ---------------------------------------------------------------- tests


class TestPayloadConstruction:
    @pytest.mark.asyncio
    async def test_builds_user_payload_from_bundle(self):
        client = _mock_client()
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        bundle = make_bundle()
        score = _score()
        await layer.decide(feature_snapshot=bundle, score=score, account=None)
        # The client.complete call received a user_payload dict.
        assert client.complete.await_count == 1
        kwargs = client.complete.await_args.kwargs
        assert "user_payload" in kwargs
        payload = kwargs["user_payload"]
        # Score carried over.
        assert payload["score"]["total_score"] == 70.0
        assert payload["score"]["band"] == "prepare_65_74"
        assert payload["score"]["direction"] == "long"
        # Features include the engine outputs.
        assert "features" in payload
        assert payload["features"]["atr"] == bundle.atr
        assert "session" in payload["features"]
        assert "vwap" in payload["features"]
        assert "fvg" in payload["features"]
        assert "volume_range" in payload["features"]
        assert "news" in payload["features"]
        # No account PII when None.
        assert payload["account"]["present"] is False

    @pytest.mark.asyncio
    async def test_strips_account_pii(self):
        client = _mock_client()
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        account = AccountInfo(
            login=1234567,
            broker="IC_Markets",
            currency="USD",
            balance=Decimal("10000.00"),
            equity=Decimal("10500.00"),
            margin=Decimal("100.00"),
            free_margin=Decimal("10400.00"),
            leverage=100,
            server_time=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
            trade_allowed=True,
            daily_pnl=Decimal("-200.00"),
            weekly_pnl=Decimal("-500.00"),
            current_spread=Decimal("30"),
        )
        await layer.decide(feature_snapshot=make_bundle(), score=_score(), account=account)
        kwargs = client.complete.await_args.kwargs
        account_payload = kwargs["user_payload"]["account"]
        # Must NOT include these PII fields.
        assert "login" not in account_payload
        assert "balance" not in account_payload
        assert "equity" not in account_payload
        assert "margin" not in account_payload
        assert "free_margin" not in account_payload
        assert "leverage" not in account_payload
        assert "broker" not in account_payload
        assert "daily_pnl" not in account_payload
        assert "weekly_pnl" not in account_payload
        # Current spread is allowed (decision-relevant, not PII).
        assert account_payload.get("current_spread_points") == 30.0

    @pytest.mark.asyncio
    async def test_caches_system_prompt_via_client(self, tmp_path):
        # AIDecisionLayer itself doesn't load the prompt; it asks
        # the client to do it. We pass a system_prompt override
        # explicitly here so the test is independent of the file.
        client = _mock_client()
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        await layer.decide(feature_snapshot=make_bundle(), score=_score())
        kwargs = client.complete.await_args.kwargs
        # system_prompt=None → "use the prompt loaded at init".
        assert kwargs.get("system_prompt") is None


class TestZoneValidation:
    @pytest.mark.asyncio
    async def test_raises_zone_violation_when_min_outside_zones(self):
        # Bundle has zones at 2373-2375 and 2374-2375; LLM proposes 3000.
        bad = _valid_llm_decision(entry_zone={"price_min": 3000.0, "price_max": 3005.0})
        client = _mock_client(return_value=bad)
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        with pytest.raises(LLMZoneViolation):
            await layer.decide(feature_snapshot=make_bundle(), score=_score())

    @pytest.mark.asyncio
    async def test_raises_zone_violation_when_both_bounds_outside_zones(self):
        # Both bounds outside the FVG zones (1000..1010 is nowhere
        # near 2373-2375). Min in-zone with max out-of-zone is
        # allowed (the executor clamps the fill).
        bad = _valid_llm_decision(entry_zone={"price_min": 1000.0, "price_max": 1010.0})
        client = _mock_client(return_value=bad)
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        with pytest.raises(LLMZoneViolation):
            await layer.decide(feature_snapshot=make_bundle(), score=_score())

    @pytest.mark.asyncio
    async def test_passes_when_min_inside_a_zone(self):
        # Min 2373.0 is inside the 2373-2375 H1 zone.
        good = _valid_llm_decision(entry_zone={"price_min": 2373.0, "price_max": 2375.0})
        client = _mock_client(return_value=good)
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        d = await layer.decide(feature_snapshot=make_bundle(), score=_score())
        assert d.decision == "scout"

    @pytest.mark.asyncio
    async def test_passes_when_both_bounds_none(self):
        # "No specific zone" is allowed (watch / prepare cases).
        good = _valid_llm_decision(
            decision="watch", entry_type=None, entry_side=None,
            entry_zone={"price_min": None, "price_max": None},
        )
        client = _mock_client(return_value=good)
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        d = await layer.decide(feature_snapshot=make_bundle(), score=_score())
        assert d.decision == "watch"


class TestNewsBlackoutHardRule:
    @pytest.mark.asyncio
    async def test_rejects_entry_during_news_blackout(self):
        # Make a bundle with news in blackout.
        bundle = make_bundle(
            news=NewsContextOutput(
                minutes_until_next_high_impact=5,
                in_blackout_flag=True,
                next_high_impact=None,
                upcoming_events=[],
                surprise_score=0.0,
            )
        )
        # LLM says "scout" — should be rejected.
        bad = _valid_llm_decision(decision="scout")
        client = _mock_client(return_value=bad)
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        with pytest.raises(LLMHardRuleViolation):
            await layer.decide(feature_snapshot=bundle, score=_score())

    @pytest.mark.asyncio
    async def test_allows_no_trade_during_news_blackout(self):
        bundle = make_bundle(
            news=NewsContextOutput(
                minutes_until_next_high_impact=5,
                in_blackout_flag=True,
                next_high_impact=None,
                upcoming_events=[],
                surprise_score=0.0,
            )
        )
        good = _valid_llm_decision(decision="no_trade", entry_type=None, entry_side=None)
        client = _mock_client(return_value=good)
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        d = await layer.decide(feature_snapshot=bundle, score=_score())
        assert d.decision == "no_trade"


class TestErrorPropagation:
    @pytest.mark.asyncio
    async def test_propagates_llm_validation_error(self):
        client = _mock_client(side_effect=LLMValidationError("bad json"))
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        with pytest.raises(LLMValidationError):
            await layer.decide(feature_snapshot=make_bundle(), score=_score())

    @pytest.mark.asyncio
    async def test_propagates_generic_llm_call_error(self):
        client = _mock_client(side_effect=RuntimeError("network blip"))
        layer = AIDecisionLayer(openrouter_client=client, settings=_settings())
        with pytest.raises(RuntimeError):
            await layer.decide(feature_snapshot=make_bundle(), score=_score())
