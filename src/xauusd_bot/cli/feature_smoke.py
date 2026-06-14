"""Feature-Engine smoke CLI — block-2 end-to-end proof-of-life.

Loads the committed XAUUSD M1 sample dataset, drives the
:class:`ReplayConnector` bar-by-bar, and feeds every bar into the
feature engine stack (Session / TripleVWAP / FixedVolumeRange / FVG /
MarketStructure / CandleMomentum / Liquidity / News). At the end of
the replay it writes:

* ``logs/feature_snapshot.json`` — the final :class:`FeatureSnapshotBundle`
* ``data/overlay/overlay_levels.json`` — the chart overlay JSON

The single check the verifier uses: if both files exist and contain
plausible numbers, block 2 is shipped. The CLI exits 0 on success.

Run from the repo root::

    python -m xauusd_bot.cli.feature_smoke

Or with custom parameters::

    python -m xauusd_bot.cli.feature_smoke --n-bars 10000 --sample path/to/x.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Make the ``xauusd_bot`` package importable when the user runs the CLI
# without ``pip install -e .`` (e.g. ``python src/xauusd_bot/cli/feature_smoke.py``
# straight from a checkout). When the package is already importable this
# is a no-op.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]  # .../src
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle  # noqa: E402
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402
from xauusd_bot.features.fvg import FVGEngine  # noqa: E402
from xauusd_bot.features.liquidity import LiquidityEngine  # noqa: E402
from xauusd_bot.features.momentum import CandleMomentumEngine  # noqa: E402
from xauusd_bot.features.news import NewsContextEngine, StubNewsProvider  # noqa: E402
from xauusd_bot.features.session import SessionEngine  # noqa: E402
from xauusd_bot.features.structure import MarketStructureEngine  # noqa: E402
from xauusd_bot.features.volume_range import FixedVolumeRangeEngine  # noqa: E402
from xauusd_bot.features.vwap import TripleVWAPEngine  # noqa: E402
from xauusd_bot.viz.overlay_writer import OverlayWriter  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = Path(__file__).resolve().parents[3] / "logs" / "feature_snapshot.json"
DEFAULT_OVERLAY = Path(__file__).resolve().parents[3] / "data" / "overlay" / "overlay_levels.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Feature-engine smoke test for block 2.")
    parser.add_argument("--n-bars", type=int, default=10000, help="Number of M1 bars to replay.")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE, help="Source parquet/csv.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Output feature-snapshot JSON.")
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY, help="Output overlay JSON path.")
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

    log.info("feature_smoke_starting", sample=str(args.sample), n_bars=args.n_bars)
    started = time.perf_counter()

    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)

    # Construct the engines once. They are stateless — we re-call .compute()
    # at the end of the replay with the full bar history.
    session_eng = SessionEngine()
    vwap_eng = TripleVWAPEngine()
    vr_eng = FixedVolumeRangeEngine()
    fvg_eng = FVGEngine()
    structure_eng = MarketStructureEngine()
    momentum_eng = CandleMomentumEngine()
    liquidity_eng = LiquidityEngine()
    news_eng = NewsContextEngine(provider=StubNewsProvider())
    overlay_writer = OverlayWriter(output_path=args.overlay)

    # Read every bar (PIT is enforced by passing current_t = last bar's time).
    n_target = min(args.n_bars, len(connector.bars))
    bars: list = []
    for i in range(n_target):
        row = connector.bars.iloc[i]
        bars.append(connector._row_to_bar(row, "M1"))  # noqa: SLF001 - internal API, fine here
    last_bar_time: datetime = connector.bars["time"].iloc[n_target - 1].to_pydatetime()

    # Run all engines. Each is PIT-safe: passing last_bar_time as the cursor
    # ensures no look-ahead.
    session_out = session_eng.compute(bars, last_bar_time)
    vwap_out = vwap_eng.compute(bars, last_bar_time)
    vr_out = vr_eng.compute(bars, last_bar_time)
    fvg_out = fvg_eng.compute(bars, last_bar_time)
    structure_out = structure_eng.compute(bars, last_bar_time)
    momentum_out = momentum_eng.compute(bars, last_bar_time)
    liquidity_out = liquidity_eng.compute(structure_out.liquidity_pools, float(bars[-1].close), bars, last_bar_time)
    news_out = news_eng.compute(last_bar_time)

    bundle = FeatureSnapshotBundle(
        ts=last_bar_time,
        session=session_out,
        vwap=vwap_out,
        volume_range=vr_out,
        fvg=fvg_out,
        structure=structure_out,
        momentum=momentum_out,
        liquidity=liquidity_out,
        news=news_out,
    )

    # Write the snapshot JSON.
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(bundle.model_dump(mode="json"), indent=2, default=str))

    # Write the overlay JSON.
    overlay_writer.write(ts=last_bar_time, vwap=vwap_out, volume_range=vr_out, fvg=fvg_out)

    elapsed = time.perf_counter() - started
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": str(args.sample),
        "n_bars_consumed": len(bars),
        "elapsed_seconds": round(elapsed, 3),
        "bars_per_second": round(len(bars) / max(elapsed, 1e-6), 1),
        "current_t": last_bar_time.isoformat(),
        "snapshot_path": str(args.report),
        "overlay_path": str(args.overlay),
        "snapshot_exists": args.report.exists(),
        "overlay_exists": args.overlay.exists(),
        "engines": {
            "session": session_out.current_session.value,
            "session_risk_factor": session_out.session_risk_factor,
            "vwap_is_cluster": vwap_out.is_cluster,
            "weekly_state": vr_out.weekly.state.value,
            "monthly_state": vr_out.monthly.state.value,
            "yearly_state": vr_out.yearly.state.value,
            "fvg_zones_count": len(fvg_out.zones),
            "fvg_top_zones_count": len(fvg_out.top_zones),
            "structure_trend": structure_out.trend,
            "structure_last_bos": structure_out.last_bos.type.value if structure_out.last_bos else None,
            "structure_last_choch": structure_out.last_choch.type.value if structure_out.last_choch else None,
            "liquidity_pools_count": len(structure_out.liquidity_pools),
            "momentum_score": round(momentum_out.score, 2),
            "news_in_blackout": news_out.in_blackout_flag,
        },
    }
    log.info("feature_smoke_complete", elapsed=elapsed, bars=len(bars))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
