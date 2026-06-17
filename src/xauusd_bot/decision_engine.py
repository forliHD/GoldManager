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

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    DecisionEvent,
    FeaturesEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.service import make_publisher, run_consumer_service
from xauusd_bot.decision.pipeline import DecisionPipeline

log = structlog.get_logger(__name__)

GROUP = "decision-engine-v1"


def _make_handler(pipeline: DecisionPipeline, publisher: Publisher):
    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, FeaturesEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("decision_engine_dropping_unknown_version", version=ev.schema_version)
            return
        decision, score, qualification = await pipeline.decide(ev.bundle, account=None)
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
    pipeline = DecisionPipeline(settings)
    publisher = make_publisher(settings)
    await publisher.connect()
    handler = _make_handler(pipeline, publisher)
    return await run_consumer_service(
        ServiceRole.DECISION_ENGINE,
        settings,
        topic=StreamTopic.FEATURES,
        group=GROUP,
        model_cls=FeaturesEvent,
        handler=handler,
        on_stop=publisher.close,
        block_ms=settings.stream_block_ms,
        batch_size=settings.stream_batch_size,
    )


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
