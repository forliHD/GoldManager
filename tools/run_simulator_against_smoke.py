"""Run the BotOverlay Python simulator against the live feature_smoke output.

This is the bridge that proves the MQL5 indicator's data-processing
logic agrees with what the Python feature engine actually produces.

Usage (from repo root)::

    python -m tools.run_simulator_against_smoke

Behavior:
  1. Runs ``xauusd_bot.cli.feature_smoke`` (default 5000 M1 bars).
     This writes ``data/overlay/overlay_levels.json``.
  2. Loads that file through
     :func:`xauusd_bot.viz.bot_overlay_simulator.simulate_mql5_read`.
  3. Prints a summary (counts + first N draw ops + warnings/errors).
  4. Exits 0 if the draw-op count is > 0, otherwise exits 2.

A real MQL5 chart test (visual) is still required — this only proves
the *data* flow from Python -> JSON -> DrawOp list is intact.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Default paths mirror xauusd_bot.cli.feature_smoke defaults.
DEFAULT_OVERLAY = Path("data/overlay/overlay_levels.json")
DEFAULT_SAMPLE = Path("data/sample/xauusd_m1_sample.parquet")
DEFAULT_N_BARS = 5000


def _run_feature_smoke(overlay_path: Path, sample_path: Path, n_bars: int) -> int:
    """Invoke the existing feature_smoke CLI as a subprocess. Returns its exit code."""

    cmd = [
        sys.executable,
        "-m",
        "xauusd_bot.cli.feature_smoke",
        "--n-bars",
        str(n_bars),
        "--overlay",
        str(overlay_path),
        "--sample",
        str(sample_path),
    ]
    print(f"==> running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--n-bars", type=int, default=DEFAULT_N_BARS)
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Don't re-run feature_smoke; just simulate the existing overlay file.",
    )
    parser.add_argument(
        "--max-display", type=int, default=5, help="Number of first ops to print"
    )
    args = parser.parse_args(argv)

    # Step 1: ensure the overlay file exists (and is fresh).
    if not args.skip_smoke:
        rc = _run_feature_smoke(args.overlay, args.sample, args.n_bars)
        if rc != 0:
            print(f"FAIL: feature_smoke exited with {rc}", file=sys.stderr)
            return rc

    if not args.overlay.exists():
        print(f"FAIL: overlay file missing at {args.overlay}", file=sys.stderr)
        return 2

    # Step 2: simulate the MQL5 read.
    # Import lazily so we get a clear error if the package is not installed.
    from xauusd_bot.viz.bot_overlay_simulator import (
        plan_to_dict,
        simulate_mql5_read,
    )

    plan = simulate_mql5_read(args.overlay)
    summary = plan_to_dict(plan)

    # Step 3: print summary.
    print("==> simulator summary:")
    print(f"    ts            = {plan.ts}")
    print(f"    n_ops         = {summary['n_ops']}")
    print(f"    n_hlines      = {summary['n_hlines']}")
    print(f"    n_rects       = {summary['n_rects']}")
    print(f"    n_labels      = {summary['n_labels']}")
    print(f"    n_warnings    = {len(plan.warnings)}")
    print(f"    n_errors      = {len(plan.errors)}")
    print(f"    first {args.max_display} ops:")
    for op in plan.ops[: args.max_display]:
        kind = op.kind
        if kind == "hline":
            print(
                f"      HLINE  {op.name:<22}  price={op.price:.2f}  color={op.color:<10}  style={op.style}"
            )
        elif kind == "rect":
            print(
                f"      RECT   {op.name:<22}  top={op.top:.2f}  bot={op.bottom:.2f}  color={op.color:<10}"
            )
        elif kind == "label":
            print(
                f"      LABEL  {op.name:<22}  price={op.price:.2f}  text={op.text!r}"
            )
    if plan.warnings:
        print(f"==> warnings ({len(plan.warnings)}):")
        for w in plan.warnings[:10]:
            print(f"      - {w}")
        if len(plan.warnings) > 10:
            print(f"      ... ({len(plan.warnings) - 10} more)")
    if plan.errors:
        print(f"==> errors ({len(plan.errors)}):", file=sys.stderr)
        for e in plan.errors:
            print(f"      - {e}", file=sys.stderr)

    # Step 4: success gate.
    if len(plan.ops) == 0:
        print("FAIL: zero draw ops — overlay file empty or schema mismatch", file=sys.stderr)
        return 2
    print(f"OK: simulator produced {len(plan.ops)} draw ops from {args.overlay}")
    return 0


if __name__ == "__main__":
    sys.exit(main())