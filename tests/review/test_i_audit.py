"""I-1 / I-4 architectural-invariant audit — Block 5c review layer.

Per AGENTS.md §3:

* **I-1:** the review layer (like all other modules) must NOT
  import ``MetaTrader5`` anywhere in code.
* **I-4:** the review layer must NOT contain code that computes
  position size, lot size, stop loss, or take profit. The
  FittingProposal is a *hypothesis* — never an execution
  instruction.

This test walks the AST of every module in
:mod:`xauusd_bot.review` and fails on code-level violations.
Docstring / comment mentions are allowed (they document the
*boundary*, not a violation).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REVIEW_PKG = Path(__file__).resolve().parents[2] / "src" / "xauusd_bot" / "review"
SCHEMAS_PKG = Path(__file__).resolve().parents[2] / "src" / "xauusd_bot" / "common" / "schemas" / "review.py"

# Identifiers that would indicate the review layer is reaching into
# "Hands" territory (Block 4) or directly using the connector.
FORBIDDEN_I1: frozenset[str] = frozenset({"MetaTrader5"})

# I-4: forbidden identifiers. Docstring mentions are allowed.
FORBIDDEN_I4: frozenset[str] = frozenset(
    {
        "position_size",
        "lot_size",
        "stop_loss",
        "take_profit",
        "sl_price",
        "tp_price",
        "VolumeInLots",
    }
)


def _iter_python_files():
    for path in sorted(REVIEW_PKG.rglob("*.py")):
        yield path
    yield SCHEMAS_PKG


def _code_identifiers(path: Path) -> set[str]:
    """Return identifiers that appear in *code* (not docstrings/comments)."""

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
            for arg in node.args.kwonlyargs:
                used.add(arg.arg)
        elif isinstance(node, ast.ClassDef):
            used.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                used.add(alias.asname or alias.name.split(".")[0])
            if node.module:
                used.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                used.add(alias.asname or alias.name.split(".")[0])
    return used


def _imports(path: Path) -> list[str]:
    """Return the list of full import names (for I-1 enforcement)."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
                for alias in node.names:
                    imports.append(f"{node.module}.{alias.name}")
    return imports


# ----------------------------------------------------------------- I-1


class TestI1NoMetaTrader5:
    """The review layer must not import MetaTrader5."""

    @pytest.mark.parametrize("path", list(_iter_python_files()), ids=lambda p: p.name)
    def test_no_metatrader5_import(self, path: Path) -> None:
        imports = _imports(path)
        for imp in imports:
            assert "MetaTrader5" not in imp, f"{path.name} imports {imp}"


# ----------------------------------------------------------------- I-4


class TestI4NoHandsIdentifiers:
    """The review layer must not contain code-level references to position_size, lot_size, etc."""

    @pytest.mark.parametrize("path", list(_iter_python_files()), ids=lambda p: p.name)
    def test_no_hands_identifiers_in_code(self, path: Path) -> None:
        used = _code_identifiers(path)
        violations = sorted(FORBIDDEN_I4 & used)
        assert not violations, f"{path.name} contains forbidden identifiers: {violations}"


# ----------------------------------------------------------------- bulk (the CLI-style audit)


def test_i1_grep_across_review_layer_has_no_source_violations() -> None:
    """Bulk I-1 check: walk all .py files in review/ + schemas/review.py
    and ensure NO source-level MetaTrader5 reference (import or attribute access)."""

    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        # Sanity — only flag actual import statements / attribute
        # access (defensive: in case the AST walker misses something).
        # Strip docstrings before the simple check.
        stripped = strip_docstrings(text)
        assert "MetaTrader5" not in stripped, (
            f"{path.name} contains 'MetaTrader5' in non-docstring code"
        )


def _strip_docstrings(src: str) -> str:
    """Strip triple-quoted strings (defensive; AST audit is the primary gate)."""

    out: list[str] = []
    in_triple = False
    triple_char = ""
    i = 0
    while i < len(src):
        c = src[i]
        if not in_triple and c in ('"', "'") and src[i : i + 3] in ('"""', "'''"):
            in_triple = True
            triple_char = src[i : i + 3]
            i += 3
            continue
        if in_triple and src[i : i + 3] == triple_char:
            in_triple = False
            i += 3
            continue
        if not in_triple:
            out.append(c)
        i += 1
    return "".join(out)


def strip_docstrings(src: str) -> str:
    return _strip_docstrings(src)