"""JournalStore — Block 5a persistence backend.

This module provides the **async persistence interface** the Block-5
pipeline (BacktestEngine, Review, FittingProposal) talks to. It is
deliberately backend-agnostic: callers depend on the
:class:`JournalStore` Protocol, not on a concrete TimescaleDB or
in-memory implementation.

Two backends
------------
* :class:`InMemoryJournalStore` — dict-based, thread-safe via
  :class:`asyncio.Lock`. Default for tests and for environments where
  TimescaleDB is not configured (``settings.environment == "test"``).
* :class:`TimescaleJournalStore` — asyncpg-backed, full
  TimescaleDB hypertables. **Stubbed in Block 5a** — see the class
  docstring for the Block 5b integration plan.

The factory :func:`get_journal_store` chooses a backend based on
``Settings.environment`` and ``Settings.timescaledb_url``.

Invariants
----------
* **I-1**: this module does NOT import MetaTrader5. Persistence is
  upstream from the connector; the store talks to a generic async
  SQL driver.
* **I-4**: the store is **write-only by API surface** — there is no
  ``delete_trade`` or ``update_snapshot`` method. Updates are
  append-only: the only update is :meth:`JournalStore.update_trade`
  for adding close-time data (exit_price, pnl, etc.). Feature
  snapshots and orders are *immutable once written* — the journal
  preserves PIT integrity this way.
* **PIT**: every read path returns records sorted by timestamp. The
  InMemory implementation enforces this with a sorted-by-default list
  on read; the Timescale implementation will rely on a hypertable
  ``ORDER BY`` index.
* **AsyncIO-only**: every public method is ``async``. The store
  never does blocking I/O on the caller's event loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.journal import (
    FeatureSnapshotRecord,
    LLMFallbackDiscrepancy,
    LLMFallbackDiscrepancyV2,
    OrderRecord,
    TradeRecord,
)
from xauusd_bot.common.schemas.review import FittingProposal, FittingProposalFilter

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------- protocol


@runtime_checkable
class JournalStore(Protocol):
    """Async persistence interface for the journal.

    The store is **append-only** (with a single update path for
    trade close data). There is no delete, no overwrite of feature
    snapshots, no overwrite of orders. This keeps the PIT audit
    trustworthy: if a record is in the journal, it is exactly as it
    was at write time.
    """

    async def write_trade(self, trade: TradeRecord) -> UUID:
        """Persist a new :class:`TradeRecord`. Returns the assigned id."""

        ...

    async def update_trade(self, trade_id: UUID, updates: dict[str, Any]) -> None:
        """Patch a subset of trade fields (only close-time data is allowed).

        The implementation MUST reject updates to fields other than
        ``timestamp_close``, ``exit_price``, ``pnl_realized``,
        ``pnl_unrealized``, ``r_multiple``, ``exit_reason``,
        ``order_ids`` (append), ``tags`` (merge). Anything else is
        a bug or a PIT violation.
        """

        ...

    async def write_feature_snapshot(self, snapshot: FeatureSnapshotRecord) -> UUID:
        """Persist a :class:`FeatureSnapshotRecord`. Returns the assigned id."""

        ...

    async def write_order(self, order: OrderRecord) -> UUID:
        """Persist an :class:`OrderRecord`. Returns the assigned id."""

        ...

    async def write_discrepancy(self, d: LLMFallbackDiscrepancy) -> UUID:
        """Persist an :class:`LLMFallbackDiscrepancy`. Returns the assigned id."""

        ...

    async def write_discrepancy_v2(self, d: LLMFallbackDiscrepancyV2) -> UUID:
        """Persist an :class:`LLMFallbackDiscrepancyV2` (Block-6 spec-exact)."""

        ...

    async def list_trades(
        self,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[TradeRecord]:
        """Return trades sorted by ``timestamp_open`` (ascending)."""

        ...

    async def get_trade(self, trade_id: UUID) -> TradeRecord | None:
        """Fetch a single trade by id, or None if missing."""

        ...

    async def get_snapshot(self, snapshot_id: UUID) -> FeatureSnapshotRecord | None:
        """Fetch a single feature snapshot by id, or None if missing."""

        ...

    async def list_snapshots(
        self,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
        limit: int = 1000,
        newest_first: bool = False,
    ) -> list[FeatureSnapshotRecord]:
        """Return snapshots in [start, end) sorted by ``bar_time`` (ascending).

        With ``newest_first=True`` the *newest* ``limit`` snapshots in the window
        are kept (still returned ascending); the default keeps the oldest.
        """

        ...

    # ---- Block 5c — FittingProposal CRUD ----

    async def add_fitting_proposal(self, proposal: FittingProposal) -> UUID:
        """Persist a new :class:`FittingProposal`. Returns the assigned id."""

        ...

    async def get_fitting_proposal(self, proposal_id: UUID) -> FittingProposal | None:
        """Fetch a single :class:`FittingProposal` by id, or None if missing."""

        ...

    async def update_fitting_proposal(self, proposal: FittingProposal) -> None:
        """Replace an existing :class:`FittingProposal` in-place.

        Implementations MUST reject status transitions that are not in
        the documented state machine:

        * ``proposed → backtested``
        * ``proposed → approved``
        * ``proposed → rejected``
        * ``backtested → approved``
        * ``backtested → rejected``

        ``approved`` / ``rejected`` are terminal. A no-op (status
        unchanged) is allowed.
        """

        ...

    async def list_fitting_proposals(
        self,
        filter: FittingProposalFilter | None = None,
    ) -> list[FittingProposal]:
        """Return :class:`FittingProposal` records matching ``filter``.

        Sorted by ``created_at`` descending (newest first) so the
        dashboard / CLI see the most recent proposals at the top.
        """

        ...


# ----------------------------------------------------------------- errors


class JournalStoreError(RuntimeError):
    """Base error for the journal store."""


class TradeNotFoundError(JournalStoreError):
    """Raised when an update_trade references a missing id."""


class FittingProposalNotFoundError(JournalStoreError):
    """Raised when an update_fitting_proposal references a missing id."""


class InvalidStatusTransitionError(JournalStoreError):
    """Raised when a fitting-proposal status transition is not in the
    documented state machine.
    """


class PITViolationError(JournalStoreError):
    """Raised when an update would break PIT (e.g. back-dating close)."""


# Documented FittingProposal status transitions. The engine
# rejects everything else (defense-in-depth: the CLI also enforces
# the same rules).
_FITTING_PROPOSAL_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"proposed", "backtested", "approved", "rejected"}),
    "backtested": frozenset({"backtested", "approved", "rejected"}),
    "approved": frozenset({"approved"}),
    "rejected": frozenset({"rejected"}),
}


# ----------------------------------------------------------------- InMemoryJournalStore


# Fields that update_trade is allowed to mutate. Everything else is
# write-once to keep PIT integrity.
_ALLOWED_TRADE_UPDATES: frozenset[str] = frozenset(
    {
        "timestamp_close",
        "exit_price",
        "pnl_realized",
        "pnl_unrealized",
        "r_multiple",
        "exit_reason",
        "order_ids",  # append-only
        "tags",  # shallow merge
    }
)


class InMemoryJournalStore:
    """In-memory async-safe journal store.

    Uses three indexes for O(1) lookups:
    * ``_trades`` (dict) and ``_snapshots`` (dict) by id
    * ``_orders`` (dict) by id
    * ``_discrepancies`` (dict) by id

    A single :class:`asyncio.Lock` protects all writes. Reads acquire
    the lock too — this is conservative but cheap, and keeps the
    snapshot tests deterministic without spinning up a real DB.

    PIT compliance
    --------------
    * ``list_trades`` sorts by ``timestamp_open`` ascending.
    * ``list_snapshots`` sorts by ``bar_time`` ascending.
    * ``update_trade`` rejects ``timestamp_close < timestamp_open``
      (back-dating a close is a PIT violation).
    * ``update_trade`` rejects mutations outside ``_ALLOWED_TRADE_UPDATES``.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._trades: dict[UUID, TradeRecord] = {}
        self._snapshots: dict[UUID, FeatureSnapshotRecord] = {}
        self._orders: dict[UUID, OrderRecord] = {}
        self._discrepancies: dict[UUID, LLMFallbackDiscrepancy] = {}
        # Block-6 spec-exact discrepancy records (LLMFallbackDiscrepancyV2).
        self._discrepancies_v2: dict[UUID, LLMFallbackDiscrepancyV2] = {}
        # Block-5c fitting proposals.
        self._fitting_proposals: dict[UUID, FittingProposal] = {}
        # Index by FK for fast "orders for a trade" lookups (read-side
        # helper, not part of the Protocol). Tests can use it.
        self._orders_by_trade: dict[UUID, list[UUID]] = defaultdict(list)
        # Per-symbol indexes for list_trades(symbol=...) fast path.
        self._trades_by_symbol: dict[str, list[UUID]] = defaultdict(list)

    # ============================================================ writes

    async def write_trade(self, trade: TradeRecord) -> UUID:
        async with self._lock:
            if trade.id in self._trades:
                raise JournalStoreError(f"trade {trade.id} already exists")
            self._trades[trade.id] = trade
            self._trades_by_symbol[trade.symbol].append(trade.id)
            return trade.id

    async def update_trade(self, trade_id: UUID, updates: dict[str, Any]) -> None:
        async with self._lock:
            existing = self._trades.get(trade_id)
            if existing is None:
                raise TradeNotFoundError(f"trade {trade_id} not found")
            # 1. Reject unknown / write-once fields.
            for key in updates:
                if key not in _ALLOWED_TRADE_UPDATES:
                    raise PITViolationError(
                        f"update_trade does not allow mutating {key!r}; "
                        f"allowed fields: {sorted(_ALLOWED_TRADE_UPDATES)}"
                    )
            # 2. Reject back-dating of close.
            ts_close = updates.get("timestamp_close")
            if ts_close is not None and ts_close < existing.timestamp_open:
                raise PITViolationError(
                    f"timestamp_close {ts_close} is before timestamp_open {existing.timestamp_open}"
                )
            # 3. Apply special merges (order_ids append, tags merge).
            apply_data: dict[str, Any] = dict(updates)
            if "order_ids" in updates:
                merged = list(existing.order_ids) + list(updates["order_ids"])
                apply_data["order_ids"] = merged
            if "tags" in updates:
                merged = {**existing.tags, **updates["tags"]}
                apply_data["tags"] = merged
            # 4. Validate via Pydantic (catches e.g. r_multiple = "abc").
            new_data = existing.model_dump()
            new_data.update(apply_data)
            patched = TradeRecord.model_validate(new_data)
            self._trades[trade_id] = patched

    async def write_feature_snapshot(self, snapshot: FeatureSnapshotRecord) -> UUID:
        async with self._lock:
            if snapshot.id in self._snapshots:
                raise JournalStoreError(f"snapshot {snapshot.id} already exists")
            self._snapshots[snapshot.id] = snapshot
            return snapshot.id

    async def write_order(self, order: OrderRecord) -> UUID:
        async with self._lock:
            if order.id in self._orders:
                raise JournalStoreError(f"order {order.id} already exists")
            self._orders[order.id] = order
            self._orders_by_trade[order.trade_id].append(order.id)
            return order.id

    async def write_discrepancy(self, d: LLMFallbackDiscrepancy) -> UUID:
        async with self._lock:
            if d.id in self._discrepancies:
                raise JournalStoreError(f"discrepancy {d.id} already exists")
            self._discrepancies[d.id] = d
            return d.id

    async def write_discrepancy_v2(self, d: LLMFallbackDiscrepancyV2) -> UUID:
        async with self._lock:
            new_id = d.decision_id
            if new_id in self._discrepancies_v2:
                raise JournalStoreError(f"discrepancy_v2 {new_id} already exists")
            self._discrepancies_v2[new_id] = d
            return new_id

    # ============================================================ FittingProposal CRUD

    async def add_fitting_proposal(self, proposal: FittingProposal) -> UUID:
        async with self._lock:
            if proposal.id in self._fitting_proposals:
                raise JournalStoreError(f"fitting_proposal {proposal.id} already exists")
            self._fitting_proposals[proposal.id] = proposal
            return proposal.id

    async def get_fitting_proposal(self, proposal_id: UUID) -> FittingProposal | None:
        async with self._lock:
            return self._fitting_proposals.get(proposal_id)

    async def update_fitting_proposal(self, proposal: FittingProposal) -> None:
        async with self._lock:
            existing = self._fitting_proposals.get(proposal.id)
            if existing is None:
                raise FittingProposalNotFoundError(
                    f"fitting_proposal {proposal.id} not found"
                )
            allowed = _FITTING_PROPOSAL_VALID_TRANSITIONS[existing.status]
            if proposal.status not in allowed:
                raise InvalidStatusTransitionError(
                    f"illegal fitting_proposal status transition: "
                    f"{existing.status!r} → {proposal.status!r}; "
                    f"allowed from {existing.status!r}: {sorted(allowed)}"
                )
            self._fitting_proposals[proposal.id] = proposal

    # ============================================================ reads

    async def list_trades(
        self,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[TradeRecord]:
        async with self._lock:
            if symbol is not None:
                candidates = [self._trades[tid] for tid in self._trades_by_symbol.get(symbol, [])]
            else:
                candidates = list(self._trades.values())
            out: list[TradeRecord] = []
            for t in candidates:
                if start is not None and t.timestamp_open < start:
                    continue
                if end is not None and t.timestamp_open >= end:
                    continue
                out.append(t)
            out.sort(key=lambda t: t.timestamp_open)
            return out[:limit]

    async def get_trade(self, trade_id: UUID) -> TradeRecord | None:
        async with self._lock:
            return self._trades.get(trade_id)

    async def get_snapshot(self, snapshot_id: UUID) -> FeatureSnapshotRecord | None:
        async with self._lock:
            return self._snapshots.get(snapshot_id)

    async def list_snapshots(
        self,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
        limit: int = 1000,
        newest_first: bool = False,
    ) -> list[FeatureSnapshotRecord]:
        async with self._lock:
            candidates = [
                s
                for s in self._snapshots.values()
                if start <= s.bar_time < end and (symbol is None or s.symbol == symbol)
            ]
            candidates.sort(key=lambda s: s.bar_time)
            # newest_first selects the newest ``limit`` (still returned ascending);
            # the default keeps the oldest ``limit`` for PIT-ordered analytics.
            return candidates[-limit:] if newest_first else candidates[:limit]

    async def list_fitting_proposals(
        self,
        filter: FittingProposalFilter | None = None,
    ) -> list[FittingProposal]:
        async with self._lock:
            items = list(self._fitting_proposals.values())
            if filter is not None:
                if filter.status:
                    items = [p for p in items if p.status in filter.status]
                if filter.category:
                    items = [p for p in items if p.category in filter.category]
                if filter.overfitting_risk:
                    items = [
                        p for p in items if p.overfitting_risk in filter.overfitting_risk
                    ]
                if filter.min_period is not None:
                    items = [
                        p
                        for p in items
                        if p.period_start.date() >= filter.min_period
                    ]
                if filter.max_period is not None:
                    items = [
                        p
                        for p in items
                        if p.period_start.date() <= filter.max_period
                    ]
            # Newest first.
            items.sort(key=lambda p: p.created_at, reverse=True)
            return items

    # ============================================================ helpers (non-Protocol)

    async def orders_for_trade(self, trade_id: UUID) -> list[OrderRecord]:
        """Return all orders attached to a trade, in insertion order."""

        async with self._lock:
            return [self._orders[oid] for oid in self._orders_by_trade.get(trade_id, [])]

    async def list_discrepancies(
        self, start: datetime | None = None, end: datetime | None = None, limit: int = 1000
    ) -> list[LLMFallbackDiscrepancy]:
        async with self._lock:
            out: list[LLMFallbackDiscrepancy] = []
            for d in self._discrepancies.values():
                if start is not None and d.timestamp < start:
                    continue
                if end is not None and d.timestamp >= end:
                    continue
                out.append(d)
            out.sort(key=lambda d: d.timestamp)
            return out[:limit]

    async def list_discrepancies_v2(
        self, start: datetime | None = None, end: datetime | None = None, limit: int = 1000
    ) -> list[LLMFallbackDiscrepancyV2]:
        async with self._lock:
            out: list[LLMFallbackDiscrepancyV2] = []
            for d in self._discrepancies_v2.values():
                if start is not None and d.timestamp < start:
                    continue
                if end is not None and d.timestamp >= end:
                    continue
                out.append(d)
            out.sort(key=lambda d: d.timestamp)
            return out[:limit]

    async def count(self) -> dict[str, int]:
        """Diagnostic — return counts of all stored records."""

        async with self._lock:
            return {
                "trades": len(self._trades),
                "snapshots": len(self._snapshots),
                "orders": len(self._orders),
                "discrepancies": len(self._discrepancies),
                "discrepancies_v2": len(self._discrepancies_v2),
                "fitting_proposals": len(self._fitting_proposals),
            }

    async def clear(self) -> None:
        """Test helper — wipe everything."""

        async with self._lock:
            self._trades.clear()
            self._snapshots.clear()
            self._orders.clear()
            self._discrepancies.clear()
            self._discrepancies_v2.clear()
            self._fitting_proposals.clear()
            self._orders_by_trade.clear()
            self._trades_by_symbol.clear()


# ----------------------------------------------------------------- TimescaleJournalStore

# JSONB-per-record schema: a thin indexed column set for filtering plus the
# full Pydantic model in ``data`` (round-trips losslessly). Plain tables, not
# hypertables — adequate for the journal's volume; hypertable conversion is a
# later optimisation. ``IF NOT EXISTS`` makes init idempotent.
_TIMESCALE_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_trades (
  id TEXT PRIMARY KEY, timestamp_open TIMESTAMPTZ NOT NULL,
  timestamp_close TIMESTAMPTZ, symbol TEXT NOT NULL, data JSONB NOT NULL);
CREATE INDEX IF NOT EXISTS ix_trades_open ON journal_trades (timestamp_open);
CREATE INDEX IF NOT EXISTS ix_trades_symbol ON journal_trades (symbol);
CREATE TABLE IF NOT EXISTS journal_orders (
  id TEXT PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, symbol TEXT NOT NULL,
  status TEXT, data JSONB NOT NULL);
CREATE INDEX IF NOT EXISTS ix_orders_ts ON journal_orders (ts);
CREATE TABLE IF NOT EXISTS journal_snapshots (
  id TEXT PRIMARY KEY, bar_time TIMESTAMPTZ NOT NULL, written_at TIMESTAMPTZ NOT NULL,
  symbol TEXT NOT NULL, data JSONB NOT NULL);
CREATE INDEX IF NOT EXISTS ix_snap_bartime ON journal_snapshots (bar_time);
CREATE TABLE IF NOT EXISTS journal_discrepancies (
  id TEXT PRIMARY KEY, ts TIMESTAMPTZ, version INT NOT NULL, data JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS journal_fitting_proposals (
  id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL,
  category TEXT, data JSONB NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fp_created ON journal_fitting_proposals (created_at);
"""


class TimescaleJournalStore:
    """Async TimescaleDB/Postgres-backed journal store (asyncpg).

    Same Protocol as :class:`InMemoryJournalStore`; records are stored as
    JSONB (with a few indexed columns) so the Pydantic models round-trip
    losslessly. The connection pool + schema are created lazily on first
    use; :func:`get_journal_store_with_fallback` degrades to in-memory if
    the DB is unreachable.

    Hypertables (planned schema)
    -----------------------------
    * ``trades`` (id uuid PK, ts_open timestamptz, ts_close timestamptz NULL,
      symbol text, side text, entry_price numeric, ...). Hypertable on
      ``ts_open``.
    * ``feature_snapshots`` (id uuid PK, bar_time timestamptz, ts_written
      timestamptz, symbol text, timeframe text, has_data bool, source_version
      text, features jsonb). Hypertable on ``bar_time``.
    * ``orders`` (id uuid PK, ts timestamptz, trade_id uuid FK, ...).
      Index on ``trade_id`` and ``ts``.
    * ``discrepancies`` (id uuid PK, ts timestamptz, decision_id uuid, ...).
      Index on ``decision_id`` and ``ts``.

    Connection-failure policy
    -------------------------
    If the connection pool fails to initialize, the factory will
    log a warning and return an :class:`InMemoryJournalStore` as
    fallback. This is the safer behaviour for production (we want
    the bot to *trade on paper* rather than refuse to start) but
    surfaces clearly in the logs.

    Why a stub
    ----------
    * asyncpg is not yet a runtime dep (Block 5a scope).
    * The Block-5b integration will live behind a feature flag.
    * All Block-5b/5c unit tests must use :class:`InMemoryJournalStore`
      anyway (CI has no TimescaleDB).
    """

    def __init__(self, dsn: str, *, connect_timeout_seconds: float = 5.0) -> None:
        # Accept SQLAlchemy-style DSNs (postgresql+asyncpg://…) and normalise
        # to plain postgresql:// for asyncpg.
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgres+asyncpg://", "postgresql://"
        )
        self._connect_timeout = connect_timeout_seconds
        self._pool: Any = None  # asyncpg.Pool | None
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is not None:
                return self._pool
            import asyncpg  # lazy — only imported when a Timescale store is actually used

            pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=5, timeout=self._connect_timeout
            )
            async with pool.acquire() as con:
                await con.execute(_TIMESCALE_SCHEMA)
            self._pool = pool
            log.info("timescale_store_ready")
            return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @staticmethod
    def _dump(model: Any) -> str:
        return json.dumps(model.model_dump(mode="json"), default=str)

    # ---------------------------------------------------------------- writes

    async def write_trade(self, trade: TradeRecord) -> UUID:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO journal_trades (id, timestamp_open, timestamp_close, symbol, data) "
                "VALUES ($1,$2,$3,$4,$5::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, timestamp_close=EXCLUDED.timestamp_close",
                str(trade.id), trade.timestamp_open, trade.timestamp_close, trade.symbol, self._dump(trade),
            )
        return trade.id

    async def update_trade(self, trade_id: UUID, updates: dict[str, Any]) -> None:
        bad = set(updates) - _ALLOWED_TRADE_UPDATES
        if bad:
            raise PITViolationError(f"update_trade rejected non-close fields: {sorted(bad)}")
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow("SELECT data FROM journal_trades WHERE id=$1", str(trade_id))
            if row is None:
                raise TradeNotFoundError(str(trade_id))
            rec = TradeRecord.model_validate(json.loads(row["data"]))
            patch = dict(updates)
            if "tags" in patch and rec.tags:
                patch["tags"] = {**rec.tags, **(patch["tags"] or {})}
            if "order_ids" in patch:
                existing = list(rec.order_ids or [])
                patch["order_ids"] = existing + [o for o in patch["order_ids"] if o not in existing]
            merged = rec.model_copy(update=patch)
            await con.execute(
                "UPDATE journal_trades SET data=$2::jsonb, timestamp_close=$3 WHERE id=$1",
                str(trade_id), self._dump(merged), merged.timestamp_close,
            )

    async def write_feature_snapshot(self, snapshot: FeatureSnapshotRecord) -> UUID:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO journal_snapshots (id, bar_time, written_at, symbol, data) "
                "VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (id) DO NOTHING",
                str(snapshot.id), snapshot.bar_time, snapshot.timestamp, snapshot.symbol, self._dump(snapshot),
            )
        return snapshot.id

    async def write_order(self, order: OrderRecord) -> UUID:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO journal_orders (id, ts, symbol, status, data) "
                "VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (id) DO NOTHING",
                str(order.id), order.timestamp, order.symbol,
                getattr(order.status, "value", str(order.status)), self._dump(order),
            )
        return order.id

    async def _write_discrepancy(self, d: Any, version: int) -> UUID:
        pool = await self._ensure_pool()
        did = getattr(d, "id", None) or uuid4()
        ts = getattr(d, "timestamp", None) or getattr(d, "ts", None)
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO journal_discrepancies (id, ts, version, data) "
                "VALUES ($1,$2,$3,$4::jsonb) ON CONFLICT (id) DO NOTHING",
                str(did), ts, version, self._dump(d),
            )
        return did if isinstance(did, UUID) else uuid4()

    async def write_discrepancy(self, d: LLMFallbackDiscrepancy) -> UUID:
        return await self._write_discrepancy(d, 1)

    async def write_discrepancy_v2(self, d: LLMFallbackDiscrepancyV2) -> UUID:
        return await self._write_discrepancy(d, 2)

    # ---------------------------------------------------------------- reads

    async def list_trades(
        self,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[TradeRecord]:
        clauses, args = [], []
        if symbol is not None:
            args.append(symbol); clauses.append(f"symbol=${len(args)}")
        if start is not None:
            args.append(start); clauses.append(f"timestamp_open>=${len(args)}")
        if end is not None:
            args.append(end); clauses.append(f"timestamp_open<${len(args)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        q = f"SELECT data FROM journal_trades{where} ORDER BY timestamp_open ASC LIMIT ${len(args)}"
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(q, *args)
        return [TradeRecord.model_validate(json.loads(r["data"])) for r in rows]

    async def get_trade(self, trade_id: UUID) -> TradeRecord | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow("SELECT data FROM journal_trades WHERE id=$1", str(trade_id))
        return TradeRecord.model_validate(json.loads(row["data"])) if row else None

    async def get_snapshot(self, snapshot_id: UUID) -> FeatureSnapshotRecord | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow("SELECT data FROM journal_snapshots WHERE id=$1", str(snapshot_id))
        return FeatureSnapshotRecord.model_validate(json.loads(row["data"])) if row else None

    async def list_snapshots(
        self,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
        limit: int = 1000,
        newest_first: bool = False,
    ) -> list[FeatureSnapshotRecord]:
        args = [start, end]
        where = "bar_time>=$1 AND bar_time<$2"
        if symbol is not None:
            args.append(symbol); where += f" AND symbol=${len(args)}"
        args.append(limit)
        if newest_first:
            # The newest ``limit`` rows in the window, returned ascending. Without
            # this an ``ASC LIMIT n`` over a wide window returns the OLDEST n —
            # stale data once the window holds more than ``limit`` snapshots.
            q = (
                f"SELECT data FROM (SELECT data, bar_time FROM journal_snapshots "
                f"WHERE {where} ORDER BY bar_time DESC LIMIT ${len(args)}) t "
                f"ORDER BY bar_time ASC"
            )
        else:
            q = f"SELECT data FROM journal_snapshots WHERE {where} ORDER BY bar_time ASC LIMIT ${len(args)}"
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(q, *args)
        return [FeatureSnapshotRecord.model_validate(json.loads(r["data"])) for r in rows]

    async def count(self) -> dict[str, int]:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            return {
                "trades": await con.fetchval("SELECT count(*) FROM journal_trades"),
                "snapshots": await con.fetchval("SELECT count(*) FROM journal_snapshots"),
                "orders": await con.fetchval("SELECT count(*) FROM journal_orders"),
                "discrepancies": await con.fetchval("SELECT count(*) FROM journal_discrepancies"),
                "fitting_proposals": await con.fetchval("SELECT count(*) FROM journal_fitting_proposals"),
            }

    # ---------------------------------------------------------------- fitting proposals

    async def add_fitting_proposal(self, proposal: FittingProposal) -> UUID:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO journal_fitting_proposals (id, created_at, status, category, data) "
                "VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT (id) DO NOTHING",
                str(proposal.id), proposal.created_at,
                getattr(proposal.status, "value", str(proposal.status)),
                getattr(proposal.category, "value", str(proposal.category)), self._dump(proposal),
            )
        return proposal.id

    async def get_fitting_proposal(self, proposal_id: UUID) -> FittingProposal | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT data FROM journal_fitting_proposals WHERE id=$1", str(proposal_id)
            )
        return FittingProposal.model_validate(json.loads(row["data"])) if row else None

    async def update_fitting_proposal(self, proposal: FittingProposal) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT data FROM journal_fitting_proposals WHERE id=$1", str(proposal.id)
            )
            if row is None:
                raise FittingProposalNotFoundError(str(proposal.id))
            existing = FittingProposal.model_validate(json.loads(row["data"]))
            allowed = _FITTING_PROPOSAL_VALID_TRANSITIONS[existing.status]
            if proposal.status not in allowed:
                raise InvalidStatusTransitionError(
                    f"{existing.status} → {proposal.status} is not a valid transition"
                )
            await con.execute(
                "UPDATE journal_fitting_proposals SET status=$2, category=$3, data=$4::jsonb WHERE id=$1",
                str(proposal.id), getattr(proposal.status, "value", str(proposal.status)),
                getattr(proposal.category, "value", str(proposal.category)), self._dump(proposal),
            )

    async def list_fitting_proposals(
        self,
        filter: FittingProposalFilter | None = None,
    ) -> list[FittingProposal]:
        clauses, args = [], []
        if filter and filter.status:
            args.append([getattr(s, "value", str(s)) for s in filter.status])
            clauses.append(f"status = ANY(${len(args)})")
        if filter and filter.category:
            args.append([getattr(c, "value", str(c)) for c in filter.category])
            clauses.append(f"category = ANY(${len(args)})")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        q = f"SELECT data FROM journal_fitting_proposals{where} ORDER BY created_at DESC"
        pool = await self._ensure_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(q, *args)
        return [FittingProposal.model_validate(json.loads(r["data"])) for r in rows]


# ----------------------------------------------------------------- factory


def get_journal_store(settings: Settings) -> JournalStore:
    """Pick a backend based on :class:`Settings`.

    Decision tree
    -------------
    * ``settings.environment == "test"`` → :class:`InMemoryJournalStore`
      (default; test isolation guarantees no real DB).
    * ``settings.environment in ("development", "production")`` →
      :class:`TimescaleJournalStore` if a DSN is set, else
      :class:`InMemoryJournalStore` with a warning log.
    * If the Timescale DSN is present but connection fails on first
      use (handled inside the class in Block 5b), the calling code
      may catch :class:`JournalStoreError` and fall back — see
      :func:`get_journal_store_with_fallback` below.

    Why a factory at all
    --------------------
    * Block 5a tests must run on CI without a TimescaleDB.
    * The Block-5b/5c read API is identical across backends.
    * A real environment can swap the backend by setting
      ``ENVIRONMENT=test`` in CI and ``ENVIRONMENT=production`` in prod.
    """

    env = settings.environment
    dsn = settings.timescaledb_url
    if env == "test" or not dsn:
        return InMemoryJournalStore()
    return TimescaleJournalStore(dsn=dsn)


async def get_journal_store_with_fallback(settings: Settings) -> JournalStore:
    """Async factory with a runtime fallback to in-memory.

    On a real env, attempt to construct the Timescale store and
    verify connectivity with a ``SELECT 1`` round-trip. On failure,
    log a warning and return :class:`InMemoryJournalStore` so the
    bot can keep running (paper-trading only — see AGENTS.md §4c.1).

    In Block 5a, the connectivity probe is best-effort: if the
    Timescale store is configured but not yet implemented (the
    stub), the probe raises ``NotImplementedError`` and we fall
    back. In Block 5b, the probe will use the real asyncpg pool.
    """

    env = settings.environment
    dsn = settings.timescaledb_url
    if env == "test" or not dsn:
        return InMemoryJournalStore()
    candidate = TimescaleJournalStore(dsn=dsn)
    try:
        # Block 5a: this raises NotImplementedError. Block 5b will
        # actually run a ``SELECT 1`` here.
        await candidate._ensure_pool()  # noqa: SLF001
    except NotImplementedError:
        log.warning(
            "timescale_store_not_implemented_falling_back_to_memory",
            env=env,
        )
        return InMemoryJournalStore()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "timescale_store_unavailable_falling_back_to_memory",
            env=env,
            error=str(exc),
        )
        return InMemoryJournalStore()
    return candidate


# ----------------------------------------------------------------- re-exports

__all__ = [
    "FittingProposalNotFoundError",
    "InMemoryJournalStore",
    "InvalidStatusTransitionError",
    "JournalStore",
    "JournalStoreError",
    "PITViolationError",
    "TimescaleJournalStore",
    "TradeNotFoundError",
    "get_journal_store",
    "get_journal_store_with_fallback",
]


# ``logging`` is imported above for symmetry with the rest of the
# codebase even though structlog is the primary logger.
_ = logging  # silence unused-import warning on linters
