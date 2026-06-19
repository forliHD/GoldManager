"""Redis Streams wrapper — topics, consumer groups, at-least-once.

The bot is composed of five services communicating over Redis Streams:

* ``market_ticks``  — raw ticks / bars from the connector
* ``features``      — :class:`FeatureSnapshot` events
* ``decisions``     — :class:`Decision` events
* ``orders``        — :class:`OrderEvent` events
* ``journal``       — :class:`JournalEntry` events (durable)

Each consumer uses a *consumer group* so multiple instances of a service
can share the load. The wrapper provides:

* :class:`Publisher` — publish a Pydantic event (serialized as JSON).
* :class:`Consumer`  — read events with at-least-once semantics, calling
  a handler. The handler is responsible for idempotency.

Acknowledgements happen *after* the handler returns successfully; on
exception the message is NOT acked and will be redelivered on the next
``consume`` call.
"""

from xauusd_bot.common.messaging.streams import (
    TOPICS,
    Consumer,
    Publisher,
    StreamMessage,
    StreamTopic,
)

__all__ = [
    "Consumer",
    "Publisher",
    "StreamMessage",
    "StreamTopic",
    "TOPICS",
]
