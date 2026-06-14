"""Common / cross-cutting infrastructure: config, schemas, messaging, logging.

The four subpackages are independent and reusable across all services:

* :mod:`xauusd_bot.common.config` — :class:`Settings` (Pydantic-Settings)
* :mod:`xauusd_bot.common.schemas` — domain Pydantic schemas
* :mod:`xauusd_bot.common.messaging` — Redis Streams wrapper
* :mod:`xauusd_bot.common.logging` — structlog setup
"""
