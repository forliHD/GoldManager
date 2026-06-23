"""Service runtime scaffolding shared by all stream-connected services.

The five trading services (data-collector, feature-engine,
decision-engine, execution-engine, journal-writer) all need the same
plumbing around their domain logic:

* **Graceful shutdown** — SIGINT/SIGTERM flip an :class:`asyncio.Event`
  so the consumer loop drains its current batch and exits cleanly
  (important under ``docker stop``, which sends SIGTERM then SIGKILL).
* **Heartbeat** — every service touches ``logs/<role>.alive`` on a
  timer. The compose healthcheck stats that file's mtime, so a wedged
  event loop (heartbeat stops) is visible as an unhealthy container.
* **Publisher / Consumer construction** — one place that reads
  ``settings.redis_url`` and names consumer groups/consumers.

This module imports the messaging layer but no engine code, so it stays
cheap to import from every service entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import structlog
from pydantic import BaseModel

from xauusd_bot.common.config import ServiceRole, Settings
from xauusd_bot.common.messaging.streams import (
    Consumer,
    Publisher,
    StreamMessage,
    StreamTopic,
)

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_DIR = Path("logs")


def heartbeat_path(role: ServiceRole | str, *, directory: Path = HEARTBEAT_DIR) -> Path:
    """Return the ``logs/<role>.alive`` path for ``role``."""

    name = role.value if isinstance(role, ServiceRole) else str(role)
    return directory / f"{name}.alive"


def _consumer_name(group: str) -> str:
    """A name unique to this process within the consumer group."""

    return f"{group}-{socket.gethostname()}-{os.getpid()}"


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to ``stop_event.set`` on the running loop.

    Falls back silently where the platform/loop does not support signal
    handlers (e.g. non-main thread, Windows ProactorEventLoop) — there
    the loop simply runs until cancelled.
    """

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover - platform dependent
            log.debug("signal_handler_unavailable", signal=sig)


async def _heartbeat_loop(
    role: ServiceRole | str,
    stop_event: asyncio.Event,
    *,
    interval: float,
    directory: Path,
) -> None:
    path = heartbeat_path(role, directory=directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = role.value if isinstance(role, ServiceRole) else str(role)
    while not stop_event.is_set():
        try:
            path.write_text(datetime.now(tz=UTC).isoformat())
        except OSError as exc:  # pragma: no cover - disk full / readonly fs
            log.warning("heartbeat_write_failed", role=name, error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)


@contextlib.asynccontextmanager
async def service_runtime(
    role: ServiceRole | str,
    *,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_dir: Path = HEARTBEAT_DIR,
) -> AsyncIterator[asyncio.Event]:
    """Async context manager wrapping a service's lifecycle.

    Installs signal handlers, starts the heartbeat task, and yields the
    shared ``stop_event``. On exit (normal or exceptional) it sets the
    event and tears the heartbeat task down.

    Usage::

        async with service_runtime(ServiceRole.DATA_COLLECTOR) as stop:
            while not stop.is_set():
                ...
    """

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)
    hb_task = asyncio.create_task(
        _heartbeat_loop(role, stop_event, interval=heartbeat_interval, directory=heartbeat_dir),
        name=f"heartbeat-{role}",
    )
    name = role.value if isinstance(role, ServiceRole) else str(role)
    log.info("service_started", role=name)
    try:
        yield stop_event
    finally:
        stop_event.set()
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task
        log.info("service_stopped", role=name)


def make_publisher(settings: Settings, *, maxlen: int | None = None) -> Publisher:
    """Construct a :class:`Publisher` bound to ``settings.redis_url``.

    ``maxlen`` defaults to ``settings.stream_maxlen`` (small-payload streams).
    The bundle-carrying streams (features/decisions, ~800 KB each) get the much
    smaller ``settings.stream_maxlen_large`` cap so Redis memory stays bounded —
    a single global cap cannot serve both (see Settings.stream_maxlen_large).
    """

    base = maxlen if maxlen is not None else settings.stream_maxlen
    overrides = {
        StreamTopic.FEATURES.value: settings.stream_maxlen_large,
        StreamTopic.DECISIONS.value: settings.stream_maxlen_large,
        # Forming-bar animation channel — only the latest entry is ever read
        # (dashboard XREADs with a $ cursor), so keep it tiny.
        StreamTopic.MARKET_LIVE.value: 200,
        # Chart-only history — must hold the whole backfill (chart_history_bars)
        # plus headroom for live bars, else the deepest views (H1) get starved.
        StreamTopic.CHART_HISTORY.value: max(5000, settings.chart_history_bars + 2000),
    }
    return Publisher(settings.redis_url, maxlen=base, maxlen_overrides=overrides)


def make_consumer(
    settings: Settings,
    topic: StreamTopic,
    group: str,
    *,
    block_ms: int = 1000,
    batch_size: int = 64,
) -> Consumer:
    """Construct a :class:`Consumer` for ``topic`` with a per-process consumer name."""

    return Consumer(
        settings.redis_url,
        topic,
        group,
        consumer_name=_consumer_name(group),
        block_ms=block_ms,
        batch_size=batch_size,
    )


async def run_consumer_service(
    role: ServiceRole,
    settings: Settings,
    *,
    topic: StreamTopic,
    group: str,
    model_cls: type[T],
    handler: Callable[[StreamMessage], Awaitable[None]],
    on_start: Callable[[], Awaitable[None]] | None = None,
    on_stop: Callable[[], Awaitable[None]] | None = None,
    block_ms: int = 1000,
    batch_size: int = 64,
) -> int:
    """Run a consume-forever service with full lifecycle management.

    Returns a process exit code (0 on clean shutdown). The ``handler``
    must be idempotent — the underlying :class:`Consumer` is
    at-least-once and redelivers on failure.
    """

    consumer = make_consumer(settings, topic, group, block_ms=block_ms, batch_size=batch_size)
    async with service_runtime(role) as stop_event:
        try:
            if on_start is not None:
                await on_start()
            log.info(
                "service_consuming",
                role=role.value,
                topic=topic.value,
                group=group,
            )
            await consumer.run_forever(handler, model_cls, stop_event=stop_event)
        finally:
            await consumer.close()
            if on_stop is not None:
                await on_stop()
    return 0


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "heartbeat_path",
    "install_signal_handlers",
    "make_consumer",
    "make_publisher",
    "run_consumer_service",
    "service_runtime",
]
