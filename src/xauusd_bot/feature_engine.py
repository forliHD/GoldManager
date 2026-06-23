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
from datetime import UTC, datetime

from xauusd_bot.common.messaging.compact import compact_bundle
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    BarClosedEvent,
    FeaturesEvent,
    JournalEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.common.schemas.journal import FeatureSnapshotRecord
from xauusd_bot.common.service import make_publisher, run_consumer_service
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.pipeline import FeaturePipeline
from xauusd_bot.viz.overlay_writer import build_overlay_payload

log = structlog.get_logger(__name__)

GROUP = "feature-engine-v1"


def _make_handler(
    settings: Settings,
    pipeline: FeaturePipeline,
    publisher: Publisher,
    buffer: list[Bar],
    vp_buffer: list[Bar],
):
    max_hist = settings.max_history_bars
    vp_max = settings.volume_profile_history_bars

    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, BarClosedEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("feature_engine_dropping_unknown_version", version=ev.schema_version)
            return
        buffer.append(ev.bar)
        if len(buffer) > max_hist:
            del buffer[: len(buffer) - max_hist]
        vp_bars: list[Bar] | None = None
        if vp_max > 0:
            vp_buffer.append(ev.bar)
            if len(vp_buffer) > vp_max:
                del vp_buffer[: len(vp_buffer) - vp_max]
            vp_bars = vp_buffer
        full_bundle = pipeline.assemble(buffer, ev.bar.time, vp_bars=vp_bars)
        # Emit the chart-overlay snapshot (VWAP / value area / FVG) to the
        # journal from the FULL bundle, before compaction strips the rich
        # fvg/structure detail. The dashboard reads the latest one to draw
        # the chart indicators. Best-effort — never block the features path.
        await _publish_overlay_snapshot(publisher, ev, full_bundle)
        # Slim the bundle before it hits the wire — the rich bundle's
        # fvg.zones / structure history is ~99 % of the payload and the
        # root cause of Redis OOM. compact_bundle keeps only what the
        # decision- and execution-engines read. See compact_bundle.
        bundle = compact_bundle(
            full_bundle,
            max_swings=settings.bundle_compact_max_swings,
            max_mitigated_zones_per_tf=settings.bundle_compact_max_mitigated_zones_per_tf,
        )
        await publisher.publish(
            StreamTopic.FEATURES,
            FeaturesEvent(symbol=ev.symbol, bundle=bundle, ref_price=ev.bar.close),
        )

    return handle


async def _publish_overlay_snapshot(
    publisher: Publisher, ev: BarClosedEvent, bundle: FeatureSnapshotBundle
) -> None:
    """Publish a ``feature_snapshot`` journal entry carrying the chart overlay.

    The overlay (VWAP triple, weekly/monthly/yearly value areas, live FVG
    zones) needs all three engine outputs; if any is missing (early warmup)
    we skip this bar. Failures are logged and swallowed — the trading path
    must never depend on overlay journaling.
    """

    if bundle.vwap is None or bundle.volume_range is None or bundle.fvg is None:
        return
    try:
        overlay = build_overlay_payload(
            ts=bundle.ts,
            vwap=bundle.vwap,
            volume_range=bundle.volume_range,
            fvg=bundle.fvg,
        )
        record = FeatureSnapshotRecord(
            timestamp=datetime.now(tz=UTC),
            symbol=ev.symbol,
            timeframe="m1",
            bar_time=ev.bar.time,
            has_data=True,
            features={"overlays": overlay},
        )
        await publisher.publish(
            StreamTopic.JOURNAL,
            JournalEvent(symbol=ev.symbol, entry_type="feature_snapshot", snapshot=record),
        )
    except Exception as exc:  # noqa: BLE001 - overlay journaling is best-effort
        log.warning("feature_engine_overlay_snapshot_failed", error=str(exc))


async def _run(settings: Settings) -> int:
    from xauusd_bot.features.news import make_news_provider

    pipeline = FeaturePipeline(
        news_provider=make_news_provider(settings),
        fvg_extend_to_fractal=settings.fvg_extend_to_fractal,
        fvg_extension_fractal_n=settings.fvg_extension_fractal_n,
        fvg_extension_max_atr=settings.fvg_extension_max_atr,
        fvg_leg_step_atr=settings.fvg_leg_step_atr,
    )
    publisher = make_publisher(settings)
    await publisher.connect()
    buffer: list[Bar] = []
    vp_buffer: list[Bar] = []  # deep history for the Volume Profile only

    # Live mode: seed the buffer with warmup history so the first
    # streamed bar already has context. Replay mode fills from the
    # stream (the collector replays from the very first bar).
    if settings.is_live_connector() and settings.warmup_bars > 0:
        from xauusd_bot.connectors.factory import make_connector

        connector = make_connector(settings)
        try:
            # One fetch deep enough for the Volume Profile; the main buffer is
            # the recent tail (kept small so FVG/structure stay fast).
            fetch_n = max(settings.warmup_bars, settings.volume_profile_history_bars)
            warm = connector.get_rates(settings.symbol, "M1", count=fetch_n)
            if settings.volume_profile_history_bars > 0:
                vp_buffer.extend(warm)
                buffer.extend(warm[-settings.warmup_bars:])
            else:
                buffer.extend(warm)
            log.info("feature_engine_warmup_loaded", bars=len(buffer), vp_bars=len(vp_buffer))
            # Detect the broker→UTC clock offset from the freshest bar so the
            # news blackout aligns broker-time bars with UTC calendar events.
            if warm:
                latest = warm[-1].time
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=UTC)
                offset_min = round((latest - datetime.now(UTC)).total_seconds() / 3600.0) * 60
                # Fan the offset out to ALL time-of-day-anchored engines
                # (session / VWAP / volume-range / news), not just news —
                # otherwise sessions and VWAP anchors are broker-time-shifted.
                pipeline.set_clock_offset(offset_min)
                log.info("feature_engine_broker_clock_offset", offset_minutes=offset_min)
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            log.warning("feature_engine_warmup_failed", error=str(exc))
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                connector.shutdown()

    handler = _make_handler(settings, pipeline, publisher, buffer, vp_buffer)
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
