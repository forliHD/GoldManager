"""FittingProposal smoke CLI — Block 5c operator workflow.

List, view, and (manually) approve / reject :class:`FittingProposal`
records persisted in the journal. This is the *human* side of the
state machine — the operator is the only one who decides what
becomes a real change.

Sub-commands::

    --list [--status proposed|backtested|approved|rejected]
           [--category score_threshold|news_blackout|...]
           [--risk low|medium|high]
    --proposal-id <UUID> [--validate] [--approve --operator NAME] [--reject --operator NAME]

The CLI is read-only by default. Status changes require
``--operator NAME`` (recorded as ``decided_by``).

Run from the repo root::

    python -m xauusd_bot.cli.fitting_proposal_smoke --list
    python -m xauusd_bot.cli.fitting_proposal_smoke --list --status proposed
    python -m xauusd_bot.cli.fitting_proposal_smoke --proposal-id <UUID>
    python -m xauusd_bot.cli.fitting_proposal_smoke \\
        --proposal-id <UUID> --approve --operator lucas --note "looks good"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

# Make ``xauusd_bot`` importable when the CLI is run without install.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.review import (  # noqa: E402
    FittingProposalFilter,
)
from xauusd_bot.journal import InMemoryJournalStore  # noqa: E402
from xauusd_bot.review import FittingProposalEngine  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_REPORT = _THIS.parents[3] / "logs" / "fitting_proposal.json"

ALLOWED_STATUSES = ("proposed", "backtested", "approved", "rejected")
ALLOWED_CATEGORIES = (
    "score_threshold",
    "news_blackout",
    "level_usage",
    "bin_size",
    "value_area",
    "entry_type",
    "session_filter",
    "sl_tp",
    "execution",
    "other",
)
ALLOWED_RISKS = ("low", "medium", "high")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fitting proposal operator CLI (Block 5c).")
    parser.add_argument("--list", action="store_true", help="List proposals (optionally filtered).")
    parser.add_argument("--status", type=str, action="append", choices=ALLOWED_STATUSES)
    parser.add_argument("--category", type=str, action="append", choices=ALLOWED_CATEGORIES)
    parser.add_argument("--risk", type=str, action="append", choices=ALLOWED_RISKS)
    parser.add_argument("--min-period", type=str, default=None, help="ISO date YYYY-MM-DD.")
    parser.add_argument("--max-period", type=str, default=None, help="ISO date YYYY-MM-DD.")
    parser.add_argument("--proposal-id", type=str, default=None)
    parser.add_argument("--validate", action="store_true", help="Run the proposal's validation backtest (no-op if no BacktestEngine wired).")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--operator", type=str, default=None)
    parser.add_argument("--note", type=str, default=None)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


def _parse_date(s: str | None) -> date | None:
    if s is None:
        return None
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d").date()


def _proposal_to_dict(p) -> dict:
    return {
        "id": str(p.id),
        "created_at": p.created_at.isoformat(),
        "period_start": p.period_start.isoformat(),
        "period_end": p.period_end.isoformat(),
        "proposal_number": p.proposal_number,
        "category": p.category,
        "observation": p.observation,
        "hypothesis": p.hypothesis,
        "validation_test": p.validation_test,
        "overfitting_risk": p.overfitting_risk,
        "overfitting_rationale": p.overfitting_rationale,
        "status": p.status,
        "backtest_result": p.backtest_result,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "decided_by": p.decided_by,
        "decision_note": p.decision_note,
        "review_id": str(p.review_id) if p.review_id else None,
        "source_period_kind": p.source_period_kind,
    }


async def _run(args: argparse.Namespace) -> int:
    setup_logging(level="INFO")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault(
        "TIMESCALEDB_URL",
        "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
    )
    journal = InMemoryJournalStore()
    fpe = FittingProposalEngine(journal=journal, backtest=None)

    report: dict = {"action": None, "result": None}

    if args.list:
        flt = FittingProposalFilter(
            status=args.status,
            category=args.category,
            overfitting_risk=args.risk,
            min_period=_parse_date(args.min_period),
            max_period=_parse_date(args.max_period),
        )
        proposals = await fpe.list_proposals(flt)
        report["action"] = "list"
        report["filter"] = flt.model_dump(mode="json")
        report["count"] = len(proposals)
        report["proposals"] = [_proposal_to_dict(p) for p in proposals]
        print(f"fitting_proposal_smoke list ok: {len(proposals)} proposal(s)")
    elif args.proposal_id is not None:
        try:
            pid = UUID(args.proposal_id)
        except ValueError:
            print(f"ERROR: invalid UUID: {args.proposal_id}", file=sys.stderr)
            return 2
        proposal = await fpe.get(pid)
        if proposal is None:
            print(f"ERROR: proposal {pid} not found", file=sys.stderr)
            return 2

        if args.validate:
            updated = await fpe.run_validation(proposal)
            proposal = updated
            report["action"] = "validate"

        if args.approve or args.reject:
            if not args.operator:
                print("ERROR: --operator NAME is required for approve/reject", file=sys.stderr)
                return 2
            if args.approve:
                proposal = await fpe.approve(pid, operator=args.operator, note=args.note)
                report["action"] = "approve"
            else:
                proposal = await fpe.reject(pid, operator=args.operator, note=args.note)
                report["action"] = "reject"

        report["proposal"] = _proposal_to_dict(proposal)
        print(
            f"fitting_proposal_smoke ok: action={report['action']}; "
            f"status={proposal.status}"
        )
    else:
        # No action — show help.
        print("ERROR: must specify --list or --proposal-id", file=sys.stderr)
        print(
            "Usage: fitting_proposal_smoke --list [--status ...]\n"
            "       fitting_proposal_smoke --proposal-id <UUID> [--validate] [--approve|--reject --operator NAME --note ...]",
            file=sys.stderr,
        )
        return 2

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info("fitting_proposal_smoke_report_written", path=str(args.report))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"fitting_proposal_smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())