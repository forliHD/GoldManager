"""Redis Streams wrapper — publisher, consumer, consumer groups.

Design choices
--------------
* Topics are an :class:`enum.StrEnum` so the compiler (and humans) catch
  typos at the call site. The set is fixed; new topics require editing
  this file (intentional — we want a small, stable surface).
* Messages are Pydantic models serialized to JSON. ``schema_version`` is
  part of every event; unknown versions are dropped at the boundary.
* The :class:`Consumer` is at-least-once. Callers must make their
  handlers idempotent (e.g. use ``client_order_id`` as the dedupe key).
* The :class:`Publisher` reuses a single :class:`redis.asyncio.Redis`
  client per process — connections are pooled.

This module does **not** import any of the bot's service code, so it
is safe to use from any process, including the dashboard.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

import structlog
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis  # type: ignore[import-not-found]

T = TypeVar("T", bound=BaseModel)

log = structlog.get_logger(__name__)


class StreamTopic(str, Enum):
    """The fixed set of stream topics the bot uses."""

    MARKET_TICKS = "market_ticks"
    # Forming (not-yet-closed) bar, republished frequently for live chart
    # animation only. NOT consumed by the trading pipeline (which decides on
    # closed bars from MARKET_TICKS). Tightly capped — only the latest matters.
    MARKET_LIVE = "market_live"
    # Historical bars for CHART CONTEXT ONLY (dashboard reads it). The trading
    # pipeline (feature/decision/execution) NEVER consumes this — that is the
    # whole point: history must not flow through as if it were live.
    CHART_HISTORY = "chart_history"
    FEATURES = "features"
    DECISIONS = "decisions"
    ORDERS = "orders"
    JOURNAL = "journal"


TOPICS: tuple[StreamTopic, ...] = tuple(StreamTopic)


def _default_serializer(obj: Any) -> str:
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Cannot serialize {type(obj).__name__}")


def _to_json(model: BaseModel) -> dict[str, str]:
    """Serialize a Pydantic model into the flat ``{key: str}`` shape Redis Streams wants."""

    raw = model.model_dump(mode="json")
    return {"payload": json.dumps(raw, default=_default_serializer)}


def _from_json(data: dict[str, str], model_cls: type[T]) -> T:
    """Deserialize the ``payload`` field back into a Pydantic model."""

    if "payload" not in data:
        raise ValueError("Stream message missing 'payload' field")
    obj = json.loads(data["payload"])
    return model_cls.model_validate(obj)


@dataclass
class StreamMessage:
    """A consumed message with its stream-side id."""

    id: str
    topic: StreamTopic
    payload: BaseModel
    raw: dict[str, str]


class Publisher:
    """Publish Pydantic events to a stream.

    Parameters
    ----------
    redis_url:
        Connection URL, e.g. ``redis://localhost:6379/0``.
    maxlen:
        Default approximate cap on stream length (XADD ``MAXLEN ~``) for
        topics without an override. Set higher for hot streams; lower for
        cold ones.
    maxlen_overrides:
        Per-topic cap overriding ``maxlen``. Essential because payload
        sizes differ by ~1000×: a ``market_ticks`` bar is ~350 bytes while
        a ``features``/``decisions`` event carries the full
        ``FeatureSnapshotBundle`` (~800 KB). A single global cap therefore
        cannot bound Redis memory for both — see ``make_publisher``.
    """

    def __init__(
        self,
        redis_url: str,
        maxlen: int = 1_000_000,
        maxlen_overrides: dict[str, int] | None = None,
    ) -> None:
        self._url = redis_url
        self._maxlen = maxlen
        self._maxlen_overrides = dict(maxlen_overrides or {})
        self._client: Redis | None = None

    async def connect(self) -> None:
        if self._client is None:
            import redis.asyncio as aioredis  # type: ignore[import-not-found]

            self._client = aioredis.from_url(self._url, encoding="utf-8", decode_responses=True)
            log.info("publisher_connected", url=self._url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def publish(self, topic: StreamTopic, model: BaseModel) -> str:
        """Publish ``model`` to ``topic`` and return the assigned stream id."""

        if self._client is None:
            await self.connect()
        assert self._client is not None
        data = _to_json(model)
        msg_id = await self._client.xadd(
            topic.value,
            data,
            maxlen=self._maxlen_overrides.get(topic.value, self._maxlen),
            approximate=True,
        )
        log.debug("stream_published", topic=topic.value, id=msg_id, kind=getattr(model, "kind", "?"))
        return msg_id


class Consumer:
    """Consume a stream with consumer-group semantics.

    Parameters
    ----------
    redis_url:
        Connection URL.
    topic:
        Which stream to consume.
    group:
        Consumer-group name. Each service should have its own group
        (e.g. ``feature-engine-v1``).
    consumer_name:
        Unique name within the group. If ``None`` the redis client
        generates one.
    block_ms:
        ``XREADGROUP`` block timeout in milliseconds.
    batch_size:
        Maximum number of messages to fetch per iteration.
    """

    def __init__(
        self,
        redis_url: str,
        topic: StreamTopic,
        group: str,
        *,
        consumer_name: str | None = None,
        block_ms: int = 1000,
        batch_size: int = 64,
    ) -> None:
        self._url = redis_url
        self._topic = topic
        self._group = group
        self._consumer_name = consumer_name
        self._block_ms = block_ms
        self._batch_size = batch_size
        self._client: Redis | None = None

    async def connect(self) -> None:
        if self._client is None:
            import redis.asyncio as aioredis  # type: ignore[import-not-found]

            self._client = aioredis.from_url(self._url, encoding="utf-8", decode_responses=True)
            await self._ensure_group()
            log.info("consumer_connected", topic=self._topic.value, group=self._group)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_group(self) -> None:
        assert self._client is not None
        try:
            await self._client.xgroup_create(
                name=self._topic.value,
                groupname=self._group,
                id="0",  # start from beginning; in prod use "$" to ignore backlog
                mkstream=True,
            )
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP is the expected race
            if "BUSYGROUP" not in str(exc):
                raise

    async def consume(
        self,
        handler: Callable[[StreamMessage], Awaitable[None]],
        model_cls: type[T],
        *,
        block_ms: int | None = None,
    ) -> int:
        """Consume one batch, hand each to ``handler``, ack on success.

        Returns the number of messages successfully processed. On
        handler exception the message is **not** acked; on the next
        call (or after restart) it will be redelivered.
        """

        if self._client is None:
            await self.connect()
        assert self._client is not None
        block = block_ms if block_ms is not None else self._block_ms
        response = await self._client.xreadgroup(
            groupname=self._group,
            consumername=self._consumer_name or "",
            streams={self._topic.value: ">"},
            count=self._batch_size,
            block=block,
        )
        if not response:
            return 0

        processed = 0
        for _stream, entries in response:
            for entry_id, data in entries:
                try:
                    payload = _from_json(data, model_cls)
                except Exception as exc:  # noqa: BLE001 - poison message
                    log.error(
                        "stream_message_decode_failed",
                        topic=self._topic.value,
                        id=entry_id,
                        error=str(exc),
                    )
                    # Ack and drop — the alternative is a poison-pill loop.
                    await self._client.xack(self._topic.value, self._group, entry_id)
                    continue
                msg = StreamMessage(id=entry_id, topic=self._topic, payload=payload, raw=data)
                try:
                    await handler(msg)
                except Exception as exc:  # noqa: BLE001 - caller decides
                    log.error(
                        "stream_handler_failed",
                        topic=self._topic.value,
                        id=entry_id,
                        error=str(exc),
                    )
                    # Do not ack — will be redelivered.
                    continue
                await self._client.xack(self._topic.value, self._group, entry_id)
                processed += 1
        return processed

    async def run_forever(
        self,
        handler: Callable[[StreamMessage], Awaitable[None]],
        model_cls: type[T],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Run :meth:`consume` in a loop until ``stop_event`` is set."""

        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                await self.consume(handler, model_cls)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.error("consumer_loop_error", error=str(exc))
                await asyncio.sleep(1.0)
