"""Weekly-review smoke CLI — Block 5c end-to-end proof-of-life.

Like the daily smoke, but for a 7-day window. Cross-day pattern
detection (setup breakdown over days, score-band drift,
discrepancy summary) is included in the ReviewRun.

When ``OPENROUTER_API_KEY`` is unset, the CLI exits 0 with
``status='skipped'`` and ``proposals_count=0`` — the smoke is
"wired", but the LLM call was bypassed (Caveat 4i.4).

Run from the repo root::

    OPENROUTER_API_KEY=sk-or-... python -m xauusd_bot.cli.weekly_review_smoke \\
        --week-start 2026-04-13

Or without the key (smoke wiring only)::

    python -m xauusd_bot.cli.weekly_review_smoke --week-start 2026-04-13
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

# Make ``xauusd_bot`` importable when the CLI is run without install.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.config import Settings  # noqa: E402
from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.decision import (  # noqa: E402
    EntryType,
    ScoreBand,
)
from xauusd_bot.common.schemas.journal import (  # noqa: E402
    ExitReasonTag,
    FeatureSnapshotRecord,
    TradeRecord,
)
from xauusd_bot.decision import OpenRouterClient  # noqa: E402
from xauusd_bot.journal import InMemoryJournalStore  # noqa: E402
from xauusd_bot.review import (  # noqa: E402
    FittingProposalEngine,
    ReviewEngine,
    ReviewerOpenRouterClient,
)

log = structlog.get_logger(__name__)

DEFAULT_REPORT = _THIS.parents[3] / "logs" / "weekly_review.json"


# ----------------------------------------------------------------- synthetic data


def _build_week_trades_and_snaps(
    *,
    week_start: date,
    n_trades: int,
    seed: int,
) -> tuple[list[TradeRecord], list[FeatureSnapshotRecord]]:
    """Build ``n_trades`` synthetic trades + snapshots across a 7-day window."""

    rng = random.Random(seed)
    bands = list(ScoreBand)
    entry_types = list(EntryType)
    trades: list[TradeRecord] = []
    snaps: list[FeatureSnapshotRecord] = []
    for i in range(n_trades):
        day_offset = i % 7
        ts = datetime(
            week_start.year,
            week_start.month,
            week_start.day,
            9 + (i % 6),
            (i * 11) % 60,
            tzinfo=UTC,
        ) + timedelta(days=day_offset)
        entry = Decimal("2370.00") + Decimal(str(rng.uniform(-2.0, 2.0))).quantize(Decimal("0.01"))
        sl = entry - Decimal("5.00")
        is_win = rng.random() > 0.45
        r = rng.uniform(1.0, 2.5) if is_win else rng.uniform(-1.0, -0.5)
        exit_price = entry + Decimal(str(r * 5.0)).quantize(Decimal("0.01"))
        pnl = Decimal(str(r * 50.0)).quantize(Decimal("0.01"))
        trade = TradeRecord(
            timestamp_open=ts,
            timestamp_close=ts + timedelta(minutes=15 + i),
            side="long" if i % 2 == 0 else "short",
            entry_price=entry,
            exit_price=exit_price,
            stop_loss=sl,
            take_profits=[entry + Decimal("5"), entry + Decimal("10"), entry + Decimal("15")],
            volume_lots=Decimal("0.10"),
            risk_amount=Decimal("50"),
            setup_id=uuid4(),
            score=60.0 + (i % 35),
            subscores={"h1_zone": 70.0, "m5_zone": 65.0, "news": 80.0},
            band=bands[i % len(bands)],
            entry_type=entry_types[i % len(entry_types)],
            fill_price=entry + Decimal("0.05"),
            slippage_pips=0.5,
            slippage_bps=2.0,
            session="london",
            atr_at_entry=0.35,
            structure_at_entry="up" if i % 2 == 0 else "down",
            exit_reason=ExitReasonTag.TP1_HIT if is_win else ExitReasonTag.SL_HIT,
            pnl_realized=pnl,
            r_multiple=r,
        )
        trades.append(trade)
        snaps.append(
            FeatureSnapshotRecord(
                timestamp=ts,
                bar_time=ts,
                has_data=True,
                features={
                    "session": "london",
                    "structure_trend": "up" if i % 2 == 0 else "down",
                    "in_blackout": False,
                    "atr": 0.35,
                    "score": 60.0 + (i % 35),
                    "band": bands[i % len(bands)].value,
                    "engine_source": "rule",
                },
            )
        )
    return trades, snaps


async def _write_records(
    store: InMemoryJournalStore,
    trades: list[TradeRecord],
    snaps: list[FeatureSnapshotRecord],
) -> None:
    for t in trades:
        await store.write_trade(t)
    for s in snaps:
        await store.write_feature_snapshot(s)


# ----------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weekly review smoke (Block 5c).")
    parser.add_argument(
        "--week-start",
        type=str,
        default=None,
        help="ISO date (YYYY-MM-DD) of the week's first day. Default = last Monday (UTC).",
    )
    parser.add_argument(
        "--n-trades",
        type=int,
        default=35,
        help="Synthetic trades for the smoke (default 35 ≥ weekly_min_sample=30).",
    )
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--force-skipped",
        action="store_true",
        help="Force the LLM call to be skipped even if OPENROUTER_API_KEY is set.",
    )
    return parser.parse_args(argv)


def _parse_week_start(s: str | None) -> date:
    if s is None:
        # Last Monday (UTC).
        today = datetime.now(tz=UTC).date()
        return today - timedelta(days=today.weekday())
    return datetime.strptime(s, "%Y-%m-%d").date()


async def _run(args: argparse.Namespace) -> int:
    setup_logging(level="INFO")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault(
        "TIMESCALEDB_URL",
        "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
    )
    week_start = _parse_week_start(args.week_start)
    settings = Settings()  # type: ignore[call-arg]
    journal = InMemoryJournalStore()

    trades, snaps = _build_week_trades_and_snaps(
        week_start=week_start, n_trades=args.n_trades, seed=args.seed
    )
    await _write_records(journal, trades, snaps)
    log.info("weekly_review_seed_complete", week_start=str(week_start), trades=len(trades))

    has_api_key = (
        os.environ.get("OPENROUTER_API_KEY", "") not in ("", None)
        and not args.force_skipped
    )
    base_client = OpenRouterClient(settings=settings, prompt_path=Path("decision_agent.md"))
    reviewer = ReviewerOpenRouterClient(
        base_client=base_client, prompt_path=Path("review_agent.md")
    )
    engine = ReviewEngine(
        journal=journal,
        backtest=None,
        reviewer=reviewer,
        settings=settings,
        weekly_min_sample_size=30,
    )

    review = await engine.run_weekly(week_start)
    log.info(
        "weekly_review_engine_returned",
        insufficient_data=review.insufficient_data,
        trade_count=review.trade_count,
        proposal_count=len(review.output.proposals) if review.output else 0,
    )

    fpe = FittingProposalEngine(journal=journal, backtest=None)
    proposals = await fpe.from_review(review)

    report: dict = {
        "week_start": str(week_start),
        "period_start": review.period_start.isoformat(),
        "period_end": review.period_end.isoformat(),
        "trade_count": review.trade_count,
        "snapshot_count": review.snapshot_count,
        "discrepancy_count": review.discrepancy_count,
        "insufficient_data": review.insufficient_data,
        "data_sufficiency": review.output.data_sufficiency if review.output else None,
        "llm_called": has_api_key and not review.insufficient_data,
        "proposals_count": len(proposals),
        "setup_breakdown_over_days": review.setup_breakdown_over_days,
        "score_band_drift": review.score_band_drift,
        "discrepancy_summary": review.discrepancy_summary,
        "proposals": [
            {
                "id": str(p.id),
                "proposal_number": p.proposal_number,
                "category": p.category,
                "observation": p.observation,
                "hypothesis": p.hypothesis,
                "validation_test": p.validation_test,
                "overfitting_risk": p.overfitting_risk,
                "status": p.status,
            }
            for p in proposals
        ],
        "summary": review.output.summary if review.output else None,
        "error": review.error,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info("weekly_review_report_written", path=str(args.report))
    print(
        f"weekly review ok: {len(proposals)} proposal(s); "
        f"data_sufficiency={report['data_sufficiency']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"weekly_review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())