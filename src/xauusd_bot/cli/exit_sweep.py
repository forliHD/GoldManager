"""Exit-param sweep — replay a recorded tape over a grid of exit configs.

Record a tape once during an LLM backtest::

    python -m xauusd_bot.cli.backtest_smoke --llm ... --record-tape tape.json

Then sweep exit params OFFLINE (no LLM, seconds, no VM load)::

    python -m xauusd_bot.cli.exit_sweep --tape tape.json

Prints the grid ranked by total R. The grid is the cross-product of a few
sensible BE-trigger / chandelier / TP1-fraction values; edit ``_GRID`` to taste.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from decimal import Decimal
from pathlib import Path

_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

from xauusd_bot.backtest.exit_replay import ExitConfig, replay_tape  # noqa: E402
from xauusd_bot.connectors.schemas import SymbolSpec  # noqa: E402

# XAUUSD spec (point/contract/volume step) — the swept exits are spec-relative.
_SPEC = SymbolSpec(
    symbol="XAUUSD", point=Decimal("0.01"), digits=2, trade_contract_size=Decimal("100"),
    volume_min=Decimal("0.01"), volume_max=Decimal("100"), volume_step=Decimal("0.01"),
)

# Sweep axes. Cross-product → one ExitConfig per cell.
_GRID = {
    "be_trigger_r": [0.0, 0.5, 1.0],          # 0 = no BE floor (old behaviour)
    "chandelier_atr": [0.0, 2.0, 3.0, 5.0],   # 0 = structure-trail only
    "tp1_pct": [30.0, 50.0, 70.0, 100.0],     # 100 = full close at TP1 (no runner)
}


def _configs():
    keys = list(_GRID)
    for combo in itertools.product(*(_GRID[k] for k in keys)):
        kw = dict(zip(keys, combo, strict=True))
        tp1 = kw["tp1_pct"]
        rest = 100.0 - tp1
        # Split the remainder TP2/TP3 evenly (TP3 is the runner).
        kw["tp2_pct"] = round(rest / 2, 2)
        kw["tp3_pct"] = round(rest - rest / 2, 2)
        kw["label"] = f"be{kw['be_trigger_r']}_ch{kw['chandelier_atr']}_tp1{int(tp1)}"
        yield ExitConfig(**kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline exit-param sweep over a recorded tape.")
    ap.add_argument("--tape", type=Path, required=True, help="Tape JSON from backtest_smoke --record-tape.")
    ap.add_argument("--top", type=int, default=15, help="How many top configs to print.")
    args = ap.parse_args(argv)

    tape = json.loads(args.tape.read_text())
    n_entries = len(tape.get("entries", []))
    print(f"tape: {len(tape.get('bars', []))} bars, {n_entries} trades\n")
    if n_entries == 0:
        print("no trades in tape — nothing to sweep.")
        return 0

    rows = []
    for cfg in _configs():
        s = replay_tape(tape, cfg, _SPEC)
        rows.append((s.total_r, s.avg_r, s.winrate, s.profit_factor, s.wins, s.losses, cfg.label))
    rows.sort(key=lambda r: r[0], reverse=True)

    print(f"{'config':28} {'totR':>7} {'avgR':>7} {'WR':>5} {'PF':>6}  W/L")
    print("-" * 64)
    for total_r, avg_r, wr, pf, w, l, label in rows[: args.top]:
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{label:28} {total_r:7.2f} {avg_r:7.3f} {wr:5.2f} {pf_s:>6}  {w}/{l}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
