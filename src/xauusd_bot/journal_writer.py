"""journal-writer service — consumes ``journal``, persists records.

The sink at the end of the pipeline. Each :class:`JournalEvent` carries
one record (trade / order / feature snapshot); this service writes it to
the configured :class:`JournalStore`.

Entry point for ``SERVICE_ROLE=journal-writer``.

**Durability seam (point 3).** ``TimescaleJournalStore`` is still a stub
(raises ``NotImplementedError``), so this writer falls back to
:class:`InMemoryJournalStore` — meaning records live only in this
process and are lost on restart. Wiring real TimescaleDB persistence
(asyncpg + hypertables) is the next step; this service is the consumer
that will use it unchanged once it lands.
"""

from __future__ import annotations

import asyncio

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.events import ENVELOPE_SCHEMA_VERSION, JournalEvent
from xauusd_bot.common.messaging.streams import StreamMessage, StreamTopic
from xauusd_bot.common.service import run_consumer_service
from xauusd_bot.journal import InMemoryJournalStore, get_journal_store_with_fallback

log = structlog.get_logger(__name__)

GROUP = "journal-writer-v1"


def _make_handler(store):
    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, JournalEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("journal_writer_dropping_unknown_version", version=ev.schema_version)
            return
        if ev.entry_type == "trade" and ev.trade is not None:
            await store.write_trade(ev.trade)
        elif ev.entry_type == "order" and ev.order is not None:
            await store.write_order(ev.order)
        elif ev.entry_type == "feature_snapshot" and ev.snapshot is not None:
            await store.write_feature_snapshot(ev.snapshot)
        elif ev.entry_type == "decision" and ev.decision is not None:
            await store.write_decision(ev.decision)
        elif ev.entry_type == "trade_close" and ev.trade_close is not None:
            tc = ev.trade_close
            trade = await store.get_trade_by_order_id(tc.order_id)
            if trade is None:
                log.warning("journal_writer_trade_close_no_match", order_id=tc.order_id)
                return
            updates: dict[str, object] = {
                "timestamp_close": tc.timestamp_close,
                "exit_price": tc.exit_price,
                "exit_reason": tc.exit_reason,
            }
            if tc.pnl_realized is not None:
                updates["pnl_realized"] = tc.pnl_realized
            if tc.r_multiple is not None:
                updates["r_multiple"] = tc.r_multiple
            await store.update_trade(trade.id, updates)
            log.info("journal_writer_trade_closed", order_id=tc.order_id, trade_id=str(trade.id))
        else:
            log.warning("journal_writer_empty_event", entry_type=ev.entry_type)

    return handle


async def _run(settings: Settings) -> int:
    # TimescaleJournalStore when a DSN is set and reachable; otherwise the
    # fallback degrades to InMemory so the pipeline never blocks on the DB.
    store = await get_journal_store_with_fallback(settings)
    kind = "in_memory" if isinstance(store, InMemoryJournalStore) else "timescale"
    log.info("journal_writer_store_ready", store=kind)

    handler = _make_handler(store)
    return await run_consumer_service(
        ServiceRole.JOURNAL_WRITER,
        settings,
        topic=StreamTopic.JOURNAL,
        group=GROUP,
        model_cls=JournalEvent,
        handler=handler,
        block_ms=settings.stream_block_ms,
        batch_size=settings.stream_batch_size,
    )


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
