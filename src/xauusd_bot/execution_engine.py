"""execution-engine service — consumes ``decisions``, emits ``orders`` + ``journal``.

For every *qualified* decision it runs the entry gauntlet (risk → stops
→ TP → size → order) via :class:`ExecutionPipeline` and publishes an
:class:`OrderEvent` on ``orders`` plus :class:`JournalEvent` records on
``journal``.

Entry point for ``SERVICE_ROLE=execution-engine``.

Scope: this drives trade *entry*. Managing open positions over
subsequent bars (trailing stops, partial TP, emergency flatten) is the
not-yet-driven position-management loop — see
:mod:`xauusd_bot.execution.pipeline` and AGENTS.md.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    DecisionEvent,
    JournalEvent,
    OrderEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.service import make_publisher, run_consumer_service
from xauusd_bot.connectors.factory import make_connector
from xauusd_bot.execution.pipeline import ExecutionPipeline

log = structlog.get_logger(__name__)

GROUP = "execution-engine-v1"


def _make_handler(pipeline: ExecutionPipeline, publisher: Publisher):
    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, DecisionEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("execution_engine_dropping_unknown_version", version=ev.schema_version)
            return
        qual = ev.qualification
        if qual is None or not qual.qualified:
            return
        if ev.ref_price is None:
            log.warning("execution_engine_no_ref_price", setup=qual.qualification_id)
            return

        now = ev.decision.timestamp or ev.produced_at or datetime.now(tz=UTC)
        outcome = pipeline.process(
            ev.decision, ev.score, qual, ev.bundle, ref_price=ev.ref_price, now=now
        )
        if not outcome.submitted:
            log.info("execution_engine_blocked", reason=outcome.blocked_reason)
            return

        # Idempotency: the consumer is at-least-once. The order's
        # client_order_id (set by OrderManager) is the dedupe key the
        # broker/journal use to reject a replayed submission.
        await publisher.publish(
            StreamTopic.ORDERS, OrderEvent(symbol=ev.symbol, order=outcome.order)
        )
        await publisher.publish(
            StreamTopic.JOURNAL,
            JournalEvent(symbol=ev.symbol, entry_type="trade", trade=outcome.trade),
        )
        await publisher.publish(
            StreamTopic.JOURNAL,
            JournalEvent(symbol=ev.symbol, entry_type="order", order=outcome.order),
        )

    return handle


async def _run(settings: Settings) -> int:
    connector = make_connector(settings)
    pipeline = ExecutionPipeline(settings, connector)
    publisher = make_publisher(settings)
    await publisher.connect()

    async def _on_stop() -> None:
        await publisher.close()
        with contextlib.suppress(Exception):
            connector.shutdown()

    handler = _make_handler(pipeline, publisher)
    return await run_consumer_service(
        ServiceRole.EXECUTION_ENGINE,
        settings,
        topic=StreamTopic.DECISIONS,
        group=GROUP,
        model_cls=DecisionEvent,
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
