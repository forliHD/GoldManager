"""feature-engine service — consumes ``market_ticks``, emits ``features``.

Maintains a rolling bar buffer (warmed from the connector in live mode,
filled from the stream in replay mode), runs the full feature engine
stack per closed bar, and publishes the resulting
:class:`FeatureSnapshotBundle` on the ``features`` stream.

Entry point for ``SERVICE_ROLE=feature-engine``.

Performance note: the engines are stateless and recompute over the bar
history each call (O(history) per bar). For long replays this is the
dominant cost; ``MAX_HISTORY_BARS`` bounds the buffer. Incremental
engine state is a future optimization — see AGENTS.md.
"""

from __future__ import annotations

import asyncio

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.compact import compact_bundle
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    BarClosedEvent,
    FeaturesEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.service import make_publisher, run_consumer_service
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.pipeline import FeaturePipeline

log = structlog.get_logger(__name__)

GROUP = "feature-engine-v1"


def _make_handler(settings: Settings, pipeline: FeaturePipeline, publisher: Publisher, buffer: list[Bar]):
    max_hist = settings.max_history_bars

    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, BarClosedEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("feature_engine_dropping_unknown_version", version=ev.schema_version)
            return
        buffer.append(ev.bar)
        if len(buffer) > max_hist:
            del buffer[: len(buffer) - max_hist]
        bundle = pipeline.assemble(buffer, ev.bar.time)
        # Slim the bundle before it hits the wire — the rich bundle's
        # fvg.zones / structure history is ~99 % of the payload and the
        # root cause of Redis OOM. compact_bundle keeps only what the
        # decision- and execution-engines read. See compact_bundle.
        bundle = compact_bundle(
            bundle,
            max_swings=settings.bundle_compact_max_swings,
            max_mitigated_zones_per_tf=settings.bundle_compact_max_mitigated_zones_per_tf,
        )
        await publisher.publish(
            StreamTopic.FEATURES,
            FeaturesEvent(symbol=ev.symbol, bundle=bundle, ref_price=ev.bar.close),
        )

    return handle


async def _run(settings: Settings) -> int:
    pipeline = FeaturePipeline()
    publisher = make_publisher(settings)
    await publisher.connect()
    buffer: list[Bar] = []

    # Live mode: seed the buffer with warmup history so the first
    # streamed bar already has context. Replay mode fills from the
    # stream (the collector replays from the very first bar).
    if settings.is_live_connector() and settings.warmup_bars > 0:
        from xauusd_bot.connectors.factory import make_connector

        connector = make_connector(settings)
        try:
            warm = connector.get_rates(settings.symbol, "M1", count=settings.warmup_bars)
            buffer.extend(warm)
            log.info("feature_engine_warmup_loaded", bars=len(warm))
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            log.warning("feature_engine_warmup_failed", error=str(exc))
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                connector.shutdown()

    handler = _make_handler(settings, pipeline, publisher, buffer)
    return await run_consumer_service(
        ServiceRole.FEATURE_ENGINE,
        settings,
        topic=StreamTopic.MARKET_TICKS,
        group=GROUP,
        model_cls=BarClosedEvent,
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
