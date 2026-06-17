"""decision-engine service — consumes ``features``, emits ``decisions``.

Runs aggregate → score → (rule or AI) → qualify for every feature
bundle and publishes the :class:`Decision` + :class:`Score`
(+ :class:`TradeQualification`) plus the originating bundle (the
execution-engine needs it for SL/TP) on the ``decisions`` stream.

Entry point for ``SERVICE_ROLE=decision-engine``.

The decision layer is connector-free: per-bar account state is not
streamed, and :class:`RuleBasedFallback` treats a missing account as
"no block". The execution-engine performs the authoritative,
account-aware risk checks with its own connector.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    DecisionEvent,
    FeaturesEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.runtime_config import get_ai_enabled
from xauusd_bot.common.service import make_publisher, run_consumer_service
from xauusd_bot.decision.pipeline import DecisionPipeline

log = structlog.get_logger(__name__)

GROUP = "decision-engine-v1"

# How long a fetched runtime AI flag is reused before re-reading Redis.
# Decisions arrive fast; a per-message GET would be wasteful, and a few
# seconds of staleness on an operator toggle is fine.
_AI_FLAG_TTL_SECONDS = 2.0


class _RuntimeAIFlag:
    """Cached reader for the dashboard-controlled AI-layer toggle.

    Reads :data:`runtime:ai_layer_enabled` from the trading Redis at most
    once per :data:`_AI_FLAG_TTL_SECONDS`, falling back to the static
    ``settings.ai_layer_enabled`` default when the key is unset or Redis
    is briefly unavailable.
    """

    def __init__(self, redis_client: Any, *, default: bool, ttl: float = _AI_FLAG_TTL_SECONDS) -> None:
        self._redis = redis_client
        self._default = default
        self._ttl = ttl
        self._value = default
        self._fetched_at = 0.0

    async def get(self) -> bool:
        now = time.monotonic()
        if now - self._fetched_at < self._ttl:
            return self._value
        try:
            self._value = await get_ai_enabled(self._redis, default=self._default)
        except Exception as exc:  # noqa: BLE001 - keep deciding on the last known value
            log.warning("decision_engine_ai_flag_read_failed", error=str(exc))
        self._fetched_at = now
        return self._value


def _make_handler(pipeline: DecisionPipeline, publisher: Publisher, ai_flag: _RuntimeAIFlag):
    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, FeaturesEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("decision_engine_dropping_unknown_version", version=ev.schema_version)
            return
        use_ai = await ai_flag.get()
        decision, score, qualification = await pipeline.decide(
            ev.bundle, account=None, use_ai=use_ai
        )
        await publisher.publish(
            StreamTopic.DECISIONS,
            DecisionEvent(
                symbol=ev.symbol,
                decision=decision,
                score=score,
                qualification=qualification,
                bundle=ev.bundle,
                ref_price=ev.ref_price,
            ),
        )

    return handle


async def _run(settings: Settings) -> int:
    import redis.asyncio as aioredis

    pipeline = DecisionPipeline(settings)
    publisher = make_publisher(settings)
    await publisher.connect()
    # Runtime AI toggle lives on the trading Redis (same instance as the
    # streams), so the dashboard's write is visible here.
    flag_redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    ai_flag = _RuntimeAIFlag(flag_redis, default=settings.ai_layer_enabled)
    log.info(
        "decision_engine_ai_config",
        ai_available=pipeline.ai_available,
        ai_default=settings.ai_layer_enabled,
        model=settings.openrouter_model,
    )
    handler = _make_handler(pipeline, publisher, ai_flag)

    async def _on_stop() -> None:
        await publisher.close()
        await flag_redis.aclose()

    return await run_consumer_service(
        ServiceRole.DECISION_ENGINE,
        settings,
        topic=StreamTopic.FEATURES,
        group=GROUP,
        model_cls=FeaturesEvent,
        handler=handler,
        on_stop=_on_stop,
        block_ms=settings.stream_block_ms,
        batch_size=settings.stream_batch_size,
    )


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
