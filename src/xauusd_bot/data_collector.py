"""data-collector service — feeds closed bars onto the ``market_ticks`` stream.

In ``CONNECTOR_MODE=replay`` it streams the configured parquet/CSV
bar-by-bar (as fast as possible by default, or paced by
``REPLAY_SPEED_SECONDS``). In ``CONNECTOR_MODE=live`` it polls the MT5
bridge for newly closed M1 bars and forwards them.

Entry point for ``SERVICE_ROLE=data-collector`` (see
:mod:`xauusd_bot.docker_entrypoint`).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.events import BarClosedEvent
from xauusd_bot.common.messaging.streams import StreamTopic
from xauusd_bot.common.service import make_publisher, service_runtime
from xauusd_bot.connectors.factory import make_connector

log = structlog.get_logger(__name__)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _idle_until_stop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5.0)


async def _run_replay(settings: Settings, connector, publisher, stop: asyncio.Event) -> None:
    symbol = settings.symbol
    delay = settings.replay_speed_seconds
    bars_df = connector.bars
    n = len(bars_df)
    log.info("replay_collector_starting", bars=n, speed_seconds=delay, loop=settings.replay_loop)
    while not stop.is_set():
        published = 0
        for i in range(n):
            if stop.is_set():
                break
            bar = connector._row_to_bar(bars_df.iloc[i], "M1")  # noqa: SLF001 - internal API, as in the smokes
            await publisher.publish(StreamTopic.MARKET_TICKS, BarClosedEvent(symbol=symbol, bar=bar))
            published += 1
            await _sleep_or_stop(stop, delay)
        log.info("replay_exhausted", published=published, loop=settings.replay_loop)
        if not settings.replay_loop:
            break
    await _idle_until_stop(stop)


async def _backfill_history(settings: Settings, connector, publisher) -> datetime | None:
    """One-time history backfill into ``market_ticks`` for chart context.

    Gated on stream length so a restart with history already present is a
    no-op. Returns the last backfilled bar time (so the live loop won't
    re-publish it) or ``None``.
    """
    n = settings.live_backfill_bars
    if n <= 0:
        return None
    import redis.asyncio as aioredis

    r = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    try:
        existing = await r.xlen(StreamTopic.MARKET_TICKS.value)
    except Exception:  # noqa: BLE001 - if we can't tell, attempt the backfill
        existing = 0
    finally:
        await r.aclose()
    if existing >= n // 2:
        log.info("live_backfill_skipped", existing=existing)
        return None
    try:
        history = await asyncio.to_thread(connector.get_rates, settings.symbol, "M1", n)
    except Exception as exc:  # noqa: BLE001 - backfill is best-effort
        log.warning("live_backfill_failed", error=str(exc))
        return None
    last = None
    for bar in history:
        await publisher.publish(StreamTopic.MARKET_TICKS, BarClosedEvent(symbol=settings.symbol, bar=bar))
        last = bar.time
    log.info("live_backfill_published", bars=len(history))
    return last


async def _run_live(settings: Settings, connector, publisher, stop: asyncio.Event) -> None:
    symbol = settings.symbol
    log.info("live_collector_starting", symbol=symbol)
    last_time = await _backfill_history(settings, connector, publisher)
    while not stop.is_set():
        try:
            recent = connector.get_rates(symbol, "M1", count=2)
        except Exception as exc:  # noqa: BLE001 - keep polling through transient bridge errors
            log.warning("live_collector_get_rates_failed", error=str(exc))
            await _sleep_or_stop(stop, 1.0)
            continue
        # The most recent fully closed bar is the second-to-last (the last
        # one is still forming). Publish it once when its time advances.
        closed = recent[-2] if len(recent) >= 2 else (recent[-1] if recent else None)
        if closed is not None and closed.time != last_time:
            await publisher.publish(StreamTopic.MARKET_TICKS, BarClosedEvent(symbol=symbol, bar=closed))
            last_time = closed.time
            log.debug("live_bar_published", time=str(closed.time))
        # Forming bar → MARKET_LIVE every poll, for live chart animation only
        # (the trading pipeline never reads this; it decides on closed bars).
        forming = recent[-1] if recent else None
        if forming is not None:
            await publisher.publish(StreamTopic.MARKET_LIVE, BarClosedEvent(symbol=symbol, bar=forming))
        await _sleep_or_stop(stop, 1.0)


async def _run(settings: Settings) -> int:
    connector = make_connector(settings)
    publisher = make_publisher(settings)
    await publisher.connect()
    async with service_runtime(ServiceRole.DATA_COLLECTOR) as stop:
        try:
            if settings.is_live_connector():
                await _run_live(settings, connector, publisher, stop)
            else:
                await _run_replay(settings, connector, publisher, stop)
        finally:
            await publisher.close()
            with contextlib.suppress(Exception):
                connector.shutdown()
    return 0


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
