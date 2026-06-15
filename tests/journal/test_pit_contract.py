"""Point-in-Time contract tests for the journal (Block 5a).

The PIT contract for the journal is:

1. A trade's ``feature_snapshot_id`` must point at a
   :class:`FeatureSnapshotRecord` whose ``bar_time <= trade.timestamp_open``.
2. ``update_trade`` must reject ``timestamp_close < timestamp_open``.
3. ``list_trades`` and ``list_snapshots`` return records in
   monotonically increasing time order.

These tests are adversarial: if any of these contracts is broken,
the BacktestEngine (Block 5b) and the ReviewAgent (Block 5c) will
silently produce wrong numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from xauusd_bot.common.schemas.decision import EntryType, ScoreBand
from xauusd_bot.common.schemas.journal import (
    FeatureSnapshotRecord,
    TradeRecord,
)
from xauusd_bot.journal import InMemoryJournalStore, PITViolationError


# ----------------------------------------------------------------- helpers


def _ts(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=UTC)


def make_trade(*, open_ts: datetime, snapshot_id=None) -> TradeRecord:
    return TradeRecord(
        timestamp_open=open_ts,
        side="long",
        entry_price=Decimal("2370.00"),
        stop_loss=Decimal("2365.00"),
        take_profits=[Decimal("2375.00")],
        volume_lots=Decimal("0.10"),
        risk_amount=Decimal("50"),
        setup_id=uuid4(),
        score=80.0,
        subscores={"h1_zone": 80, "m5_zone": 70},
        band=ScoreBand.PREPARE_65_74,
        entry_type=EntryType.SCOUT,
        fill_price=Decimal("2370.00"),
        session="london",
        atr_at_entry=0.35,
        structure_at_entry="up",
        feature_snapshot_id=snapshot_id,
    )


def make_snapshot(*, bar_time: datetime) -> FeatureSnapshotRecord:
    return FeatureSnapshotRecord(
        timestamp=bar_time,
        bar_time=bar_time,
        has_data=True,
        features={"h1_zone": 80},
    )


# ----------------------------------------------------------------- 1. snapshot must precede trade open


@pytest.mark.asyncio
async def test_trade_can_reference_a_snapshot_with_same_or_earlier_bar_time() -> None:
    """PIT happy path: snapshot.bar_time == trade.timestamp_open is allowed."""

    store = InMemoryJournalStore()
    snap = make_snapshot(bar_time=_ts(10))
    await store.write_feature_snapshot(snap)
    trade = make_trade(open_ts=_ts(10), snapshot_id=snap.id)
    await store.write_trade(trade)

    fetched_snap = await store.get_snapshot(snap.id)
    fetched_trade = await store.get_trade(trade.id)
    assert fetched_snap is not None
    assert fetched_trade is not None
    assert fetched_snap.bar_time <= fetched_trade.timestamp_open


@pytest.mark.asyncio
async def test_trade_can_have_no_snapshot_fk() -> None:
    """A trade without feature_snapshot_id is allowed (PIT-anchor is optional)."""

    store = InMemoryJournalStore()
    trade = make_trade(open_ts=_ts(10), snapshot_id=None)
    await store.write_trade(trade)
    fetched = await store.get_trade(trade.id)
    assert fetched is not None
    assert fetched.feature_snapshot_id is None


@pytest.mark.asyncio
async def test_trade_referencing_dangling_snapshot_id_does_not_crash() -> None:
    """The journal does not enforce FK integrity at write time —
    it persists whatever the upstream stack hands in. The PIT
    audit happens at the read side (e.g. ReviewAgent).

    This is intentional: a stale FK would otherwise block writes
    in a recovery scenario, and the journal's job is *durability*,
    not referential integrity (Postgres handles that in Block 5b).
    """

    store = InMemoryJournalStore()
    fake_id = uuid4()
    trade = make_trade(open_ts=_ts(10), snapshot_id=fake_id)
    await store.write_trade(trade)
    fetched = await store.get_trade(trade.id)
    assert fetched is not None
    assert fetched.feature_snapshot_id == fake_id
    # The snapshot does not exist:
    assert await store.get_snapshot(fake_id) is None


# ----------------------------------------------------------------- 2. update_trade rejects back-dated close


@pytest.mark.asyncio
async def test_update_trade_rejects_close_before_open() -> None:
    store = InMemoryJournalStore()
    t = make_trade(open_ts=_ts(10))
    await store.write_trade(t)
    with pytest.raises(PITViolationError) as exc:
        await store.update_trade(
            t.id,
            updates={"timestamp_close": _ts(9)},  # 1 hour before open
        )
    assert "before timestamp_open" in str(exc.value)


@pytest.mark.asyncio
async def test_update_trade_allows_close_equal_to_open() -> None:
    """Same-second close is technically PIT-OK (the trade opened and
    closed within the same M1 bar — pathological but legal)."""

    store = InMemoryJournalStore()
    t = make_trade(open_ts=_ts(10))
    await store.write_trade(t)
    await store.update_trade(
        t.id,
        updates={"timestamp_close": _ts(10), "exit_price": Decimal("2370.00"), "pnl_realized": Decimal("0")},
    )
    fetched = await store.get_trade(t.id)
    assert fetched is not None
    assert fetched.timestamp_close == _ts(10)


@pytest.mark.asyncio
async def test_update_trade_allows_close_far_after_open() -> None:
    store = InMemoryJournalStore()
    t = make_trade(open_ts=_ts(10))
    await store.write_trade(t)
    later = _ts(10) + timedelta(days=5)
    await store.update_trade(
        t.id,
        updates={
            "timestamp_close": later,
            "exit_price": Decimal("2380.00"),
            "pnl_realized": Decimal("100"),
            "r_multiple": 2.0,
        },
    )
    fetched = await store.get_trade(t.id)
    assert fetched is not None
    assert fetched.timestamp_close == later
    assert fetched.r_multiple == 2.0


# ----------------------------------------------------------------- 3. list_* returns sorted output


@pytest.mark.asyncio
async def test_list_trades_preserves_pit_ordering_under_random_writes() -> None:
    """Adversarial: write trades in random order; list_trades
    must still return them sorted by timestamp_open ascending.
    """

    store = InMemoryJournalStore()
    base = _ts(10)
    trades = [make_trade(open_ts=base + timedelta(hours=h)) for h in (5, 1, 9, 3, 7)]
    for t in trades:
        await store.write_trade(t)
    out = await store.list_trades()
    opens = [t.timestamp_open for t in out]
    assert opens == sorted(opens)
    # And the order is exactly [1, 3, 5, 7, 9] hours after base.
    expected = [base + timedelta(hours=h) for h in (1, 3, 5, 7, 9)]
    assert opens == expected


@pytest.mark.asyncio
async def test_list_snapshots_preserves_pit_ordering_under_random_writes() -> None:
    store = InMemoryJournalStore()
    base = _ts(10)
    snaps = [make_snapshot(bar_time=base + timedelta(hours=h)) for h in (4, 0, 8, 2, 6)]
    for s in snaps:
        await store.write_feature_snapshot(s)
    out = await store.list_snapshots(start=base, end=base + timedelta(hours=10))
    bars = [s.bar_time for s in out]
    assert bars == sorted(bars)


# ----------------------------------------------------------------- 4. read API returns correct close-time data


@pytest.mark.asyncio
async def test_update_trade_round_trip_preserves_pnl_and_r_multiple() -> None:
    """A PIT-correct trade record carries close-time data that
    downstream read APIs (queries.compute_equity_curve) trust.
    """

    store = InMemoryJournalStore()
    t = make_trade(open_ts=_ts(10))
    await store.write_trade(t)
    later = _ts(10) + timedelta(hours=2)
    await store.update_trade(
        t.id,
        updates={
            "timestamp_close": later,
            "exit_price": Decimal("2380.00"),
            "pnl_realized": Decimal("100"),
            "r_multiple": 2.0,
            "exit_reason": "tp2_hit",  # type: ignore[arg-type]
        },
    )
    fetched = await store.get_trade(t.id)
    assert fetched is not None
    # Use the query layer to confirm the data flows into the read API.
    from xauusd_bot.journal.queries import compute_equity_curve, compute_r_distribution

    ec = compute_equity_curve([fetched])
    assert len(ec) == 1
    assert ec[0][0] == later
    assert ec[0][1] == Decimal("100")
    rd = compute_r_distribution([fetched])
    assert rd["2"] == 1
