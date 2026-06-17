"""Redis subscriber for dashboard WebSocket broadcasts (Block 9).

The :class:`RedisSubscriber` consumes Redis Streams (``market_ticks``,
``features``, ``decisions``, ``orders``, ``journal``) and forwards
each entry to the WebSocket broker :mod:`xauusd_bot.dashboard.websocket`.

Stream contract
---------------
* Source streams live on the dashboard-streams Redis URL (default
  same as ``settings.redis_url``, see
  :setting:`Settings.dashboard_redis_streams_url`).
* Each stream entry is JSON-decoded into ``{"topic": "...", "data":
  {...}, "ts": "..."}``. The subscriber forwards the parsed payload
  to the broker.
* If an entry cannot be JSON-decoded, it is logged at WARNING and
  dropped — never crashes the loop.

Reconnect logic
---------------
The subscriber uses an infinite retry loop with exponential backoff
(capped at 5s) so a temporary Redis blip does not kill the dashboard.
On graceful shutdown (``stop()``) the loop exits cleanly.

Why not pub/sub?
----------------
The 07_devops_docker_viz.md contract uses Redis Streams (XADD / XREAD)
for the trading pipeline so entries are durable across restarts. The
dashboard uses XREAD with ``$`` (last-id) cursor + a small BLOCK to
get near-real-time updates with at-least-once delivery semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable, Coroutine

import structlog

from xauusd_bot.common.config import Settings

log = structlog.get_logger(__name__)
_ = logging  # symmetry with rest of codebase

# Streams we subscribe to, in read-order. The matching WebSocket topic
# is the same name (minus the "stream_" prefix we don't have here).
DEFAULT_STREAMS: tuple[str, ...] = (
    "market_ticks",
    "features",
    "decisions",
    "orders",
    "journal",
)

# Redis stream name → WebSocket topic name. They match except for the bar
# stream, which is ``market_ticks`` on Redis but the broker/frontend call
# the WS topic ``ticks``. Without this mapping the broker rejects every bar
# as an unknown topic.
_STREAM_TO_WS_TOPIC: dict[str, str] = {"market_ticks": "ticks"}

# XREAD block timeout (ms). Short enough to keep shutdown latency low.
_BLOCK_MS = 1000


# Callback signature: a coroutine that takes a parsed payload dict and
# returns nothing. The broker registers one of these per topic.
BroadcastFn = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class RedisSubscriber:
    """Async consumer of Redis Streams for the dashboard.

    Parameters
    ----------
    settings:
        :class:`Settings` — we read
        ``settings.dashboard_redis_streams_url`` (default
        ``settings.redis_url``).
    broadcast:
        Async callable ``(topic: str, payload: dict) -> None`` —
        typically ``broker.broadcast``. The subscriber does not
        import the broker to avoid a circular import; it just calls
        what it's given.
    streams:
        Tuple of stream names to subscribe to. Default
        :data:`DEFAULT_STREAMS`.
    """

    def __init__(
        self,
        settings: Settings,
        broadcast: BroadcastFn,
        *,
        streams: tuple[str, ...] = DEFAULT_STREAMS,
    ) -> None:
        url = settings.dashboard_redis_streams_url or settings.redis_url
        self._url = url
        self._broadcast = broadcast
        self._streams = tuple(streams)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._redis = None  # type: ignore[assignment]

    @property
    def streams(self) -> tuple[str, ...]:
        return self._streams

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ============================================================ lifecycle

    async def start(self) -> None:
        """Spawn the consume loop as a background asyncio task."""

        if self.is_running:
            log.warning("redis_subscriber_already_running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="dashboard-redis-subscriber")
        log.info(
            "redis_subscriber_started",
            streams=list(self._streams),
            url=self._url,
        )

    async def stop(self) -> None:
        """Signal the consume loop to exit; close the redis connection."""

        if not self.is_running:
            return
        self._stop.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("redis_subscriber_stop_timeout")
                task.cancel()
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass
        log.info("redis_subscriber_stopped")

    # ============================================================ internals

    async def _run(self) -> None:
        """Consume loop with reconnect-on-failure."""

        import redis.asyncio as redis_async
        import redis.exceptions

        backoff = 0.5
        max_backoff = 5.0
        while not self._stop.is_set():
            try:
                self._redis = redis_async.from_url(
                    self._url, encoding="utf-8", decode_responses=True
                )
                await self._consume(self._redis)
                backoff = 0.5  # clean exit → reset
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "redis_subscriber_error_will_reconnect",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    backoff_seconds=backoff,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, max_backoff)
            finally:
                if self._redis is not None:
                    try:
                        await self._redis.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                    self._redis = None

    async def _consume(self, redis) -> None:
        """Single connect-and-consume cycle.

        We start each stream at the last entry (XREAD with id "$") and
        then BLOCK waiting for new ones. Reconnect triggers a fresh
        re-read from "$" — at-least-once semantics during the
        disconnect window.
        """

        last_ids: dict[str, str] = {stream: "$" for stream in self._streams}
        while not self._stop.is_set():
            try:
                response = await redis.xread(
                    {stream: last_ids[stream] for stream in self._streams},
                    block=_BLOCK_MS,
                    count=64,
                )
            except Exception as exc:  # noqa: BLE001
                # Bubble up to the reconnect loop.
                raise exc
            if not response:
                continue
            # response shape: [(stream_name, [(entry_id, {fields...}), ...]), ...]
            for stream_name, entries in response:
                topic = _STREAM_TO_WS_TOPIC.get(stream_name, stream_name)
                for entry_id, fields in entries:
                    last_ids[stream_name] = entry_id
                    payload = _decode_entry(topic, entry_id, fields)
                    if payload is None:
                        continue
                    try:
                        await self._broadcast(topic, payload)
                    except Exception as exc:  # noqa: BLE001
                        # Broadcast failures must not kill the consumer.
                        log.warning(
                            "redis_subscriber_broadcast_failed",
                            topic=topic,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )


def _decode_entry(topic: str, entry_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Parse an XREAD entry into a broadcast payload.

    Two shapes are accepted:

    1. ``fields`` has a ``payload`` JSON string — we decode it
       (canonical contract from the trading pipeline).
    2. ``fields`` is already a flat dict — we forward the entries
       directly (useful for tests / dev-mode producers).
    """

    if "payload" in fields and isinstance(fields["payload"], str):
        try:
            data = json.loads(fields["payload"])
        except (TypeError, ValueError) as exc:
            log.warning(
                "redis_subscriber_bad_payload",
                topic=topic,
                entry_id=entry_id,
                error=str(exc),
            )
            return None
        if not isinstance(data, dict):
            log.warning(
                "redis_subscriber_payload_not_object",
                topic=topic,
                entry_id=entry_id,
                payload_type=type(data).__name__,
            )
            return None
        return {
            "topic": data.get("topic", topic),
            "data": data.get("data", data),
            "ts": data.get("ts", datetime.now(tz=UTC).isoformat()),
        }
    # Fallback: flat-fields shape. We re-wrap so the broker can use a
    # uniform shape.
    return {
        "topic": topic,
        "data": dict(fields),
        "ts": datetime.now(tz=UTC).isoformat(),
    }


__all__ = ["DEFAULT_STREAMS", "RedisSubscriber"]
