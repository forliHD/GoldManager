"""Architecture invariant tests for Block 4 — execution layer.

These tests enforce the hard constraints documented in
``AGENTS.md`` §3 (I-1, I-4). The verifier also runs the same
grep audit, but having it as a test makes the contract explicit
and fails fast in CI if anyone re-introduces a banned import.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


EXECUTION_DIR = Path("src/xauusd_bot/execution")
SCHEMAS_DIR = Path("src/xauusd_bot/common/schemas/execution.py")
CLI_DIR = Path("src/xauusd_bot/cli/execution_smoke.py")


# =============================================================== I-1


def test_execution_module_does_not_import_metatrader5() -> None:
    """I-1: `import MetaTrader5` may only appear in live.py / docker/."""

    for py_file in EXECUTION_DIR.rglob("*.py"):
        text = py_file.read_text()
        assert "import MetaTrader5" not in text, f"I-1 violation in {py_file}"
        assert "from MetaTrader5" not in text, f"I-1 violation in {py_file}"


def test_execution_schemas_do_not_import_metatrader5() -> None:
    text = SCHEMAS_DIR.read_text()
    assert "import MetaTrader5" not in text
    assert "from MetaTrader5" not in text


def test_execution_cli_does_not_import_metatrader5() -> None:
    text = CLI_DIR.read_text()
    assert "import MetaTrader5" not in text
    assert "from MetaTrader5" not in text


def test_all_execution_modules_use_imarketconnector_protocol() -> None:
    """Every order-touching module imports IMarketConnector (or its sub-protocol)."""

    # Modules that should be importing the connector Protocol.
    for py_file in [
        "src/xauusd_bot/execution/orders.py",
        "src/xauusd_bot/execution/pending.py",
        "src/xauusd_bot/execution/emergency.py",
    ]:
        text = Path(py_file).read_text()
        assert "IMarketConnector" in text, f"{py_file} must import IMarketConnector"


# =============================================================== I-4 (execution side)


def test_execution_modules_can_mention_lot_size_sl_tp() -> None:
    """I-4 (inverted for execution): lot size / SL / TP are explicitly allowed."""

    # We don't *ban* these terms in execution/; we just verify the
    # scripts we wrote actually contain them. If the rule changes
    # later, this test becomes the natural pivot point.
    orders_text = Path("src/xauusd_bot/execution/orders.py").read_text()
    sizer_text = Path("src/xauusd_bot/execution/sizer.py").read_text()
    stops_text = Path("src/xauusd_bot/execution/stops.py").read_text()
    tp_text = Path("src/xauusd_bot/execution/take_profit.py").read_text()
    for text, term in [
        (orders_text, "volume"),
        (sizer_text, "volume_lots"),
        (stops_text, "sl_price"),
        (tp_text, "tp1_price"),
    ]:
        assert term in text


# =============================================================== structural


def test_execution_module_layout() -> None:
    """The execution/ directory contains the expected modules."""

    expected = {
        "__init__.py",
        "risk.py",
        "sizer.py",
        "orders.py",
        "pending.py",
        "stops.py",
        "take_profit.py",
        "emergency.py",
    }
    found = {p.name for p in EXECUTION_DIR.iterdir()}
    missing = expected - found
    assert not missing, f"missing modules: {missing}"


def test_execution_public_api_exports() -> None:
    """The ``__init__.py`` exports the documented manager names."""

    from xauusd_bot.execution import __all__ as public

    expected_managers = {
        "RiskManager", "PositionSizer", "OrderManager", "PendingOrderManager",
        "StopManager", "TakeProfitManager", "EmergencyStopManager",
    }
    missing = expected_managers - set(public)
    assert not missing, f"missing public exports: {missing}"


def test_execution_schemas_public_api_exports() -> None:
    """The schemas module exports the documented schema names."""

    from xauusd_bot.common.schemas.execution import __all__ as public

    expected = {
        "RiskVerdict", "SizingResult", "StopsAndTPs", "OrderEnvelope",
        "OrderTag", "TrailingMode", "ExitReason", "EmergencyStopState",
        "PendingSweepResult", "ExecutionLifecycleReport",
    }
    missing = expected - set(public)
    assert not missing, f"missing schema exports: {missing}"


def test_no_module_in_execution_uses_print() -> None:
    """No raw print() calls — structlog is the only logger."""

    for py_file in EXECUTION_DIR.rglob("*.py"):
        text = py_file.read_text()
        # Skip docstrings (rough heuristic: look only for `print(` calls).
        for m in re.finditer(r"print\s*\(", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end]
            # Allow in test code only.
            assert "test" in str(py_file), f"raw print() in {py_file}: {line!r}"


def test_execution_does_not_use_todo_comments() -> None:
    """AGENTS.md says no TODO comments in Block 4 modules."""

    for py_file in EXECUTION_DIR.rglob("*.py"):
        text = py_file.read_text()
        # Search for TODO / FIXME / XXX (case-insensitive).
        assert not re.search(r"#\s*TODO\b", text, flags=re.IGNORECASE), f"TODO in {py_file}"
        assert not re.search(r"#\s*FIXME\b", text, flags=re.IGNORECASE), f"FIXME in {py_file}"
        assert not re.search(r"#\s*XXX\b", text, flags=re.IGNORECASE), f"XXX in {py_file}"
