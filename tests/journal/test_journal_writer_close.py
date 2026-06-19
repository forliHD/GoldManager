"""journal-writer: a ``trade_close`` event finalises the open trade."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from xauusd_bot.common.messaging.events import JournalEvent
from xauusd_bot.common.schemas.journal import ExitReasonTag, TradeCloseUpdate
from xauusd_bot.journal.store import InMemoryJournalStore
from xauusd_bot.journal_writer import _make_handler

from .test_store import make_trade


@pytest.mark.asyncio
async def test_trade_close_event_updates_open_trade() -> None:
    store = InMemoryJournalStore()
    open_t = make_trade(
        timestamp_open=datetime(2026, 6, 18, 20, 50, tzinfo=UTC),
        order_ids=["1459563795"],
        entry_price=Decimal("4231.23"),
        stop_loss=Decimal("4233.40"),
    )
    await store.write_trade(open_t)

    handle = _make_handler(store)
    ev = JournalEvent(
        symbol="XAUUSD+",
        entry_type="trade_close",
        trade_close=TradeCloseUpdate(
            order_id="1459563795",
            timestamp_close=datetime(2026, 6, 18, 21, 5, tzinfo=UTC),
            exit_price=Decimal("4226.50"),
            pnl_realized=Decimal("103.40"),
            r_multiple=2.18,
            exit_reason=ExitReasonTag.TP1_HIT,
        ),
    )
    await handle(SimpleNamespace(payload=ev))

    updated = await store.get_trade(open_t.id)
    assert updated is not None
    assert updated.exit_price == Decimal("4226.50")
    assert updated.pnl_realized == Decimal("103.40")
    assert updated.r_multiple == 2.18
    assert updated.exit_reason == ExitReasonTag.TP1_HIT
    assert updated.timestamp_close == datetime(2026, 6, 18, 21, 5, tzinfo=UTC)


@pytest.mark.asyncio
async def test_trade_close_no_match_is_noop() -> None:
    store = InMemoryJournalStore()
    handle = _make_handler(store)
    ev = JournalEvent(
        symbol="XAUUSD+",
        entry_type="trade_close",
        trade_close=TradeCloseUpdate(
            order_id="does-not-exist",
            timestamp_close=datetime.now(tz=UTC),
            exit_price=Decimal("4200"),
            exit_reason=ExitReasonTag.MANUAL,
        ),
    )
    # Must not raise when no open trade carries the ticket.
    await handle(SimpleNamespace(payload=ev))
