"""Architecture invariant audits for the dashboard (Block 9).

Coverage
--------
* **I-1**: no `MetaTrader5` import in dashboard/.
* **I-4**: no `position_size` / `lot_size` / `stop_loss` / `take_profit` /
  `sl_price` / `tp_price` / `VolumeInLots` as CODE statements in
  dashboard/api.py. The dashboard is read-only — these tokens must not
  appear as executable code (docstring mentions are fine).
* **PII**: auth endpoints never log the plaintext password.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "src" / "xauusd_bot" / "dashboard"
API_PY = DASHBOARD_DIR / "api.py"

# Code-statement tokens forbidden by I-4. Mentions in docstrings /
# comments are fine; only AST Name / Attribute usage counts.
I_4_TOKENS: tuple[str, ...] = (
    "position_size",
    "lot_size",
    "stop_loss",
    "take_profit",
    "sl_price",
    "tp_price",
    "VolumeInLots",
)


# ----------------------------------------------------------------- I-1


class TestI1NoMetaTrader5Import:
    def test_no_metatrader5_import_in_dashboard(self) -> None:
        """I-1 audit: no MetaTrader5 import anywhere in dashboard/."""

        violations: list[tuple[str, int]] = []
        for py in DASHBOARD_DIR.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                # Match: import MetaTrader5 / from MetaTrader5 ...
                if re.match(r"^(import|from)\s+MetaTrader5\b", stripped):
                    violations.append((str(py.relative_to(DASHBOARD_DIR)), lineno))
        assert not violations, (
            f"I-1 violation — MetaTrader5 imported in: {violations}"
        )


# ----------------------------------------------------------------- I-4


class TestI4NoPositionSizeCodeStatements:
    def test_no_position_size_code_in_api(self) -> None:
        """I-4 audit: position_size/lot_size/etc. are NOT used as code in api.py.

        Reading ``trade_record.stop_loss`` (a Pydantic field on the
        TradeRecord schema) is fine — that is data plumbing, not the
        decision layer computing SL. The audit flags these forbidden
        patterns:

        * Standalone variable assignment to one of the tokens.
        * Function calls named exactly one of the tokens.
        * Local definitions (def position_size(...)).

        Attribute access ``X.<token>`` is allowed when ``X`` is a
        TradeRecord/Bar/Tick/AccountInfo (i.e. data plumbing). The
        test deliberately focuses on the LLM-decision-layer anti-pattern.
        """

        if not API_PY.is_file():
            pytest.skip(f"api.py not found at {API_PY}")

        source = API_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations: list[tuple[str, int, str]] = []

        for node in ast.walk(tree):
            # Function definition named one of the tokens.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in I_4_TOKENS:
                    violations.append((API_PY.name, node.lineno, f"def {node.name}"))
            # Function call: position_size(), lot_size(), ...
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in I_4_TOKENS
            ):
                violations.append((API_PY.name, node.lineno, f"call {node.func.id}()"))
            # Standalone assignment: position_size = ... (Assign target).
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in I_4_TOKENS:
                        violations.append(
                            (API_PY.name, target.lineno, f"assign {target.id}")
                        )

        assert not violations, (
            f"I-4 violation — I-4 tokens used as code in api.py: {violations}"
        )


# ----------------------------------------------------------------- PII


class TestPIINoPasswordInLogs:
    def test_auth_api_source_does_not_log_password(self) -> None:
        """PII: the auth endpoints' source MUST NOT log the plaintext password.

        We use a simple heuristic: the `password` parameter to
        ``/api/auth/login`` must never appear in a ``log.*`` or
        ``print(*`` call. AST walk catches direct uses; string-literal
        mentions in docstrings/comments are fine.
        """

        from xauusd_bot.dashboard import api

        source = API_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations: list[tuple[int, str]] = []

        # Functions whose job is to consume the password (auth-internal
        # — they hash + check, they never log).
        AUTH_INTERNAL: frozenset[str] = frozenset(
            {
                "verify_password",
                "_verify_password",
                "_hash_password",
                "create_session",
                "checkpw",
                "hashpw",
            }
        )

        for node in ast.walk(tree):
            # Catch any function call where the password variable is an
            # argument: e.g. log.info("...", password=...).
            if isinstance(node, ast.Call):
                # Identify the called function name.
                func = node.func
                func_name: str | None = None
                if isinstance(func, ast.Name):
                    func_name = func.id
                elif isinstance(func, ast.Attribute):
                    func_name = func.attr
                if func_name in AUTH_INTERNAL:
                    continue
                # node.args is a list of positional args (not on the
                # function — it's on the Call itself).
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id == "password":
                        violations.append((node.lineno, "password passed to function"))
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Name) and kw.value.id == "password":
                        violations.append((node.lineno, f"password in kwarg {kw.arg!r}"))

        assert not violations, (
            f"PII violation — password parameter leaked into a function call: {violations}"
        )
