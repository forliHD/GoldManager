"""Architecture-invariant tests for the backtest layer (Block 5b).

Mirrors the Block-4 ``tests/execution/test_invariants.py`` pattern
(AGENTS.md §3 I-1 + I-4 + the backtest-specific PIT contract).

* I-1: ``import MetaTrader5`` may NOT appear in any backtest module
  or the smoke CLI. The backtest reuses the :class:`ReplayConnector`
  exclusively.
* I-3 (PIT): the backtest engine never reads a bar past
  ``end_date``; the :class:`ReplayConnector` enforces this.
* I-4 (backtest-flavoured): the backtest is a *pure orchestrator*
  that calls into the existing Block 2 / 3 / 4 modules without
  re-implementing their logic. No backtest-only branches in
  ``features/`` / ``decision/`` / ``execution/``.
* Determinism: there is no ``random`` / ``numpy.random`` /
  ``datetime.now()`` call in the backtest hot path that depends on
  real wall-clock.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKTEST_DIR = Path("src/xauusd_bot/backtest")
BACKTEST_SCHEMAS = Path("src/xauusd_bot/common/schemas/backtest.py")
BACKTEST_CLI = Path("src/xauusd_bot/cli/backtest_smoke.py")

# Modules the backtest must NOT modify (I-4 spirit).
READONLY_MODULES = Path("src/xauusd_bot/features")
READONLY_MODULES_2 = Path("src/xauusd_bot/decision")
READONLY_MODULES_3 = Path("src/xauusd_bot/execution")


# Regex for an *actual* import statement (not a docstring mention).
# Matches:   import MetaTrader5
#            from MetaTrader5 import X
_MT5_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+MetaTrader5\b|from\s+MetaTrader5\b|import\s+MetaTrader5\s*$|from\s+MetaTrader5\s+import)",
    re.MULTILINE,
)


def _has_mt5_import(text: str) -> bool:
    """Return True iff the source contains a real ``MetaTrader5`` import."""

    return bool(_MT5_IMPORT_RE.search(text))


# =============================================================== I-1


def test_backtest_module_does_not_import_metatrader5() -> None:
    """I-1: `import MetaTrader5` may not appear in the backtest layer."""

    for py_file in BACKTEST_DIR.rglob("*.py"):
        text = py_file.read_text()
        assert not _has_mt5_import(text), f"I-1 violation in {py_file}"


def test_backtest_schemas_do_not_import_metatrader5() -> None:
    text = BACKTEST_SCHEMAS.read_text()
    assert not _has_mt5_import(text)


def test_backtest_smoke_cli_does_not_import_metatrader5() -> None:
    text = BACKTEST_CLI.read_text()
    assert not _has_mt5_import(text)


# =============================================================== structural


def test_backtest_module_layout() -> None:
    """The backtest/ directory contains the expected modules."""

    expected = {"__init__.py", "engine.py", "models.py", "walkforward.py"}
    found = {p.name for p in BACKTEST_DIR.iterdir()}
    missing = expected - found
    assert not missing, f"missing modules: {missing}"


def test_backtest_public_api_exports() -> None:
    """The backtest ``__init__.py`` exports the documented names."""

    from xauusd_bot.backtest import __all__ as public

    expected = {
        "BacktestEngine",
        "WalkForwardEngine",
        "SlippageModel",
        "SpreadModel",
        "FixedSlippage",
        "FixedSpread",
        "VolatilitySlippage",
        "VolatilitySpread",
        "ChainedSlippage",
        "NewsAwareSpread",
        "BacktestResult",
        "BacktestStats",
        "BreakdownEntry",
        "WalkForwardResult",
        "WalkForwardWindow",
    }
    missing = expected - set(public)
    assert not missing, f"missing public exports: {missing}"


def test_backtest_uses_replay_connector_not_live() -> None:
    """The backtest engine imports the ReplayConnector, never the Live one."""

    text = (BACKTEST_DIR / "engine.py").read_text()
    assert "ReplayConnector" in text
    assert "LiveMT5Connector" not in text


# =============================================================== I-4 (backtest-flavoured)


def test_backtest_does_not_contain_backtest_only_feature_logic() -> None:
    """The backtest is an orchestrator, not a feature re-implementation.

    No ``if backtest:`` / ``in_backtest`` branch should appear in
    Block 2 (features) or Block 3 (decision). Block 4 (execution)
    is also read-only from the backtest's perspective.
    """

    suspicious = re.compile(
        r"if\s+backtest\b|in_backtest\b|is_backtest\b",
        re.IGNORECASE,
    )
    for d in (READONLY_MODULES, READONLY_MODULES_2, READONLY_MODULES_3):
        for py_file in d.rglob("*.py"):
            text = py_file.read_text()
            assert not suspicious.search(text), (
                f"backtest-only branch in {py_file} — orchestrator should call "
                f"into the module, not fork its logic"
            )


def test_backtest_engine_does_not_redefine_decision_or_execution() -> None:
    """The engine must call into the existing classes, not redefine them."""

    text = (BACKTEST_DIR / "engine.py").read_text()
    # Should call into the real classes, not re-implement them.
    assert "from xauusd_bot.decision" in text
    assert "from xauusd_bot.execution" in text
    # No ad-hoc subclass of decision / execution.
    assert "class FeatureAggregator" not in text
    assert "class RiskManager" not in text
    assert "class ScoringEngine" not in text
    assert "class PositionSizer" not in text


def test_backtest_uses_imarketconnector_protocol() -> None:
    """The engine module uses the IMarketConnector protocol path."""

    text = (BACKTEST_DIR / "engine.py").read_text()
    assert "IMarketConnector" in text or "ReplayConnector" in text


# =============================================================== determinism


def test_backtest_does_not_use_random_module() -> None:
    """The backtest engine must not use ``random`` (no RNG = deterministic)."""

    text = (BACKTEST_DIR / "engine.py").read_text()
    assert "import random" not in text
    assert "from random" not in text


def test_backtest_does_not_use_numpy_random() -> None:
    text = (BACKTEST_DIR / "engine.py").read_text()
    assert "np.random" not in text
    assert "numpy.random" not in text


# =============================================================== log hygiene


def test_no_module_in_backtest_uses_print() -> None:
    """No raw print() calls — structlog is the only logger."""

    for py_file in BACKTEST_DIR.rglob("*.py"):
        text = py_file.read_text()
        for m in re.finditer(r"print\s*\(", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end]
            assert "test" in str(py_file), f"raw print() in {py_file}: {line!r}"


def test_backtest_does_not_use_todo_comments() -> None:
    """No TODO / FIXME / XXX in backtest/ — Block 5b is ship-ready."""

    for py_file in BACKTEST_DIR.rglob("*.py"):
        text = py_file.read_text()
        assert not re.search(r"#\s*TODO\b", text, flags=re.IGNORECASE), f"TODO in {py_file}"
        assert not re.search(r"#\s*FIXME\b", text, flags=re.IGNORECASE), f"FIXME in {py_file}"
        assert not re.search(r"#\s*XXX\b", text, flags=re.IGNORECASE), f"XXX in {py_file}"
