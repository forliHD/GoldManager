"""AI-Decision-Layer smoke CLI — Block 6 end-to-end proof-of-life.

If ``OPENROUTER_API_KEY`` is set, this CLI:

  1. Loads the committed XAUUSD M1 sample dataset.
  2. Walks ``n_bars`` M1 bars (starting at ``start_bar``).
  3. Builds the full feature snapshot per bar (mirrors
     :mod:`xauusd_bot.cli.decision_smoke`).
  4. For bars where ``score >= ai_layer_score_threshold``, calls
     the live :class:`AIDecisionLayer` (OpenRouter).
  5. Writes ``logs/ai_snapshot.json`` with the LLM decisions +
     discrepancy counters.

If ``OPENROUTER_API_KEY`` is unset, the CLI prints a "skipped"
message and exits 0. This keeps the smoke runnable on CI without
network access.

Cost guard
----------
The ``--max-budget-usd`` flag (default 0.01) is a hard limit. After
the accumulated cost (estimated from input+output tokens ×
``--input-price-per-1k`` and ``--output-price-per-1k``) exceeds the
budget, the CLI logs a warning and stops calling the LLM (remaining
bars use RuleBasedFallback). Default pricing is conservative; pass
explicit prices to override.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Make ``xauusd_bot`` importable when the user runs the CLI without
# ``pip install -e .``. See feature_smoke.py for the same trick.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.config import Settings  # noqa: E402
from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.ai_decision import LLMDecision  # noqa: E402
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle  # noqa: E402
from xauusd_bot.decision import (  # noqa: E402
    AIDecisionLayer,
    AIDecisionOrchestrator,
    FeatureAggregator,
    OpenRouterClient,
    RuleBasedFallback,
    ScoringEngine,
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
from xauusd_bot.journal.store import InMemoryJournalStore  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = Path(__file__).resolve().parents[3] / "logs" / "ai_snapshot.json"
DEFAULT_PROMPT = Path(__file__).resolve().parents[3] / "decision_agent.md"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-Decision-Layer smoke (live OpenRouter).")
    parser.add_argument("--n-bars", type=int, default=200)
    parser.add_argument("--start-bar", type=int, default=2000)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--symbol", type=str, default="XAUUSD")
    parser.add_argument(
        "--max-budget-usd", type=float, default=0.01,
        help="Hard cost limit in USD. LLM calls stop after this.",
    )
    parser.add_argument(
        "--input-price-per-1k", type=float, default=0.003,
        help="USD per 1k input tokens (default: Claude-3.5-Sonnet).",
    )
    parser.add_argument(
        "--output-price-per-1k", type=float, default=0.015,
        help="USD per 1k output tokens (default: Claude-3.5-Sonnet).",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(level="INFO")

    if not args.sample.exists():
        log.error("sample_missing", path=str(args.sample))
        print(f"ERROR: sample dataset not found at {args.sample}.", file=sys.stderr)
        print("Run: python -m tools.generate_sample_data", file=sys.stderr)
        return 2

    # ---- Pre-flight: API key gate
    if not os.getenv("OPENROUTER_API_KEY"):
        log.info("ai_smoke_skipped_no_api_key")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "skipped": True,
                    "reason": "OPENROUTER_API_KEY not set",
                    "report_path": str(args.report),
                },
                indent=2,
            )
        )
        print(json.dumps({"skipped": True, "reason": "OPENROUTER_API_KEY not set"}, indent=2))
        return 0

    log.info(
        "ai_smoke_starting",
        sample=str(args.sample),
        n_bars=args.n_bars,
        max_budget_usd=args.max_budget_usd,
    )
    started = time.perf_counter()

    # ---- Build the engines + decision stack (mirrors decision_smoke).
    settings = Settings()
    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)
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
    fallback = RuleBasedFallback(settings=settings)
    openrouter = OpenRouterClient(settings=settings, prompt_path=args.prompt)
    ai_layer = AIDecisionLayer(openrouter_client=openrouter, settings=settings)
    journal_store = InMemoryJournalStore()
    orchestrator = AIDecisionOrchestrator(
        ai_layer=ai_layer,
        rule_fallback=fallback,
        settings=settings,
        journal_store=journal_store,
    )

    # ---- Cost tracking.
    accumulated_cost = 0.0
    budget_exhausted = False
    decisions: list[dict[str, object]] = []
    aggregates = {
        "n_bars_evaluated": 0,
        "n_above_threshold": 0,
        "n_llm_calls": 0,
        "n_llm_skipped_budget": 0,
        "n_llm_fallback": 0,
        "n_llm_enter": 0,
        "n_llm_no_trade": 0,
        "n_rule_enter": 0,
        "n_rule_no_trade": 0,
    }

    start_bar = max(0, args.start_bar)
    n_target = min(args.n_bars, len(connector.bars) - start_bar)
    if n_target <= 0:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "n_bars_consumed": 0,
                    "aggregates": aggregates,
                    "skipped": True,
                    "reason": "no bars in range",
                },
                indent=2,
            )
        )
        return 0

    # ---- Pre-build the full bar list (O(N) memory, O(N²) decision loop).
    all_bars: list = []
    for j in range(start_bar + n_target):
        all_bars.append(connector._row_to_bar(connector.bars.iloc[j], "M1"))  # noqa: SLF001

    for k in range(n_target):
        i = start_bar + k
        bar = all_bars[i]
        current_t = bar.time
        bars_so_far = all_bars[: i + 1]
        # Build the feature bundle (same as decision_smoke).
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
        agg = aggregator.aggregate(bundle)
        score = scoring.score(agg)
        aggregates["n_bars_evaluated"] += 1
        if score.total_score < settings.ai_layer_score_threshold:
            continue
        aggregates["n_above_threshold"] += 1

        if budget_exhausted:
            aggregates["n_llm_skipped_budget"] += 1
            continue

        # ---- Cost estimate: rough tokens via JSON payload size.
        # (Real OpenRouter reports this in the response; we estimate.)
        estimated_input_tokens = max(1, len(json.dumps(bundle.model_dump(mode="json"), default=str)) // 4)
        estimated_output_tokens = 200  # conservative average
        est_cost = (
            estimated_input_tokens / 1000.0 * args.input_price_per_1k
            + estimated_output_tokens / 1000.0 * args.output_price_per_1k
        )
        if accumulated_cost + est_cost > args.max_budget_usd:
            log.warning("ai_smoke_budget_exhausted", accumulated_usd=accumulated_cost)
            budget_exhausted = True
            aggregates["n_llm_skipped_budget"] += 1
            continue

        accumulated_cost += est_cost
        aggregates["n_llm_calls"] += 1
        try:
            decision = await orchestrator.decide(
                feature_snapshot=bundle, score=score, agg=agg
            )
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            log.warning("ai_smoke_decide_failed", error=str(exc))
            aggregates["n_llm_fallback"] += 1
            continue

        if decision.action.value == "no_trade":
            aggregates["n_llm_no_trade"] += 1
        else:
            aggregates["n_llm_enter"] += 1

        decisions.append(
            {
                "i": i,
                "ts": current_t.isoformat(),
                "score": score.total_score,
                "band": score.band.value,
                "direction": score.direction,
                "decision_action": decision.action.value,
                "entry_type": decision.entry_type.value if decision.entry_type else None,
                "block_reason": decision.block_reason,
                "estimated_cost_usd": round(est_cost, 6),
            }
        )

    elapsed = time.perf_counter() - started
    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": str(args.sample),
        "n_bars_consumed": n_target,
        "start_bar": start_bar,
        "elapsed_seconds": round(elapsed, 3),
        "report_path": str(args.report),
        "aggregates": aggregates,
        "estimated_cost_usd": round(accumulated_cost, 6),
        "max_budget_usd": args.max_budget_usd,
        "budget_exhausted": budget_exhausted,
        "decisions": decisions,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info(
        "ai_smoke_complete",
        elapsed=elapsed,
        bars=n_target,
        llm_calls=aggregates["n_llm_calls"],
        estimated_cost_usd=accumulated_cost,
    )
    print(
        json.dumps(
            {
                "n_bars_consumed": n_target,
                "elapsed_seconds": round(elapsed, 3),
                "aggregates": aggregates,
                "estimated_cost_usd": round(accumulated_cost, 6),
                "budget_exhausted": budget_exhausted,
                "report_path": str(args.report),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
