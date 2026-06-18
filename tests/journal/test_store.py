"""Tests for the journal store backends (Block 5a)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
)
from xauusd_bot.common.schemas.journal import (
    ExitReasonTag,
    FeatureSnapshotRecord,
    LLMFallbackDiscrepancy,
    LLMFallbackDiscrepancyV2,
    OrderRecord,
    OrderStatusTag,
    TradeRecord,
)
from xauusd_bot.connectors.schemas import OrderSide, OrderType
from xauusd_bot.journal import (
    InMemoryJournalStore,
    JournalStore,
    JournalStoreError,
    PITViolationError,
    TimescaleJournalStore,
    TradeNotFoundError,
    get_journal_store,
    get_journal_store_with_fallback,
)
from xauusd_bot.journal.store import _ALLOWED_TRADE_UPDATES


# ----------------------------------------------------------------- factories


def _ts(hour: int = 13, minute: int = 30, day: int = 15) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=UTC)


def make_trade(**overrides) -> TradeRecord:
    base = dict(
        timestamp_open=_ts(),
        side="long",
        entry_price=Decimal("2370.00"),
        stop_loss=Decimal("2365.00"),
        take_profits=[Decimal("2375.00")],
        volume_lots=Decimal("0.10"),
        risk_amount=Decimal("50"),
        setup_id=uuid4(),
        score=80.0,
        subscores={"h1_zone": 80, "m5_zone": 70, "vwap": 75},
        band=ScoreBand.PREPARE_65_74,
        entry_type=EntryType.SCOUT,
        fill_price=Decimal("2370.00"),
        session="london",
        atr_at_entry=0.35,
        structure_at_entry="up",
    )
    base.update(overrides)
    return TradeRecord(**base)


def make_snapshot(**overrides) -> FeatureSnapshotRecord:
    base = dict(
        timestamp=_ts(),
        bar_time=_ts(),
        has_data=True,
        features={"h1_zone": 80, "vwap": 2372.5},
    )
    base.update(overrides)
    return FeatureSnapshotRecord(**base)


def make_order(trade_id: UUID, **overrides) -> OrderRecord:
    base = dict(
        timestamp=_ts(),
        trade_id=trade_id,
        client_order_id=f"cli-{uuid4().hex[:6]}",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        volume=Decimal("0.10"),
        fill_price=Decimal("2370.00"),
        status=OrderStatusTag.FILLED,
    )
    base.update(overrides)
    return OrderRecord(**base)


def make_discrepancy(**overrides) -> LLMFallbackDiscrepancy:
    base = dict(
        timestamp=_ts(),
        decision_id=uuid4(),
        rule_action=DecisionAction.ENTER_LONG,
        rule_score=80.0,
        rule_band=ScoreBand.PREPARE_65_74,
        rule_block_reasons=[],
        llm_action=DecisionAction.ENTER_LONG,
        llm_score=78.0,
        llm_reasoning="aligned",
        final_action=DecisionAction.ENTER_LONG,
        final_source="rule",
        resolution="agreement",  # type: ignore[arg-type]
    )
    base.update(overrides)
    return LLMFallbackDiscrepancy(**base)


# ----------------------------------------------------------------- InMemoryJournalStore basics


@pytest.mark.asyncio
async def test_in_memory_store_writes_and_reads_trade() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    tid = await store.write_trade(t)
    assert tid == t.id
    fetched = await store.get_trade(tid)
    assert fetched == t


@pytest.mark.asyncio
async def test_in_memory_store_rejects_duplicate_trade() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    await store.write_trade(t)
    with pytest.raises(JournalStoreError, match="already exists"):
        await store.write_trade(t)


@pytest.mark.asyncio
async def test_in_memory_store_rejects_duplicate_snapshot() -> None:
    store = InMemoryJournalStore()
    s = make_snapshot()
    await store.write_feature_snapshot(s)
    with pytest.raises(JournalStoreError, match="already exists"):
        await store.write_feature_snapshot(s)


@pytest.mark.asyncio
async def test_in_memory_store_rejects_duplicate_order() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    await store.write_trade(t)
    o = make_order(trade_id=t.id)
    await store.write_order(o)
    with pytest.raises(JournalStoreError, match="already exists"):
        await store.write_order(o)


@pytest.mark.asyncio
async def test_in_memory_store_rejects_duplicate_discrepancy() -> None:
    store = InMemoryJournalStore()
    d = make_discrepancy()
    await store.write_discrepancy(d)
    with pytest.raises(JournalStoreError, match="already exists"):
        await store.write_discrepancy(d)


@pytest.mark.asyncio
async def test_in_memory_store_writes_and_reads_snapshot() -> None:
    store = InMemoryJournalStore()
    s = make_snapshot()
    sid = await store.write_feature_snapshot(s)
    assert sid == s.id
    fetched = await store.get_snapshot(sid)
    assert fetched == s


@pytest.mark.asyncio
async def test_in_memory_store_writes_and_reads_order() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    await store.write_trade(t)
    o = make_order(trade_id=t.id)
    oid = await store.write_order(o)
    assert oid == o.id
    orders = await store.orders_for_trade(t.id)
    assert orders == [o]


@pytest.mark.asyncio
async def test_in_memory_store_writes_and_reads_discrepancy() -> None:
    store = InMemoryJournalStore()
    d = make_discrepancy()
    did = await store.write_discrepancy(d)
    assert did == d.id
    all_disc = await store.list_discrepancies()
    assert all_disc == [d]


@pytest.mark.asyncio
async def test_list_discrepancies_filters_by_time_window() -> None:
    store = InMemoryJournalStore()
    base = _ts()
    d1 = make_discrepancy(timestamp=base)
    d2 = make_discrepancy(timestamp=base + timedelta(hours=1))
    d3 = make_discrepancy(timestamp=base + timedelta(hours=2))
    for d in (d3, d1, d2):
        await store.write_discrepancy(d)
    out = await store.list_discrepancies(start=base, end=base + timedelta(hours=2))
    # d1, d2 in window; d3 excluded
    assert {d.id for d in out} == {d1.id, d2.id}
    # And the order is sorted ascending
    assert [d.timestamp for d in out] == sorted(d.timestamp for d in out)


# ----------------------------------------------------------------- list / sort / filter


@pytest.mark.asyncio
async def test_list_trades_sorted_ascending_by_timestamp_open() -> None:
    store = InMemoryJournalStore()
    base = _ts()
    # Insert in scrambled order; should come back sorted.
    t3 = make_trade(timestamp_open=base + timedelta(hours=3))
    t1 = make_trade(timestamp_open=base + timedelta(hours=1))
    t2 = make_trade(timestamp_open=base + timedelta(hours=2))
    for t in (t3, t1, t2):
        await store.write_trade(t)
    all_trades = await store.list_trades()
    assert [t.timestamp_open for t in all_trades] == sorted(
        t.timestamp_open for t in (t1, t2, t3)
    )


@pytest.mark.asyncio
async def test_list_trades_filters_by_symbol() -> None:
    store = InMemoryJournalStore()
    a = make_trade(symbol="XAUUSD")
    b = make_trade(symbol="EURUSD")
    await store.write_trade(a)
    await store.write_trade(b)
    only_xau = await store.list_trades(symbol="XAUUSD")
    assert [t.id for t in only_xau] == [a.id]


@pytest.mark.asyncio
async def test_list_trades_filters_by_time_window() -> None:
    store = InMemoryJournalStore()
    base = _ts()
    t1 = make_trade(timestamp_open=base + timedelta(hours=1))
    t2 = make_trade(timestamp_open=base + timedelta(hours=2))
    t3 = make_trade(timestamp_open=base + timedelta(hours=3))
    for t in (t1, t2, t3):
        await store.write_trade(t)
    out = await store.list_trades(start=base + timedelta(hours=2))
    # t1 excluded (before window); t2/t3 included
    assert {t.id for t in out} == {t2.id, t3.id}


@pytest.mark.asyncio
async def test_list_trades_respects_limit() -> None:
    store = InMemoryJournalStore()
    base = _ts()
    for i in range(5):
        await store.write_trade(make_trade(timestamp_open=base + timedelta(hours=i)))
    out = await store.list_trades(limit=3)
    assert len(out) == 3


@pytest.mark.asyncio
async def test_list_trades_filters_by_time_window_excludes_upper_bound() -> None:
    """``list_trades`` uses ``timestamp_open < end`` (half-open interval)."""

    store = InMemoryJournalStore()
    base = _ts()
    t1 = make_trade(timestamp_open=base)
    t2 = make_trade(timestamp_open=base + timedelta(hours=1))
    for t in (t1, t2):
        await store.write_trade(t)
    # End exactly at t2's open → t2 excluded
    out = await store.list_trades(start=base, end=base + timedelta(hours=1))
    assert {t.id for t in out} == {t1.id}


@pytest.mark.asyncio
async def test_list_snapshots_filters_by_window() -> None:
    store = InMemoryJournalStore()
    base = _ts()
    s1 = make_snapshot(timestamp=base, bar_time=base)
    s2 = make_snapshot(timestamp=base + timedelta(hours=1), bar_time=base + timedelta(hours=1))
    s3 = make_snapshot(timestamp=base + timedelta(hours=2), bar_time=base + timedelta(hours=2))
    for s in (s1, s2, s3):
        await store.write_feature_snapshot(s)
    out = await store.list_snapshots(
        start=base + timedelta(hours=1),
        end=base + timedelta(hours=2, minutes=30),
    )
    # Window is [start, end) — s2 is in, s3 is in, s1 is before, s3 ok if < end
    assert {s.id for s in out} == {s2.id, s3.id}


# ----------------------------------------------------------------- update_trade PIT + allowed fields


@pytest.mark.asyncio
async def test_update_trade_allows_close_time_and_pnl() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    await store.write_trade(t)
    await store.update_trade(
        t.id,
        updates={
            "timestamp_close": t.timestamp_open + timedelta(hours=1),
            "exit_price": Decimal("2375.00"),
            "pnl_realized": Decimal("50"),
            "r_multiple": 1.0,
            "exit_reason": ExitReasonTag.TP1_HIT,
        },
    )
    fetched = await store.get_trade(t.id)
    assert fetched is not None
    assert fetched.exit_price == Decimal("2375.00")
    assert fetched.pnl_realized == Decimal("50")
    assert fetched.r_multiple == 1.0
    assert fetched.exit_reason == ExitReasonTag.TP1_HIT


@pytest.mark.asyncio
async def test_update_trade_rejects_disallowed_field() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    await store.write_trade(t)
    with pytest.raises(PITViolationError, match="entry_price"):
        await store.update_trade(t.id, updates={"entry_price": Decimal("9999")})


@pytest.mark.asyncio
async def test_update_trade_rejects_back_dated_close() -> None:
    store = InMemoryJournalStore()
    t = make_trade(timestamp_open=_ts(hour=10))
    await store.write_trade(t)
    with pytest.raises(PITViolationError, match="before timestamp_open"):
        await store.update_trade(
            t.id,
            updates={"timestamp_close": _ts(hour=9)},  # 1 hour before open
        )


@pytest.mark.asyncio
async def test_update_trade_raises_when_missing() -> None:
    store = InMemoryJournalStore()
    with pytest.raises(TradeNotFoundError, match="not found"):
        await store.update_trade(uuid4(), updates={"pnl_realized": Decimal("0")})


@pytest.mark.asyncio
async def test_update_trade_appends_order_ids_and_merges_tags() -> None:
    store = InMemoryJournalStore()
    t = make_trade(tags={"force": "true"})
    await store.write_trade(t)
    await store.update_trade(t.id, updates={"order_ids": ["o-1", "o-2"], "tags": {"close_reason": "tp1"}})
    fetched = await store.get_trade(t.id)
    assert fetched is not None
    assert fetched.order_ids == ["o-1", "o-2"]
    assert fetched.tags == {"force": "true", "close_reason": "tp1"}


@pytest.mark.asyncio
async def test_update_trade_allowed_fields_constant_matches_documented_set() -> None:
    """The frozen allowed-set must not silently grow."""

    assert _ALLOWED_TRADE_UPDATES == frozenset(
        {
            "timestamp_close",
            "exit_price",
            "pnl_realized",
            "pnl_unrealized",
            "r_multiple",
            "exit_reason",
            "order_ids",
            "tags",
        }
    )


# ----------------------------------------------------------------- helpers


@pytest.mark.asyncio
async def test_count_returns_per_table_totals() -> None:
    store = InMemoryJournalStore()
    assert await store.count() == {
        "trades": 0,
        "snapshots": 0,
        "orders": 0,
        "discrepancies": 0,
        "discrepancies_v2": 0,
        "fitting_proposals": 0,
    }
    t = make_trade()
    await store.write_trade(t)
    await store.write_feature_snapshot(make_snapshot())
    await store.write_order(make_order(trade_id=t.id))
    await store.write_discrepancy(make_discrepancy())
    assert await store.count() == {
        "trades": 1,
        "snapshots": 1,
        "orders": 1,
        "discrepancies": 1,
        "discrepancies_v2": 0,
        "fitting_proposals": 0,
    }


@pytest.mark.asyncio
async def test_clear_wipes_everything() -> None:
    store = InMemoryJournalStore()
    t = make_trade()
    await store.write_trade(t)
    await store.write_feature_snapshot(make_snapshot())
    await store.clear()
    assert await store.count() == {
        "trades": 0,
        "snapshots": 0,
        "orders": 0,
        "discrepancies": 0,
        "discrepancies_v2": 0,
        "fitting_proposals": 0,
    }


# ----------------------------------------------------------------- Protocol conformance


def test_in_memory_store_implements_protocol() -> None:
    """Static check: InMemoryJournalStore satisfies the JournalStore Protocol."""

    store: object = InMemoryJournalStore()
    assert isinstance(store, JournalStore)


# ----------------------------------------------------------------- TimescaleJournalStore stub


def test_timescale_store_raises_when_db_unreachable() -> None:
    """Real asyncpg store: a write against an unreachable DB raises (not NotImplemented)."""

    # Port 1 is never a Postgres — connection fails fast.
    store = TimescaleJournalStore(
        dsn="postgresql+asyncpg://x:x@127.0.0.1:1/x", connect_timeout_seconds=1.0
    )
    with pytest.raises(Exception):  # noqa: B017,PT011 - any connection error is fine
        asyncio.run(store.write_trade(make_trade()))


def test_timescale_store_normalises_sqlalchemy_dsn() -> None:
    store = TimescaleJournalStore(dsn="postgresql+asyncpg://u:p@h:5432/db")
    assert store._dsn == "postgresql://u:p@h:5432/db"  # noqa: SLF001


def test_timescale_store_factory_returns_timescale_for_prod() -> None:
    settings = Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        environment="production",
    )
    store = get_journal_store(settings)
    assert isinstance(store, TimescaleJournalStore)


def test_timescale_store_factory_returns_memory_for_test_env() -> None:
    settings = Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        environment="test",
    )
    store = get_journal_store(settings)
    assert isinstance(store, InMemoryJournalStore)


def test_timescale_store_factory_returns_memory_when_dsn_empty() -> None:
    settings = Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="",  # empty
        environment="development",
    )
    store = get_journal_store(settings)
    assert isinstance(store, InMemoryJournalStore)


@pytest.mark.asyncio
async def test_async_factory_falls_back_to_memory_on_stub() -> None:
    """The async factory must fall back to InMemory when the Timescale
    store is still a stub (Block 5a)."""

    settings = Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        environment="production",
    )
    store = await get_journal_store_with_fallback(settings)
    assert isinstance(store, InMemoryJournalStore)


@pytest.mark.asyncio
async def test_async_factory_returns_memory_for_test_env_directly() -> None:
    settings = Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        environment="test",
    )
    store = await get_journal_store_with_fallback(settings)
    assert isinstance(store, InMemoryJournalStore)


# ----------------------------------------------------------------- async safety under contention


@pytest.mark.asyncio
async def test_concurrent_writes_dont_lose_records() -> None:
    store = InMemoryJournalStore()
    n = 50

    async def write_one(i: int) -> None:
        # Hours 0..23 cycle; minute is varied so each trade is unique.
        t = make_trade(timestamp_open=_ts(hour=i % 24, minute=i))
        await store.write_trade(t)

    await asyncio.gather(*(write_one(i) for i in range(n)))
    trades = await store.list_trades()
    assert len(trades) == n
    assert len({t.id for t in trades}) == n


# ----------------------------------------------------------------- V2 discrepancy (Block 6)


def make_discrepancy_v2(**overrides) -> LLMFallbackDiscrepancyV2:
    base = dict(
        timestamp=_ts(),
        decision_id=uuid4(),
        score=70.0,
        llm_raw_response='{"decision": "scout"}',
        fallback_reason="validation_error",
        rule_decision="enter_long",
        llm_decision="scout",
    )
    base.update(overrides)
    return LLMFallbackDiscrepancyV2(**base)


@pytest.mark.asyncio
async def test_v2_write_and_list_round_trip() -> None:
    store = InMemoryJournalStore()
    rec = make_discrepancy_v2()
    returned_id = await store.write_discrepancy_v2(rec)
    assert returned_id == rec.decision_id
    recs = await store.list_discrepancies_v2()
    assert len(recs) == 1
    assert recs[0].decision_id == rec.decision_id
    assert recs[0].fallback_reason == "validation_error"
    assert recs[0].llm_decision == "scout"
    assert recs[0].rule_decision == "enter_long"
    assert recs[0].score == 70.0


@pytest.mark.asyncio
async def test_v2_rejects_invalid_fallback_reason() -> None:
    """The Literal type on fallback_reason rejects unknown values."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LLMFallbackDiscrepancyV2(
            timestamp=_ts(),
            decision_id=uuid4(),
            score=70.0,
            fallback_reason="not_a_valid_reason",  # type: ignore[arg-type]
            rule_decision="no_trade",
        )


@pytest.mark.asyncio
async def test_v2_optional_llm_fields_default_to_none() -> None:
    rec = LLMFallbackDiscrepancyV2(
        timestamp=_ts(),
        decision_id=uuid4(),
        score=50.0,
        fallback_reason="score_below_threshold",
        rule_decision="no_trade",
    )
    assert rec.llm_decision is None
    assert rec.llm_raw_response is None
