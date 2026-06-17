"""Static sanity check for ``mql5/BotOverlay.mq5``.

This is NOT an MQL5 compiler. It catches only obvious code smells:

  1. Brace balance — every ``{`` must be closed by ``}``, and vice versa.
     (Comments and string literals are excluded via single-pass scan.)
  2. Whitelisted MQL5 stdlib functions only — see ``ALLOWED_FUNCS``.
     Unknown identifiers that look like function calls trigger a
     warning (not an error, so user-defined helpers still pass).
  3. No Python-style imports — MQL5 is its own language; a stray
     ``import MetaTrader5`` would be a copy-paste accident.
  4. No execution-risk string literals — ``position_size``,
     ``lot_size``, ``VolumeInLots`` would indicate the indicator is
     doing more than drawing (I-4 invariant: the indicator REPRESENTS,
     it does not DECIDE).

Exit code 0 on success, 1 on any failure. Designed to run from CI
(``python -m tools.check_mql5_syntax``) and as a pre-commit hook.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Default path — relative to the repo root.
DEFAULT_PATH = Path("mql5/BotOverlay.mq5")

# MQL5 stdlib object / file / chart functions we use. Anything outside
# this list triggers a non-fatal warning (best-effort).
ALLOWED_FUNCS = {
    # Object drawing
    "ObjectCreate",
    "ObjectDelete",
    "ObjectSetInteger",
    "ObjectSetDouble",
    "ObjectSetString",
    "ObjectGet",
    "ObjectFind",
    "ObjectName",
    "ObjectsTotal",
    "OBJPROP_COLOR",
    "OBJPROP_STYLE",
    "OBJPROP_WIDTH",
    "OBJPROP_BACK",
    "OBJPROP_SELECTABLE",
    "OBJPROP_FILL",
    "OBJPROP_CORNER",
    "OBJPROP_PRICE",
    "OBJPROP_TEXT",
    "OBJPROP_FONT",
    "OBJPROP_FONTSIZE",
    "OBJ_HLINE",
    "OBJ_RECTANGLE",
    "OBJ_LABEL",
    "STYLE_SOLID",
    "STYLE_DOT",
    "CORNER_LEFT_UPPER",
    # File I/O
    "FileOpen",
    "FileReadString",
    "FileClose",
    "FileIsEnding",
    "FILE_READ",
    "FILE_TXT",
    "FILE_ANSI",
    "INVALID_HANDLE",
    "CP_UTF8",
    # Chart / event
    "ChartID",
    "EventSetTimer",
    "EventKillTimer",
    "TimeCurrent",
    "CHARTEVENT_CHART_CHANGE",
    # OnInit return value
    "INIT_SUCCEEDED",
    # String utilities
    "StringConcatenate",
    "StringFormat",
    "StringFind",
    "StringSubstr",
    "StringLen",
    "StringGetCharacter",
    "StringToDouble",
    "DoubleToString",
    "IntegerToString",
    "ShortToString",
    "Print",
    # Colors
    "clrDodgerBlue",
    "clrOrange",
    "clrMagenta",
    "clrGray",
    "clrWhite",
    "clrYellow",
    "clrGreen",
    "clrRed",
    "clrBlack",
}

# I-4 / safety strings that should NEVER appear in a chart-only indicator.
EXECUTION_RISK_STRINGS = (
    "position_size",
    "lot_size",
    "VolumeInLots",
    "stop_loss",
    "take_profit",
    "sl_price",
    "tp_price",
)


def strip_strings_and_comments(text: str) -> str:
    """Replace string literals and comment runs with whitespace.

    Keeps line breaks so line numbers in error messages still align
    with the original source.
    """

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Block comment /* ... */ — across lines.
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j < 0:
                # Unterminated comment — treat rest of file as comment.
                out.extend(" " * (n - i))
                break
            for k in range(i, j + 2):
                out.append(text[k] if text[k] == "\n" else " ")
            i = j + 2
            continue
        # Line comment // ...
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            end = j if j >= 0 else n
            out.extend(" " * (end - i))
            i = end
            continue
        # String literal "..."
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            end = min(j + 1, n)
            for k in range(i, end):
                out.append(" " if text[k] != "\n" else "\n")
            i = end
            continue
        # Char literal 'x' (MQL5 — e.g. '}', '\\n'). Must also be
        # stripped, otherwise `'}'` confuses the brace counter.
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            end = min(j + 1, n)
            for k in range(i, end):
                out.append(" " if text[k] != "\n" else "\n")
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def check_brace_balance(stripped: str) -> list[str]:
    """Track depth in a single pass; report imbalance location.

    A simple opens-vs-closes count would miss that comments can
    contain braces. By scanning the post-strip text and tracking the
    running depth (with the minimum reached), we catch both:
      - leftover opens at EOF (depth != 0)
      - stray closes that have no matching open (depth < 0 mid-file)
    """

    depth = 0
    min_depth = 0
    min_line = 0
    cur_line = 1
    for ch in stripped:
        if ch == "\n":
            cur_line += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < min_depth:
                min_depth = depth
                min_line = cur_line
    errors: list[str] = []
    if depth != 0:
        errors.append(
            f"brace imbalance: depth={depth} at EOF (extra "
            f"{'open' if depth > 0 else 'close'} brace)"
        )
    if min_depth < 0:
        errors.append(
            f"closing brace at line {min_line} has no matching open brace"
        )
    return errors


def check_no_python_imports(text: str) -> list[str]:
    """Forbid Python-style imports in the MQL5 file."""

    errors: list[str] = []
    for line_num, line in enumerate(text.splitlines(), 1):
        if re.search(r"\bimport\s+MetaTrader5\b", line):
            errors.append(f"line {line_num}: Python `import MetaTrader5` not allowed in MQL5")
        if re.search(r"^\s*from\s+MetaTrader5\b", line):
            errors.append(f"line {line_num}: Python `from MetaTrader5` not allowed in MQL5")
    return errors


def check_no_execution_risk(text: str) -> list[str]:
    """Forbid I-4 risk strings (indicator should only draw, not trade)."""

    errors: list[str] = []
    for line_num, line in enumerate(text.splitlines(), 1):
        # Strip comments before checking (string already stripped).
        stripped = re.sub(r"//.*$", "", line)
        for risk in EXECUTION_RISK_STRINGS:
            if risk in stripped:
                errors.append(
                    f"line {line_num}: execution-risk string '{risk}' "
                    f"(indicator should only draw, I-4)"
                )
    return errors


# Identifier regex — MQL5 functions look like Foo, FooBar, _Foo, etc.
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def check_unknown_function_calls(stripped: str) -> list[str]:
    """Warn (don't error) on function calls outside the whitelist.

    User-defined helpers (DrawOverlay, JNum, ...) intentionally are
    not in the whitelist — they're written by us. We only WARN for
    unknown functions so this stays a best-effort sanity check, not
    a hard gate.
    """

    warnings: list[str] = []
    for line_num, line in enumerate(stripped.splitlines(), 1):
        # Skip preprocessor directives.
        if line.lstrip().startswith("#"):
            continue
        for match in _IDENT_RE.finditer(line):
            name = match.group(1)
            if name in ALLOWED_FUNCS:
                continue
            if name[0].isupper() or name.startswith("_"):
                warnings.append(
                    f"line {line_num}: function '{name}' not in whitelist"
                )
    return warnings


def check_file(path: Path) -> tuple[int, list[str], list[str]]:
    """Run all checks against ``path``. Returns (exit_code, errors, warnings)."""

    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return 1, [f"file not found: {path}"], []

    text = path.read_text(encoding="utf-8")
    stripped = strip_strings_and_comments(text)

    errors.extend(check_brace_balance(stripped))
    errors.extend(check_no_python_imports(text))
    errors.extend(check_no_execution_risk(text))
    warnings.extend(check_unknown_function_calls(stripped))

    return (1 if errors else 0), errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_PATH,
        help=f"Path to MQL5 source file (default: {DEFAULT_PATH})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (exit 1 on any warning).",
    )
    args = parser.parse_args(argv)

    exit_code, errors, warnings = check_file(args.path)

    if errors:
        print(f"FAIL: {args.path}", file=sys.stderr)
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
    else:
        print(f"OK: {args.path}")

    if warnings:
        for w in warnings:
            print(f"  WARN: {w}", file=sys.stderr)
        if args.strict:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())