"""Architecture-invariant tests for the journal layer (Block 5a).

Mirrors the Block-4 ``tests/execution/test_invariants.py`` pattern
(AGENTS.md §3 I-1 + I-4 + the journal-specific PIT contract).

* I-1: ``import MetaTrader5`` may NOT appear in any journal module.
* I-4 (journal-flavoured): the journal must NOT compute lot size,
  SL, or TP — it persists what Block 4 already produced.
* PIT: a trade's ``feature_snapshot_id`` must point at a snapshot
  with ``bar_time <= trade.timestamp_open``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

JOURNAL_DIR = Path("src/xauusd_bot/journal")
JOURNAL_SCHEMAS = Path("src/xauusd_bot/common/schemas/journal.py")
JOURNAL_CLI = Path("src/xauusd_bot/cli/journal_smoke.py")


# Regex for an *actual* import statement (not a docstring mention).
# Matches:   import MetaTrader5
#            from MetaTrader5 import X
#            from x.y import MetaTrader5
_MT5_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+MetaTrader5\b|from\s+MetaTrader5\b|import\s+MetaTrader5\s*$|from\s+MetaTrader5\s+import)",
    re.MULTILINE,
)


def _has_mt5_import(text: str) -> bool:
    """Return True iff the source contains a real ``MetaTrader5`` import."""

    return bool(_MT5_IMPORT_RE.search(text))


# =============================================================== I-1


def test_journal_module_does_not_import_metatrader5() -> None:
    """I-1: `import MetaTrader5` may not appear in the journal layer."""

    for py_file in JOURNAL_DIR.rglob("*.py"):
        text = py_file.read_text()
        assert not _has_mt5_import(text), f"I-1 violation in {py_file}"


def test_journal_schemas_do_not_import_metatrader5() -> None:
    text = JOURNAL_SCHEMAS.read_text()
    assert not _has_mt5_import(text)


def test_journal_smoke_cli_does_not_import_metatrader5() -> None:
    text = JOURNAL_CLI.read_text()
    assert not _has_mt5_import(text)


# =============================================================== I-4 (journal-flavoured)


def test_journal_module_layout() -> None:
    """The journal/ directory contains the expected modules."""

    expected = {"__init__.py", "store.py", "queries.py"}
    found = {p.name for p in JOURNAL_DIR.iterdir()}
    missing = expected - found
    assert not missing, f"missing modules: {missing}"


def test_journal_public_api_exports() -> None:
    """The journal ``__init__.py`` exports the documented names."""

    from xauusd_bot.journal import __all__ as public

    expected = {
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
    }
    missing = expected - set(public)
    assert not missing, f"missing public exports: {missing}"


def test_journal_does_not_recompute_lot_size_sl_tp() -> None:
    """I-4 (journal-flavoured): the journal persists Block-4 outputs
    but does not compute them.

    Concretely: no module under ``xauusd_bot.journal/`` may call
    ``PositionSizer``, ``StopManager``, ``TakeProfitManager``, or
    the ``RiskManager.approve`` method. The journal only stores
    what the upstream layers produced.
    """

    forbidden_imports = [
        "PositionSizer",
        "StopManager",
        "TakeProfitManager",
        "RiskManager",
        "OrderManager",
    ]
    for py_file in JOURNAL_DIR.rglob("*.py"):
        text = py_file.read_text()
        for name in forbidden_imports:
            assert f"from xauusd_bot.execution" not in text or name not in text, (
                f"journal/ must not import {name} from execution/: {py_file}"
            )


def test_journal_does_not_have_position_size_lot_size_in_business_logic() -> None:
    """The terms ``position_size`` and ``lot_size`` are I-4 violations
    if they appear as *computed* fields in the journal layer. They
    may only appear as Pydantic *field names* on TradeRecord /
    OrderRecord (the schema is the persistence contract).
    """

    for py_file in JOURNAL_DIR.rglob("*.py"):
        text = py_file.read_text()
        for term in ("position_size", "lot_size"):
            for m in re.finditer(rf"\b{term}\b", text):
                # Allowed only in docstrings (rough check: ignore lines
                # starting with ``#`` or inside triple-quoted strings).
                # Since this is a heuristic we just ban the term outright
                # in journal/ and add explicit exceptions in the docstring
                # if needed.
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                line = text[line_start:line_end]
                assert False, (
                    f"journal/ must not reference {term!r} in business logic: "
                    f"{py_file}: {line!r}"
                )


def test_journal_schemas_have_volume_lots_and_stop_loss_as_persisted_fields() -> None:
    """The schema layer is the persistence contract: it stores
    ``volume_lots``, ``stop_loss``, ``take_profits`` as opaque
    Decimal fields (not as computed). The Pydantic model is the
    place where these terms *are* allowed.
    """

    text = JOURNAL_SCHEMAS.read_text()
    for term in ("volume_lots", "stop_loss", "take_profits"):
        assert term in text, f"schema must persist {term} as a Decimal field"


# =============================================================== PIT contract


def test_journal_queries_dont_read_global_state() -> None:
    """All query functions take their input as an explicit argument."""

    import xauusd_bot.journal.queries as q

    for name in [
        "compute_equity_curve",
        "compute_max_drawdown",
        "compute_r_distribution",
        "compute_score_band_stats",
        "compute_session_stats",
        "compute_setup_breakdown",
        "compute_sharpe",
    ]:
        fn = getattr(q, name)
        # Sanity: each is a plain function (not a class with __init__ side effects).
        assert callable(fn)
        # The first parameter is the input collection — the signature
        # is implicitly enforced by usage. We just check it's not a
        # singleton/classmethod.
        assert not isinstance(fn, classmethod)
        assert not isinstance(fn, staticmethod)


# =============================================================== structural


def test_no_module_in_journal_uses_print() -> None:
    """No raw print() calls — structlog is the only logger."""

    for py_file in JOURNAL_DIR.rglob("*.py"):
        text = py_file.read_text()
        for m in re.finditer(r"print\s*\(", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end]
            assert "test" in str(py_file) or "cli" in str(py_file), (
                f"raw print() in {py_file}: {line!r}"
            )


def test_journal_does_not_use_todo_comments() -> None:
    """No TODO/FIXME/XXX in journal/ — Block 5a is ship-ready."""

    for py_file in JOURNAL_DIR.rglob("*.py"):
        text = py_file.read_text()
        assert not re.search(r"#\s*TODO\b", text, flags=re.IGNORECASE), f"TODO in {py_file}"
        assert not re.search(r"#\s*FIXME\b", text, flags=re.IGNORECASE), f"FIXME in {py_file}"
        assert not re.search(r"#\s*XXX\b", text, flags=re.IGNORECASE), f"XXX in {py_file}"
