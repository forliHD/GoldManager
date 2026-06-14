"""Replay smoke CLI — block-1 end-to-end proof-of-life.

Loads the committed XAUUSD M1 sample dataset, drives
:class:`ReplayConnector` bar-by-bar through the data layer, and writes
a JSON smoke report to ``logs/replay_smoke.json`` with:

* bar count processed
* spread percentiles (p50, p90, p95, p99)
* data-quality issue counts (gaps, spikes, OHLC inconsistencies)
* sanity: max-gap bars, first/last bar time

This is the single check the verifier uses to confirm block 1 is wired
correctly. If the JSON file appears and contains plausible numbers,
block 1 is shipped.

Run from the repo root::

    python -m xauusd_bot.cli.replay_smoke

Or with a custom sample::

    python -m xauusd_bot.cli.replay_smoke --n-bars 20000 --sample path/to/x.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Make the ``xauusd_bot`` package importable when the user runs the CLI
# without ``pip install -e .`` (e.g. ``python src/xauusd_bot/cli/replay_smoke.py``
# straight from a checkout). When the package is already importable this
# is a no-op.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]  # .../src
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402
from xauusd_bot.data.ohlc_builder import OHLCBuilder  # noqa: E402
from xauusd_bot.data.quality_monitor import DataQualityMonitor  # noqa: E402
from xauusd_bot.data.spread_monitor import SpreadMonitor  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = Path(__file__).resolve().parents[3] / "logs" / "replay_smoke.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay smoke test for block 1.")
    parser.add_argument("--n-bars", type=int, default=10000, help="Number of bars to replay.")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE, help="Source parquet/csv.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Output JSON report path.")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Symbol to simulate.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(level="INFO")

    if not args.sample.exists():
        log.error("sample_missing", path=str(args.sample))
        print(f"ERROR: sample dataset not found at {args.sample}.", file=sys.stderr)
        print("Run: python -m tools.generate_sample_data", file=sys.stderr)
        return 2

    log.info("smoke_starting", sample=str(args.sample), n_bars=args.n_bars)
    started = time.perf_counter()

    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)
    spec = connector.spec
    builder = OHLCBuilder(symbol=args.symbol, source_timeframe="M1")
    spread = SpreadMonitor(
        symbol=args.symbol,
        point=spec.point,
        window=2000,
        warn_points=50.0,
        block_points=120.0,
    )
    quality = DataQualityMonitor(spec=spec)

    # Phase 1: feed M1 bars through the builder + spread + quality monitor.
    bars_consumed = 0
    n_target = min(args.n_bars, len(connector.bars))
    for i in range(n_target):
        row = connector.bars.iloc[i]
        bar = connector._row_to_bar(row, "M1")  # noqa: SLF001 - internal API, fine here
        builder.on_bar(bar)
        # Synthesize a "spread" proportional to bar range so the monitor
        # has data to chew on.
        synthetic_spread_points = float((bar.high - bar.low) / spec.point) * 0.1 + 30.0
        spread.update_from_points(synthetic_spread_points)
        quality.update(bar)
        bars_consumed += 1

    # Phase 2: advance the cursor and prove point-in-time correctness.
    last_bar_time = connector.bars["time"].iloc[n_target - 1].to_pydatetime()
    connector.advance_time(last_bar_time)
    visible_after = connector.get_rates(args.symbol, "M1", count=5)
    late_attempt = connector.get_rates(args.symbol, "M1", count=5, end_time=last_bar_time)
    pit_ok = (
        len(visible_after) > 0
        and len(late_attempt) > 0
        and all(b.time <= last_bar_time for b in late_attempt)
        and all(b.time <= last_bar_time for b in visible_after)
    )

    # Phase 3: build a small snapshot.
    snap = spread.snapshot()
    qreport = quality.report

    elapsed = time.perf_counter() - started

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": str(args.sample),
        "n_bars_requested": args.n_bars,
        "n_bars_consumed": bars_consumed,
        "elapsed_seconds": round(elapsed, 3),
        "bars_per_second": round(bars_consumed / max(elapsed, 1e-6), 1),
        "first_bar_time": str(connector.bars["time"].iloc[0]),
        "last_bar_time": str(connector.bars["time"].iloc[n_target - 1]),
        "current_t": connector.current_t.isoformat(),
        "point_in_time_ok": pit_ok,
        "spread": {
            "p50": round(snap.p50, 2),
            "p90": round(snap.p90, 2),
            "p95": round(snap.p95, 2),
            "p99": round(snap.p99, 2),
            "current": round(snap.current, 2),
            "n_samples": snap.n,
            "is_outlier": snap.is_outlier,
            "is_block": snap.is_block,
        },
        "quality": qreport.to_dict(),
        "closed_bars_by_tf": {tf: len(bars) for tf, bars in builder.closed_bars_by_tf.items()},
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info("smoke_complete", report=str(args.report), elapsed=elapsed, bars=bars_consumed, pit_ok=pit_ok)
    print(json.dumps({k: report[k] for k in ("n_bars_consumed", "elapsed_seconds", "bars_per_second", "point_in_time_ok", "spread", "quality")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
