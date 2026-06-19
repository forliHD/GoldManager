"""I-4 architectural-invariant audit: Brain vs Hands.

Per AGENTS.md §3 I-4, the decision layer (Block 3) is the
"Brain" — it never computes position size, lot size, stop loss,
or take profit. Those are Block 4 (Execution). This test walks
the AST of every module in :mod:`xauusd_bot.decision` and fails
if any of the forbidden identifiers appears as a
variable/function-arg/assignment (NOT in docstrings or comments).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


DECISION_PKG = Path(__file__).resolve().parents[2] / "src" / "xauusd_bot" / "decision"
# Identifiers that would indicate the decision layer is reaching
# into "Hands" territory (Block 4). Docstring/comment mentions
# are allowed; code uses are not.
FORBIDDEN_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "position_size",
        "lot_size",
        "stop_loss",
        "take_profit",
        "VolumeInLots",
        "sl_price",
        "tp_price",
    }
)


def _iter_python_files(root: Path):
    yield from sorted(root.rglob("*.py"))


def _all_identifiers_in_code(path: Path) -> set[str]:
    """Return the set of identifier names that appear in *code* (not docstrings)."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.arg):
            used.add(node.arg)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    used.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            used.add(node.target.id)
        elif isinstance(node, ast.FunctionDef):
            used.add(node.name)
            for arg in node.args.args:
                used.add(arg.arg)
            for arg in node.args.kwonlyargs:
                used.add(arg.arg)
        elif isinstance(node, ast.AsyncFunctionDef):
            used.add(node.name)
            for arg in node.args.args:
                used.add(arg.arg)
        elif isinstance(node, ast.ClassDef):
            used.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                used.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                used.add(alias.asname or alias.name.split(".")[0])
    return used


class TestI4Audit:
    """Static AST audit — the decision layer must not mention
    position size / SL / TP / lot size in code."""

    @pytest.mark.parametrize(
        "path",
        [pytest.param(p, id=p.name) for p in _iter_python_files(DECISION_PKG)],
    )
    def test_no_forbidden_identifiers(self, path: Path) -> None:
        names = _all_identifiers_in_code(path)
        violations = names & FORBIDDEN_IDENTIFIERS
        assert not violations, (
            f"{path.name} uses forbidden identifiers (I-4 violation): "
            f"{sorted(violations)}. The decision layer must not compute "
            "position_size, lot_size, stop_loss, take_profit, sl_price, "
            "tp_price — those are Block 4 (Execution). Docstring "
            "mentions are allowed; code uses are not."
        )
