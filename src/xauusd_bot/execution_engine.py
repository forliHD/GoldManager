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
from xauusd_bot.common.runtime_config import (
    STATE_KEY_ACCOUNT,
    STATE_KEY_POSITIONS,
    STATE_KEY_RISK,
    get_emergency_stop,
    set_json,
)
from xauusd_bot.common.service import make_consumer, make_publisher, service_runtime
from xauusd_bot.connectors.factory import make_connector
from xauusd_bot.execution.pipeline import ExecutionPipeline

log = structlog.get_logger(__name__)

GROUP = "execution-engine-v1"

# How often the operational state (account/positions/risk) is snapshotted
# to Redis for the dashboard's live cockpit.
_STATE_INTERVAL_SECONDS = 3.0


def _account_snapshot(acc) -> dict:
    def f(v):
        return float(v) if v is not None else None

    return {
        "balance": f(acc.balance),
        "equity": f(acc.equity),
        "margin": f(acc.margin),
        "free_margin": f(acc.free_margin),
        "currency": acc.currency,
        "leverage": acc.leverage,
        "daily_pnl": f(acc.daily_pnl),
        "weekly_pnl": f(acc.weekly_pnl),
        "current_spread": f(acc.current_spread),
        "trade_allowed": acc.trade_allowed,
        "server_time": acc.server_time.isoformat() if acc.server_time else None,
        "ts": datetime.now(tz=UTC).isoformat(),
    }


def _risk_snapshot(acc, settings: Settings, n_positions: int) -> dict:
    balance = float(acc.balance) if acc.balance is not None else 0.0
    daily_pnl = float(acc.daily_pnl) if acc.daily_pnl is not None else 0.0
    weekly_pnl = float(acc.weekly_pnl) if acc.weekly_pnl is not None else 0.0
    spread = float(acc.current_spread) if acc.current_spread is not None else None
    return {
        "daily_pnl": daily_pnl,
        "daily_loss_cap": -balance * settings.risk_max_daily,
        "daily_cap_pct": settings.risk_max_daily,
        "weekly_pnl": weekly_pnl,
        "weekly_loss_cap": -balance * settings.risk_max_weekly,
        "weekly_cap_pct": settings.risk_max_weekly,
        "open_positions": n_positions,
        "max_open_positions": settings.risk_max_open_positions,
        "max_trades_per_session": settings.risk_max_trades_per_session,
        "spread_max_pips": settings.spread_max_pips,
        "current_spread_pips": (spread / 10.0) if spread is not None else None,
        "ts": datetime.now(tz=UTC).isoformat(),
    }


async def _sync_emergency(pipeline: ExecutionPipeline, redis_client) -> None:
    """Mirror the dashboard kill-switch onto the EmergencyStopManager.

    Engaging flattens + cancels the book and halts new entries (the
    RiskManager refuses while the stop is active); clearing releases it.
    """

    engaged = await get_emergency_stop(redis_client)
    active = pipeline.emergency.is_active()
    if engaged and not active:
        await asyncio.to_thread(pipeline.emergency.manual_trigger, "dashboard")
        log.warning("execution_emergency_engaged_from_dashboard")
    elif not engaged and active:
        await asyncio.to_thread(pipeline.emergency.clear)
        log.info("execution_emergency_cleared_from_dashboard")


async def _publish_state(
    pipeline: ExecutionPipeline,
    connector,
    settings: Settings,
    redis_client,
    stop_event: asyncio.Event,
) -> None:
    """Snapshot account / positions / risk to Redis on a timer for the dashboard.

    Also syncs the operator kill-switch each tick (≤3s latency).
    """

    while not stop_event.is_set():
        try:
            await _sync_emergency(pipeline, redis_client)
        except Exception as exc:  # noqa: BLE001 - never let the kill-switch sync crash the loop
            log.warning("execution_emergency_sync_failed", error=str(exc))
        try:
            # Connector calls are sync (RPyC can block) — run off the loop.
            acc = await asyncio.to_thread(connector.get_account)
            positions = await asyncio.to_thread(connector.positions_get, settings.symbol)
            pos_snap = [
                {
                    "id": p.position_id,
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "volume": float(p.volume),
                    "open_price": float(p.open_price),
                    "sl": float(p.sl) if p.sl is not None else None,
                    "tp": float(p.tp) if p.tp is not None else None,
                    "profit": float(p.profit),
                    "open_time": p.open_time.isoformat() if p.open_time else None,
                }
                for p in positions
            ]
            await set_json(redis_client, STATE_KEY_ACCOUNT, _account_snapshot(acc))
            await set_json(redis_client, STATE_KEY_POSITIONS, pos_snap)
            await set_json(redis_client, STATE_KEY_RISK, _risk_snapshot(acc, settings, len(positions)))
        except Exception as exc:  # noqa: BLE001 - never let state publishing kill the service
            log.warning("execution_state_publish_failed", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=_STATE_INTERVAL_SECONDS)


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
    import redis.asyncio as aioredis

    connector = make_connector(settings)
    pipeline = ExecutionPipeline(settings, connector)
    publisher = make_publisher(settings)
    await publisher.connect()
    state_redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    consumer = make_consumer(
        settings,
        StreamTopic.DECISIONS,
        GROUP,
        block_ms=settings.stream_block_ms,
        batch_size=settings.stream_batch_size,
    )
    handler = _make_handler(pipeline, publisher)

    # service_runtime gives us the shared stop_event so the consumer loop
    # and the state-publisher task shut down together.
    async with service_runtime(ServiceRole.EXECUTION_ENGINE) as stop:
        state_task = asyncio.create_task(
            _publish_state(pipeline, connector, settings, state_redis, stop),
            name="execution-state-publisher",
        )
        log.info("execution_engine_consuming", topic=StreamTopic.DECISIONS.value, group=GROUP)
        try:
            await consumer.run_forever(handler, DecisionEvent, stop_event=stop)
        finally:
            state_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state_task
            await consumer.close()
            await publisher.close()
            await state_redis.aclose()
            with contextlib.suppress(Exception):
                connector.shutdown()
    return 0


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
