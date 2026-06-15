"""Journal — Block 5a (Trade + Feature Snapshot persistence).

Public API
----------
* :class:`xauusd_bot.common.schemas.journal.TradeRecord` — persisted trade
* :class:`xauusd_bot.common.schemas.journal.FeatureSnapshotRecord` — persisted feature snapshot
* :class:`xauusd_bot.common.schemas.journal.OrderRecord` — persisted order
* :class:`xauusd_bot.common.schemas.journal.LLMFallbackDiscrepancy` — rule↔LLM disagreement
* :class:`xauusd_bot.journal.store.JournalStore` — async persistence Protocol
* :class:`xauusd_bot.journal.store.InMemoryJournalStore` — default backend (tests, dev)
* :class:`xauusd_bot.journal.store.TimescaleJournalStore` — production backend (Block-5b deliverable)
* :func:`xauusd_bot.journal.store.get_journal_store` — backend selector
* :mod:`xauusd_bot.journal.queries` — pure-function aggregations (Block-5a)
"""

from xauusd_bot.journal.queries import (
    compute_equity_curve,
    compute_max_drawdown,
    compute_r_distribution,
    compute_score_band_stats,
    compute_session_stats,
    compute_setup_breakdown,
    compute_sharpe,
)
from xauusd_bot.journal.store import (
    InMemoryJournalStore,
    JournalStore,
    JournalStoreError,
    PITViolationError,
    TimescaleJournalStore,
    TradeNotFoundError,
    get_journal_store,
    get_journal_store_with_fallback,
)

__all__ = [
    "InMemoryJournalStore",
    "JournalStore",
    "JournalStoreError",
    "PITViolationError",
    "TimescaleJournalStore",
    "TradeNotFoundError",
    "compute_equity_curve",
    "compute_max_drawdown",
    "compute_r_distribution",
    "compute_score_band_stats",
    "compute_session_stats",
    "compute_setup_breakdown",
    "compute_sharpe",
    "get_journal_store",
    "get_journal_store_with_fallback",
]
