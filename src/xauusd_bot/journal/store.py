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
    ) -> list[FeatureSnapshotRecord]:
        """Return snapshots in [start, end) sorted by ``bar_time`` (ascending)."""

        ...


# ----------------------------------------------------------------- errors


class JournalStoreError(RuntimeError):
    """Base error for the journal store."""


class TradeNotFoundError(JournalStoreError):
    """Raised when an update_trade references a missing id."""


class PITViolationError(JournalStoreError):
    """Raised when an update would break PIT (e.g. back-dating close)."""


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
    ) -> list[FeatureSnapshotRecord]:
        async with self._lock:
            candidates = [
                s
                for s in self._snapshots.values()
                if start <= s.bar_time < end and (symbol is None or s.symbol == symbol)
            ]
            candidates.sort(key=lambda s: s.bar_time)
            return candidates[:limit]

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
            }

    async def clear(self) -> None:
        """Test helper — wipe everything."""

        async with self._lock:
            self._trades.clear()
            self._snapshots.clear()
            self._orders.clear()
            self._discrepancies.clear()
            self._discrepancies_v2.clear()
            self._orders_by_trade.clear()
            self._trades_by_symbol.clear()


# ----------------------------------------------------------------- TimescaleJournalStore (stub)


class TimescaleJournalStore:
    """Async TimescaleDB-backed journal store. **STUB in Block 5a.**

    Block 5b (BacktestEngine) will implement this class against a
    real TimescaleDB instance. The interface MUST stay identical to
    :class:`InMemoryJournalStore` so the factory and the Block-5
    call-sites do not need to change.

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
        self._dsn = dsn
        self._connect_timeout = connect_timeout_seconds
        self._pool: Any = None  # asyncpg.Pool | None (typed at runtime in Block 5b)
        self._lock = asyncio.Lock()

    async def _ensure_pool(self) -> Any:  # pragma: no cover - stub
        raise NotImplementedError(
            "TimescaleJournalStore is a Block-5a stub. "
            "Block 5b (BacktestEngine) will implement asyncpg.Pool bootstrap here."
        )

    # All Protocol methods raise NotImplementedError. They are
    # listed verbatim so a static type-checker can verify the shape
    # stays in lock-step with the Protocol.

    async def write_trade(self, trade: TradeRecord) -> UUID:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.write_trade is a Block-5b deliverable.")

    async def update_trade(self, trade_id: UUID, updates: dict[str, Any]) -> None:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.update_trade is a Block-5b deliverable.")

    async def write_feature_snapshot(self, snapshot: FeatureSnapshotRecord) -> UUID:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.write_feature_snapshot is a Block-5b deliverable.")

    async def write_order(self, order: OrderRecord) -> UUID:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.write_order is a Block-5b deliverable.")

    async def write_discrepancy(self, d: LLMFallbackDiscrepancy) -> UUID:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.write_discrepancy is a Block-5b deliverable.")

    async def write_discrepancy_v2(self, d: LLMFallbackDiscrepancyV2) -> UUID:  # pragma: no cover - stub
        raise NotImplementedError(
            "TimescaleJournalStore.write_discrepancy_v2 is a Block-5b deliverable."
        )

    async def list_trades(  # pragma: no cover - stub
        self,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[TradeRecord]:
        raise NotImplementedError("TimescaleJournalStore.list_trades is a Block-5b deliverable.")

    async def get_trade(self, trade_id: UUID) -> TradeRecord | None:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.get_trade is a Block-5b deliverable.")

    async def get_snapshot(self, snapshot_id: UUID) -> FeatureSnapshotRecord | None:  # pragma: no cover - stub
        raise NotImplementedError("TimescaleJournalStore.get_snapshot is a Block-5b deliverable.")

    async def list_snapshots(  # pragma: no cover - stub
        self,
        start: datetime,
        end: datetime,
        symbol: str | None = None,
        limit: int = 1000,
    ) -> list[FeatureSnapshotRecord]:
        raise NotImplementedError("TimescaleJournalStore.list_snapshots is a Block-5b deliverable.")


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
    "InMemoryJournalStore",
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
