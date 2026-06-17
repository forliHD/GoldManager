"""Daily-review smoke CLI — Block 5c end-to-end proof-of-life.

Runs the :class:`ReviewEngine` for a single day. Pulls trades /
snapshots / discrepancies from the :class:`InMemoryJournalStore`,
builds the :class:`KPISummary` via the Block-5a aggregators, and
(if ``OPENROUTER_API_KEY`` is set) calls the Reviewer LLM to
produce a list of :class:`ReviewProposal`. The proposals are then
materialised as :class:`FittingProposal` (status='proposed') via
the :class:`FittingProposalEngine`.

When ``OPENROUTER_API_KEY`` is unset, the CLI exits 0 with
``status='skipped'`` and ``proposals_count=0`` — the smoke is
"wired", but the LLM call was bypassed (Caveat 4i.4).

Run from the repo root::

    OPENROUTER_API_KEY=sk-or-... python -m xauusd_bot.cli.daily_review_smoke \\
        --day 2026-04-15

Or without the key (smoke wiring only)::

    python -m xauusd_bot.cli.daily_review_smoke --day 2026-04-15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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

DEFAULT_REPORT = _THIS.parents[3] / "logs" / "daily_review.json"


# ----------------------------------------------------------------- synthetic data


def _seed_synthetic_trades(
    *,
    store: InMemoryJournalStore,
    day: date,
    n_trades: int,
    seed: int,
) -> None:
    """Seed the in-memory journal with ``n_trades`` synthetic trades for ``day``.

    The trades are deterministic given ``seed``. Used only for the
    smoke — a real review would read from the journal populated by
    the live or backtest pipeline.

    This function builds the records (sync), then schedules the
    writes via the running event loop from the caller.
    """

    import random

    rng = random.Random(seed)
    bands = list(ScoreBand)
    entry_types = list(EntryType)
    trade_records: list[TradeRecord] = []
    snap_records: list[FeatureSnapshotRecord] = []
    for i in range(n_trades):
        ts = datetime(day.year, day.month, day.day, 9 + (i % 6), (i * 11) % 60, tzinfo=UTC)
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
        trade_records.append(trade)

        # Also seed a snapshot for each trade.
        snap = FeatureSnapshotRecord(
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
        snap_records.append(snap)
    return trade_records, snap_records


async def _write_records(
    store: InMemoryJournalStore,
    trade_records: list[TradeRecord],
    snap_records: list[FeatureSnapshotRecord],
) -> None:
    for t in trade_records:
        await store.write_trade(t)
    for s in snap_records:
        await store.write_feature_snapshot(s)


async def _seed_synthetic_snapshots_only(
    *,
    store: InMemoryJournalStore,
    day: date,
    n_snapshots: int,
) -> None:
    """Seed snapshots (no trades) — used to test the insufficient-data path."""

    bands = list(ScoreBand)
    for i in range(n_snapshots):
        ts = datetime(day.year, day.month, day.day, 9 + (i % 8), (i * 7) % 60, tzinfo=UTC)
        snap = FeatureSnapshotRecord(
            timestamp=ts,
            bar_time=ts,
            has_data=True,
            features={
                "session": "asia" if i % 4 < 2 else "london",
                "structure_trend": "range",
                "in_blackout": False,
                "atr": 0.20,
                "score": 55.0 + (i % 30),
                "band": bands[i % len(bands)].value,
                "engine_source": "rule",
            },
        )
        await store.write_feature_snapshot(snap)


# ----------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily review smoke (Block 5c).")
    parser.add_argument("--day", type=str, default=None, help="ISO date (YYYY-MM-DD). Default = yesterday (UTC).")
    parser.add_argument("--n-trades", type=int, default=12, help="Synthetic trades for the smoke (default 12 ≥ daily_min_sample).")
    parser.add_argument("--seed", type=int, default=42, help="Seed for synthetic data.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--force-skipped",
        action="store_true",
        help="Force the LLM call to be skipped even if OPENROUTER_API_KEY is set.",
    )
    return parser.parse_args(argv)


def _parse_day(s: str | None) -> date:
    if s is None:
        return (datetime.now(tz=UTC) - timedelta(days=1)).date()
    return datetime.strptime(s, "%Y-%m-%d").date()


async def _run(args: argparse.Namespace) -> int:
    setup_logging(level="INFO")
    # Provide safe defaults for required Settings fields when no
    # .env exists (smoke runs locally without docker).
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault(
        "TIMESCALEDB_URL",
        "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
    )
    day = _parse_day(args.day)
    settings = Settings()  # type: ignore[call-arg]
    journal = InMemoryJournalStore()

    # Seed synthetic data.
    trade_records, snap_records = _seed_synthetic_trades(
        store=journal, day=day, n_trades=args.n_trades, seed=args.seed
    )
    await _write_records(journal, trade_records, snap_records)
    # Always seed some snapshots too (the smoke needs > 0 snapshots).
    await _seed_synthetic_snapshots_only(store=journal, day=day, n_snapshots=20)

    log.info("daily_review_seed_complete", day=str(day), trades=args.n_trades)

    # Wire up the reviewer (LLM via OpenRouter, shared with Block 6).
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
        daily_min_sample_size=10,
    )

    review = await engine.run_daily(day)
    log.info(
        "daily_review_engine_returned",
        insufficient_data=review.insufficient_data,
        trade_count=review.trade_count,
        proposal_count=len(review.output.proposals) if review.output else 0,
        data_sufficiency=review.output.data_sufficiency if review.output else None,
        has_api_key=has_api_key,
    )

    # Materialise proposals via FittingProposalEngine.
    fpe = FittingProposalEngine(journal=journal, backtest=None)
    proposals = await fpe.from_review(review)

    # Build the report.
    report: dict = {
        "day": str(day),
        "period_start": review.period_start.isoformat(),
        "period_end": review.period_end.isoformat(),
        "trade_count": review.trade_count,
        "snapshot_count": review.snapshot_count,
        "discrepancy_count": review.discrepancy_count,
        "insufficient_data": review.insufficient_data,
        "data_sufficiency": review.output.data_sufficiency if review.output else None,
        "llm_called": has_api_key and not review.insufficient_data,
        "proposals_count": len(proposals),
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
                "review_id": str(p.review_id) if p.review_id else None,
            }
            for p in proposals
        ],
        "summary": review.output.summary if review.output else None,
        "error": review.error,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info("daily_review_report_written", path=str(args.report))
    print(f"daily review ok: {len(proposals)} proposal(s); data_sufficiency={report['data_sufficiency']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"daily_review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())