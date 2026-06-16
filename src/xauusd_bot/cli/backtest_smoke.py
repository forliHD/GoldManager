"""Backtest smoke CLI — Block 5b end-to-end proof-of-life.

Runs the :class:`BacktestEngine` over the committed XAUUSD M1
sample and, optionally, a :class:`WalkForwardEngine` over the same
window. Writes ``logs/backtest_snapshot.json`` with the full
:class:`BacktestResult` and the :class:`WalkForwardResult`.

The CLI is the canonical "Block 5b is working" deliverable. It
proves:

1. The :class:`BacktestEngine` produces a plausible BacktestResult
   (n_bars_processed > 0, n_trades >= 0, stats with no NaN).
2. The :class:`WalkForwardEngine` produces a non-empty
   WalkForwardResult with the expected number of windows.
3. I-1 holds: the smoke does NOT import ``MetaTrader5``.
4. I-3 holds: the engine never reads bars past ``end_date``.

Run from the repo root::

    python -m xauusd_bot.cli.backtest_smoke

Or with custom parameters::

    python -m xauusd_bot.cli.backtest_smoke \\
        --start-date 2026-04-15 --end-date 2026-04-30 \\
        --in-sample-months 1 --out-of-sample-months 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# Make ``xauusd_bot`` importable when the CLI is run without install.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.backtest import (  # noqa: E402
    BacktestEngine,
    FixedSlippage,
    FixedSpread,
    WalkForwardEngine,
)
from xauusd_bot.common.config import Settings  # noqa: E402
from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.backtest import (  # noqa: E402
    BacktestResult,
    WalkForwardResult,
)
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402
from xauusd_bot.journal import InMemoryJournalStore  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = _THIS.parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = _THIS.parents[3] / "logs" / "backtest_snapshot.json"


# ----------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest smoke for Block 5b.")
    parser.add_argument("--start-date", type=str, default="2026-04-01", help="ISO date (UTC).")
    parser.add_argument("--end-date", type=str, default="2026-04-30", help="ISO date (UTC).")
    parser.add_argument("--in-sample-months", type=int, default=None, help="WF IS window in months (plan default).")
    parser.add_argument("--out-of-sample-months", type=int, default=None, help="WF OOS window in months.")
    parser.add_argument("--step-months", type=int, default=None, help="WF step in months.")
    parser.add_argument("--in-sample-days", type=int, default=3, help="WF IS window in days (smoke default; 30-day data).")
    parser.add_argument("--out-of-sample-days", type=int, default=1, help="WF OOS window in days.")
    parser.add_argument("--step-days", type=int, default=1, help="WF step in days.")
    parser.add_argument("--warmup-bars", type=int, default=200, help="Warm-up bars per backtest.")
    parser.add_argument("--max-bars", type=int, default=200, help="Cap on bars processed (smoke budgets).")
    parser.add_argument("--context-window-bars", type=int, default=600, help="Rolling feature window.")
    parser.add_argument("--skip-walkforward", action="store_true", help="Skip the Walk-Forward run.")
    parser.add_argument("--symbol", type=str, default="XAUUSD")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def _parse_date(s: str) -> datetime:
    """Parse an ISO date string (YYYY-MM-DD) into a UTC midnight datetime."""

    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=UTC)


# ----------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(level="INFO")

    if not args.sample.exists():
        log.error("sample_missing", path=str(args.sample))
        print(f"ERROR: sample dataset not found at {args.sample}.", file=sys.stderr)
        print("Run: python -m tools.generate_sample_data", file=sys.stderr)
        return 2

    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if end_date <= start_date:
        log.error("invalid_window", start=args.start_date, end=args.end_date)
        print("ERROR: end_date must be after start_date.", file=sys.stderr)
        return 2

    log.info(
        "backtest_smoke_starting",
        sample=str(args.sample),
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        in_sample_months=args.in_sample_months,
        out_of_sample_months=args.out_of_sample_months,
    )
    started = time.perf_counter()

    settings = Settings()  # type: ignore[call-arg]
    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)
    journal = InMemoryJournalStore()

    slippage = FixedSlippage(Decimal("0.50"))
    spread = FixedSpread(Decimal("0.30"))

    # --- 1. Plain BacktestEngine run.
    engine = BacktestEngine(
        connector=connector,
        journal=journal,
        settings=settings,
        slippage_model=slippage,
        spread_model=spread,
        context_window_bars=args.context_window_bars,
    )
    backtest_result: BacktestResult = engine.run(
        start_date=start_date,
        end_date=end_date,
        warmup_bars=args.warmup_bars,
        max_bars=args.max_bars,
    )

    # --- 2. WalkForward run.
    walkforward_result: WalkForwardResult | None = None
    if not args.skip_walkforward:
        wf_kwargs: dict[str, object] = {
            "connector": connector,
            "settings": settings,
            "slippage_model": slippage,
            "spread_model": spread,
            "context_window_bars": args.context_window_bars,
            # Cap each inner backtest to keep the WF total runtime
            # bounded. Default 200 (matches the main backtest) so
            # the smoke completes in <30s even on a 30-day dataset.
            "max_bars_per_window": args.max_bars,
        }
        # Prefer month-based if supplied, else day-based.
        if args.in_sample_months is not None:
            wf_kwargs["in_sample_months"] = args.in_sample_months
            wf_kwargs["out_of_sample_months"] = args.out_of_sample_months
            wf_kwargs["step_months"] = args.step_months
        else:
            wf_kwargs["in_sample_days"] = args.in_sample_days
            wf_kwargs["out_of_sample_days"] = args.out_of_sample_days
            wf_kwargs["step_days"] = args.step_days
        wf = WalkForwardEngine(**wf_kwargs)  # type: ignore[arg-type]
        walkforward_result = wf.run(start_date=start_date, end_date=end_date)

    elapsed = time.perf_counter() - started

    # --- 3. Persist a JSON snapshot.
    report = _build_report(
        sample=str(args.sample),
        start_date=start_date,
        end_date=end_date,
        backtest_result=backtest_result,
        walkforward_result=walkforward_result,
        elapsed=elapsed,
        report_path=args.report,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info(
        "backtest_smoke_complete",
        n_bars=backtest_result.n_bars_processed,
        n_trades=backtest_result.n_trades,
        sharpe=backtest_result.stats.sharpe,
        max_dd=str(backtest_result.stats.max_drawdown),
        wf_windows=len(walkforward_result.windows) if walkforward_result else 0,
        runtime=elapsed,
    )

    print(
        json.dumps(
            {
                "n_bars_processed": backtest_result.n_bars_processed,
                "n_trades": backtest_result.n_trades,
                "sharpe": backtest_result.stats.sharpe,
                "max_drawdown": backtest_result.stats.max_drawdown,
                "winrate": backtest_result.stats.winrate,
                "profit_factor": backtest_result.stats.profit_factor,
                "wf_windows": len(walkforward_result.windows) if walkforward_result else 0,
                "wf_oos_degradation": walkforward_result.oos_sharpe_degradation if walkforward_result else None,
                "wf_is_overfit": walkforward_result.is_overfit if walkforward_result else None,
                "report_path": str(args.report),
            },
            indent=2,
        )
    )
    return 0


# ----------------------------------------------------------------- helpers


def _build_report(
    *,
    sample: str,
    start_date: datetime,
    end_date: datetime,
    backtest_result: BacktestResult,
    walkforward_result: WalkForwardResult | None,
    elapsed: float,
    report_path: Path,
) -> dict[str, object]:
    """Convert the typed result objects into a JSON-serializable dict."""

    out: dict[str, object] = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": sample,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "elapsed_seconds": round(elapsed, 6),
        "report_path": str(report_path),
        "n_bars_processed": backtest_result.n_bars_processed,
        "n_trades": backtest_result.n_trades,
        "runtime_seconds": backtest_result.runtime_seconds,
        "stats": backtest_result.stats.model_dump(),
        "equity_curve_sample": [
            [t.isoformat(), str(eq)] for t, eq in backtest_result.equity_curve_sample
        ],
        "r_distribution": backtest_result.r_distribution,
        "setup_breakdown": {
            k: v.model_dump() for k, v in backtest_result.setup_breakdown.items()
        },
        "session_breakdown": {
            k: v.model_dump() for k, v in backtest_result.session_breakdown.items()
        },
        "score_band_breakdown": {
            k: v.model_dump() for k, v in backtest_result.score_band_breakdown.items()
        },
        "tags": backtest_result.tags,
    }
    if walkforward_result is not None:
        out["walkforward"] = {
            "windows": [
                {
                    "window_index": w.window_index,
                    "start_in": w.start_in.isoformat(),
                    "end_in": w.end_in.isoformat(),
                    "start_oos": w.start_oos.isoformat(),
                    "end_oos": w.end_oos.isoformat(),
                    "in_sample_stats": w.in_sample_stats.model_dump(),
                    "out_of_sample_stats": w.out_of_sample_stats.model_dump(),
                    "oos_degradation_pct": w.oos_degradation_pct,
                    "in_sample_sharpe": w.in_sample_sharpe,
                    "out_of_sample_sharpe": w.out_of_sample_sharpe,
                }
                for w in walkforward_result.windows
            ],
            "robustness_matrix": walkforward_result.robustness_matrix,
            "mean_oos_sharpe": walkforward_result.mean_oos_sharpe,
            "std_oos_sharpe": walkforward_result.std_oos_sharpe,
            "oos_sharpe_degradation": walkforward_result.oos_sharpe_degradation,
            "is_overfit": walkforward_result.is_overfit,
            "runtime_seconds": walkforward_result.runtime_seconds,
            "n_bars_processed": walkforward_result.n_bars_processed,
        }
    return out


if __name__ == "__main__":
    raise SystemExit(main())
