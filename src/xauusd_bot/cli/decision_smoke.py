"""Decision-Engine smoke CLI — block-3 end-to-end proof-of-life.

Loads the committed XAUUSD M1 sample dataset, drives the
:class:`ReplayConnector` bar-by-bar, and feeds every bar through the
full decision stack:

    bars → features (8 engines) → FeatureAggregator → ScoringEngine
         → RuleBasedFallback → TradeQualificationEngine

Writes ``logs/decision_snapshot.json`` with one row per M1 bar
(``aggregated_features``, ``subscores``, ``total_score``, ``band``,
``reasoning``, ``decision.action``, ``entry_type``, ``block_reasons``)
and a final aggregate count of long/short/scout/reduced/full/blocked
actions. The CLI exits 0 on success.

PIT-correctness: each row's ``current_t`` is the bar's close time.
The engine stacks filter ``bars <= current_t`` before scoring, so
no row can ever score on a future bar.

Run from the repo root::

    python -m xauusd_bot.cli.decision_smoke

Or with custom parameters::

    python -m xauusd_bot.cli.decision_smoke --n-bars 10000 \\
        --sample data/sample/xauusd_m1_sample.parquet \\
        --report logs/decision_snapshot.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Make the ``xauusd_bot`` package importable when the user runs the CLI
# without ``pip install -e .``. See feature_smoke.py for the same trick.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]  # .../src
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.config import Settings  # noqa: E402
from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle  # noqa: E402
from xauusd_bot.decision import (  # noqa: E402
    FeatureAggregator,
    RuleBasedFallback,
    ScoringEngine,
    TradeQualificationEngine,
)
from xauusd_bot.features.fvg import FVGEngine  # noqa: E402
from xauusd_bot.features.liquidity import LiquidityEngine  # noqa: E402
from xauusd_bot.features.momentum import CandleMomentumEngine  # noqa: E402
from xauusd_bot.features.news import NewsContextEngine, StubNewsProvider  # noqa: E402
from xauusd_bot.features.session import SessionEngine  # noqa: E402
from xauusd_bot.features.structure import MarketStructureEngine  # noqa: E402
from xauusd_bot.features.volume_range import FixedVolumeRangeEngine  # noqa: E402
from xauusd_bot.features.vwap import TripleVWAPEngine  # noqa: E402
from xauusd_bot.features._indicators import atr as compute_atr  # noqa: E402
from xauusd_bot.features._indicators import bars_to_df  # noqa: E402
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = Path(__file__).resolve().parents[3] / "logs" / "decision_snapshot.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decision-engine smoke test for block 3.")
    parser.add_argument(
        "--n-bars",
        type=int,
        default=200,
        help=(
            "Number of M1 bars to replay. Each bar recomputes every engine from "
            "scratch (O(N) per bar, O(N^2) total) so 200 bars is the default "
            "smoke budget. For larger windows use the BacktestEngine (Block 5)."
        ),
    )
    parser.add_argument(
        "--start-bar",
        type=int,
        default=0,
        help=(
            "Index in the source dataset to start replaying from. Use a "
            "non-zero value (e.g. 5000) to skip the engine warm-up phase "
            "and get meaningful signals within a small --n-bars budget."
        ),
    )
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE, help="Source parquet/csv.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Output decision-snapshot JSON.")
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

    log.info("decision_smoke_starting", sample=str(args.sample), n_bars=args.n_bars)
    started = time.perf_counter()

    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)

    # Construct the engines and decision stack once.
    session_eng = SessionEngine()
    vwap_eng = TripleVWAPEngine()
    vr_eng = FixedVolumeRangeEngine()
    fvg_eng = FVGEngine()
    structure_eng = MarketStructureEngine()
    momentum_eng = CandleMomentumEngine()
    liquidity_eng = LiquidityEngine()
    news_eng = NewsContextEngine(provider=StubNewsProvider())

    aggregator = FeatureAggregator()
    scoring = ScoringEngine()
    settings = Settings()  # uses .env / env; conftest-friendly defaults in tests
    fallback = RuleBasedFallback(settings=settings)
    qualifier = TradeQualificationEngine(settings=settings)

    # We sample every M1 bar; the per-engine ``compute()`` methods are
    # O(N) over the cumulative history, so per-bar cost grows with
    # N. For 10k bars this stays under a few seconds.
    start_bar = max(0, args.start_bar)
    n_target = min(args.n_bars, len(connector.bars) - start_bar)
    if n_target <= 0:
        # Edge case: emit a minimal valid snapshot and exit 0.
        empty_ts = datetime.now(tz=UTC)
        empty_bundle = FeatureSnapshotBundle(
            ts=empty_ts,
            session=session_eng.compute([], empty_ts),
            vwap=vwap_eng.compute([], empty_ts),
            volume_range=vr_eng.compute([], empty_ts),
            fvg=fvg_eng.compute([], empty_ts),
            structure=structure_eng.compute([], empty_ts),
            momentum=momentum_eng.compute([], empty_ts),
            liquidity=liquidity_eng.compute([], 0.0, [], empty_ts),
            news=news_eng.compute(empty_ts),
        )
        agg = aggregator.aggregate(empty_bundle)
        score = scoring.score(agg)
        decision = fallback.decide(score, agg, account=connector.get_account())
        qualification = qualifier.qualify(decision, score, agg, empty_bundle, account=connector.get_account())
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "sample": str(args.sample),
                    "n_bars_consumed": 0,
                    "rows": [],
                    "aggregates": _empty_aggregates(),
                    "qualified_count": 0,
                },
                indent=2,
                default=str,
            )
        )
        print(json.dumps({"n_bars_consumed": 0, "qualified_count": 0}, indent=2))
        return 0

    rows: list[dict[str, object]] = []
    aggregates = {
        "n_long_full": 0,
        "n_long_reduced": 0,
        "n_long_scout": 0,
        "n_short_full": 0,
        "n_short_reduced": 0,
        "n_short_scout": 0,
        "n_blocked": 0,
        "n_total": 0,
        "n_qualified": 0,
    }
    block_reason_counts: dict[str, int] = {}

    # Pre-build the full bar list (one Bar object per M1 row). The
    # engines are O(N) per call, so the smoke is O(N²) overall —
    # see the help text on --n-bars. The engines filter the list
    # internally on ``current_t``, so PIT is preserved.
    all_bars: list = []
    for j in range(start_bar + n_target):
        all_bars.append(connector._row_to_bar(connector.bars.iloc[j], "M1"))  # noqa: SLF001

    for k in range(n_target):
        i = start_bar + k
        bar = all_bars[i]
        current_t = bar.time
        # Pass the cumulative slice. This is O(i) per bar to copy;
        # the per-engine compute() is also O(i), so the total cost
        # is O(N²). For large windows use the BacktestEngine
        # (Block 5) which uses incremental updates.
        bars_so_far = all_bars[: i + 1]

        # Features. PIT = current_t.
        session_out = session_eng.compute(bars_so_far, current_t)
        vwap_out = vwap_eng.compute(bars_so_far, current_t)
        vr_out = vr_eng.compute(bars_so_far, current_t)
        fvg_out = fvg_eng.compute(bars_so_far, current_t)
        structure_out = structure_eng.compute(bars_so_far, current_t)
        momentum_out = momentum_eng.compute(bars_so_far, current_t)
        liquidity_out = liquidity_eng.compute(
            structure_out.liquidity_pools, float(bar.close), bars_so_far, current_t
        )
        news_out = news_eng.compute(current_t)

        bundle = FeatureSnapshotBundle(
            ts=current_t,
            session=session_out,
            vwap=vwap_out,
            volume_range=vr_out,
            fvg=fvg_out,
            structure=structure_out,
            momentum=momentum_out,
            liquidity=liquidity_out,
            news=news_out,
            atr=compute_atr(bars_to_df(bars_so_far), period=14),
        )

        # Decision stack.
        agg = aggregator.aggregate(bundle)
        score = scoring.score(agg)
        account = connector.get_account()
        decision = fallback.decide(score, agg, account=account)
        qualification = qualifier.qualify(decision, score, agg, bundle, account=account)

        # Aggregates.
        aggregates["n_total"] += 1
        if qualification.qualified:
            aggregates["n_qualified"] += 1
            side = "long" if qualification.final_action.value == "enter_long" else "short"
            et = qualification.final_entry_type.value if qualification.final_entry_type else "scout"
            aggregates[f"n_{side}_{et}"] += 1
        else:
            aggregates["n_blocked"] += 1
            for r in qualification.block_reasons:
                block_reason_counts[r] = block_reason_counts.get(r, 0) + 1

        # Keep rows bounded: at most 200 representative rows in the
        # JSON (every 50th bar by default for n=10k) to keep the
        # file under 1 MB. Full per-bar data is the verifier's job
        # via tests.
        if k % max(1, n_target // 200) == 0 or qualification.qualified:
            rows.append(
                {
                    "i": i,
                    "ts": current_t.isoformat(),
                    "close": float(bar.close),
                    "session": session_out.current_session.value,
                    "news_in_blackout": news_out.in_blackout_flag,
                    "structure_trend": structure_out.trend,
                    "atr": bundle.atr,
                    "dominant_engine": agg.dominant_engine,
                    "has_data": agg.has_data,
                    "n_conflicts": len(agg.conflicts),
                    "subscores": {k: round(v.value, 2) for k, v in agg.subscores.items()},
                    "total_score": score.total_score,
                    "band": score.band.value,
                    "direction": score.direction,
                    "reasoning": score.reasoning,
                    "decision_action": decision.action.value,
                    "entry_type": decision.entry_type.value if decision.entry_type else None,
                    "decision_block_reason": decision.block_reason,
                    "qualified": qualification.qualified,
                    "qualification_id": str(qualification.qualification_id),
                    "final_action": qualification.final_action.value,
                    "final_entry_type": (
                        qualification.final_entry_type.value
                        if qualification.final_entry_type
                        else None
                    ),
                    "block_reasons": qualification.block_reasons,
                }
            )

    elapsed = time.perf_counter() - started
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": str(args.sample),
        "n_bars_consumed": n_target,
        "start_bar": start_bar,
        "elapsed_seconds": round(elapsed, 3),
        "bars_per_second": round(n_target / max(elapsed, 1e-6), 1),
        "current_t": (
            connector.bars["time"].iloc[start_bar + n_target - 1].to_pydatetime().isoformat()
        ),
        "report_path": str(args.report),
        "aggregates": aggregates,
        "block_reason_counts": block_reason_counts,
        "qualified_count": aggregates["n_qualified"],
        "rows": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info(
        "decision_smoke_complete",
        elapsed=elapsed,
        bars=n_target,
        qualified=aggregates["n_qualified"],
        blocked=aggregates["n_blocked"],
    )
    # Also print a compact summary on stdout.
    print(
        json.dumps(
            {
                "n_bars_consumed": n_target,
                "elapsed_seconds": round(elapsed, 3),
                "aggregates": aggregates,
                "block_reason_counts": block_reason_counts,
                "qualified_count": aggregates["n_qualified"],
                "report_path": str(args.report),
            },
            indent=2,
        )
    )
    return 0


def _empty_aggregates() -> dict[str, int]:
    return {
        "n_long_full": 0,
        "n_long_reduced": 0,
        "n_long_scout": 0,
        "n_short_full": 0,
        "n_short_reduced": 0,
        "n_short_scout": 0,
        "n_blocked": 0,
        "n_total": 0,
        "n_qualified": 0,
    }


if __name__ == "__main__":
    raise SystemExit(main())
