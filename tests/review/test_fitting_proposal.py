"""Tests for the FittingProposalEngine — Block 5c Phase 3.

Covers the state machine, the validation-backtest flow, the
filter, and the operator-only transitions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from xauusd_bot.backtest.engine import BacktestEngine
from xauusd_bot.common.schemas.backtest import (
    BacktestResult,
    BacktestStats,
)
from xauusd_bot.common.schemas.review import (
    FittingProposal,
    FittingProposalFilter,
    ReviewOutput,
    ReviewProposal,
    ReviewRun,
)
from xauusd_bot.journal import InMemoryJournalStore
from xauusd_bot.journal.store import (
    InvalidStatusTransitionError,
    FittingProposalNotFoundError,
)
from xauusd_bot.review.fitting_proposal import (
    FittingProposalEngine,
    IllegalStatusError,
)


def _run(coro):
    return asyncio.run(coro)


def _proposal(**overrides) -> FittingProposal:
    base = dict(
        period_start=datetime(2026, 6, 15, tzinfo=UTC),
        period_end=datetime(2026, 6, 16, tzinfo=UTC),
        proposal_number=1,
        category="score_threshold",
        observation="N=42",
        hypothesis="try threshold=70",
        validation_test="score_threshold=70, IS=4w, OOS=1w",
        overfitting_risk="low",
        overfitting_rationale="N sufficient",
    )
    base.update(overrides)
    return FittingProposal(**base)


def _review_run(proposals: list[ReviewProposal] | None = None, *, insufficient: bool = False) -> ReviewRun:
    output: ReviewOutput | None = None
    if proposals is not None:
        output = ReviewOutput(
            proposals=proposals,
            overall_assessment="ok",
            data_sufficiency="sufficient",
            summary="ok",
        )
    return ReviewRun(
        period_start=datetime(2026, 6, 15, tzinfo=UTC),
        period_end=datetime(2026, 6, 22, tzinfo=UTC),
        period_kind="weekly",
        insufficient_data=insufficient,
        trade_count=len(proposals) if proposals else 0,
        snapshot_count=0,
        discrepancy_count=0,
        output=output,
    )


def _review_proposal(num: int = 1, **overrides) -> ReviewProposal:
    base = dict(
        proposal_number=num,
        category="score_threshold",
        observation="N=42",
        hypothesis="tighten threshold",
        validation_test="score_threshold=70",
        overfitting_risk="low",
        overfitting_rationale="N sufficient",
    )
    base.update(overrides)
    return ReviewProposal(**base)


def _fake_backtest_result() -> BacktestResult:
    return BacktestResult(
        n_bars_processed=200,
        n_trades=10,
        start_date=datetime(2026, 6, 15, tzinfo=UTC),
        end_date=datetime(2026, 6, 16, tzinfo=UTC),
        runtime_seconds=1.0,
        stats=BacktestStats(
            n_trades=10,
            n_closed=10,
            n_wins=6,
            n_losses=4,
            n_breakeven=0,
            winrate=0.6,
            avg_r=0.5,
            total_r=5.0,
            profit_factor=1.5,
            expectancy=0.5,
            sharpe=1.2,
            sortino=1.5,
            max_drawdown=100.0,
            max_drawdown_duration_bars=10,
            total_pnl=250.0,
            final_equity=10250.0,
        ),
    )


# ----------------------------------------------------------------- from_review


def test_from_review_creates_proposed_for_each_proposal() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        review = _review_run(
            proposals=[_review_proposal(1), _review_proposal(2, category="news_blackout")]
        )
        proposals = await engine.from_review(review)
        assert len(proposals) == 2
        for p in proposals:
            assert p.status == "proposed"
            assert p.backtest_result is None
            assert p.review_id == review.id
            assert p.source_period_kind == "weekly"
        assert proposals[0].proposal_number == 1
        assert proposals[1].category == "news_blackout"
        # Persisted.
        all_in_journal = await engine.list_proposals()
        assert len(all_in_journal) == 2

    _run(scenario())


def test_from_review_returns_empty_when_insufficient_data() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        review = _review_run(proposals=None, insufficient=True)
        proposals = await engine.from_review(review)
        assert proposals == []

    _run(scenario())


def test_from_review_returns_empty_when_no_proposals() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        review = _review_run(proposals=[])
        proposals = await engine.from_review(review)
        assert proposals == []

    _run(scenario())


# ----------------------------------------------------------------- run_validation


def test_run_validation_with_parseable_test_runs_backtest() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        fake_engine = MagicMock(spec=BacktestEngine)
        fake_engine.run.return_value = _fake_backtest_result()
        engine = FittingProposalEngine(journal=journal, backtest=fake_engine)
        fp = _proposal(validation_test="score_threshold=70, IS=4w, OOS=1w")
        await engine.from_review(_review_run(proposals=[_review_proposal()]))
        # The from_review above created a *new* proposal — let's
        # use the actual persisted one.
        persisted = (await engine.list_proposals())[0]
        updated = await engine.run_validation(persisted)
        assert updated.status == "backtested"
        assert updated.backtest_result is not None
        assert updated.backtest_result["n_trades"] == 10
        assert updated.backtest_result["sharpe"] == 1.2
        # spec is included for audit.
        assert updated.backtest_result["spec"]["score_threshold"] == 70
        # backtest was called with the proposal's period.
        fake_engine.run.assert_called_once()
        kwargs = fake_engine.run.call_args.kwargs
        assert kwargs["max_bars"] == 200

    _run(scenario())


def test_run_validation_with_unparseable_test_keeps_proposed() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        fake_engine = MagicMock(spec=BacktestEngine)
        engine = FittingProposalEngine(journal=journal, backtest=fake_engine)
        fp = _proposal(validation_test="just words no patterns")
        updated = await engine.run_validation(fp)
        assert updated.status == "proposed"
        assert updated.backtest_result is None
        fake_engine.run.assert_not_called()

    _run(scenario())


def test_run_validation_with_whitespace_only_test_keeps_proposed() -> None:
    """Whitespace-only validation_test passes schema (min_length=1) but parser returns None."""

    async def scenario():
        journal = InMemoryJournalStore()
        fake_engine = MagicMock(spec=BacktestEngine)
        engine = FittingProposalEngine(journal=journal, backtest=fake_engine)
        fp = _proposal(validation_test="   ")
        updated = await engine.run_validation(fp)
        assert updated.status == "proposed"
        assert updated.backtest_result is None
        fake_engine.run.assert_not_called()

    _run(scenario())


def test_run_validation_without_backtest_engine_keeps_proposed() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal(validation_test="score_threshold=70, IS=4w")
        updated = await engine.run_validation(fp)
        assert updated.status == "proposed"
        assert updated.backtest_result is None

    _run(scenario())


def test_run_validation_with_backtest_exception_keeps_proposed() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        fake_engine = MagicMock(spec=BacktestEngine)
        fake_engine.run.side_effect = RuntimeError("boom")
        engine = FittingProposalEngine(journal=journal, backtest=fake_engine)
        fp = _proposal(validation_test="score_threshold=70")
        updated = await engine.run_validation(fp)
        assert updated.status == "proposed"
        assert updated.backtest_result is None

    _run(scenario())


# ----------------------------------------------------------------- approve / reject


def test_approve_proposed_sets_status_and_decision_fields() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal()
        await journal.add_fitting_proposal(fp)
        approved = await engine.approve(fp.id, operator="lucas", note="ok")
        assert approved.status == "approved"
        assert approved.decided_by == "lucas"
        assert approved.decision_note == "ok"
        assert approved.decided_at is not None

    _run(scenario())


def test_reject_proposed_sets_status_rejected() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal()
        await journal.add_fitting_proposal(fp)
        rejected = await engine.reject(fp.id, operator="lucas", note="no thanks")
        assert rejected.status == "rejected"
        assert rejected.decided_by == "lucas"

    _run(scenario())


def test_approve_backtested_is_allowed() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal(status="backtested")
        await journal.add_fitting_proposal(fp)
        approved = await engine.approve(fp.id, operator="lucas")
        assert approved.status == "approved"

    _run(scenario())


def test_approve_already_approved_is_idempotent() -> None:
    """Re-approving an already-approved proposal updates the decided_at timestamp
    (no-op on status; allowed by the state machine)."""

    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal(status="approved")
        await journal.add_fitting_proposal(fp)
        # Should NOT raise — the state machine allows approved→approved.
        result = await engine.approve(fp.id, operator="lucas", note="re-affirmed")
        assert result.status == "approved"
        assert result.decision_note == "re-affirmed"

    _run(scenario())


def test_approve_rejected_raises() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal(status="rejected")
        await journal.add_fitting_proposal(fp)
        with pytest.raises(IllegalStatusError):
            await engine.approve(fp.id, operator="lucas")

    _run(scenario())


def test_reject_already_rejected_is_idempotent() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal(status="rejected")
        await journal.add_fitting_proposal(fp)
        # approved/rejected are terminal but idempotent re-stamp is allowed.
        result = await engine.reject(fp.id, operator="lucas", note="re-rejected")
        assert result.status == "rejected"

    _run(scenario())


def test_approve_missing_id_raises_not_found() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        with pytest.raises(FittingProposalNotFoundError):
            await engine.approve(uuid4_field := _random_uuid(), operator="lucas")  # noqa: F841

    _run(scenario())


def _random_uuid():
    from uuid import uuid4
    return uuid4()


# ----------------------------------------------------------------- list / filter


def test_list_proposals_no_filter_returns_all_newest_first() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        for i in range(3):
            fp = _proposal(proposal_number=i + 1)
            await journal.add_fitting_proposal(fp)
        items = await engine.list_proposals()
        assert len(items) == 3
        # Newest first — the implementation sorts by created_at desc.
        assert items[0].created_at >= items[1].created_at

    _run(scenario())


def test_list_proposals_filter_by_status() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        await journal.add_fitting_proposal(_proposal(status="proposed"))
        await journal.add_fitting_proposal(_proposal(status="approved"))
        items = await engine.list_proposals(FittingProposalFilter(status=["approved"]))
        assert len(items) == 1
        assert items[0].status == "approved"

    _run(scenario())


def test_list_proposals_filter_by_category() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        await journal.add_fitting_proposal(_proposal(category="score_threshold"))
        await journal.add_fitting_proposal(_proposal(category="news_blackout"))
        items = await engine.list_proposals(FittingProposalFilter(category=["news_blackout"]))
        assert len(items) == 1
        assert items[0].category == "news_blackout"

    _run(scenario())


def test_list_proposals_filter_by_risk() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        await journal.add_fitting_proposal(_proposal(overfitting_risk="low"))
        await journal.add_fitting_proposal(_proposal(overfitting_risk="high"))
        items = await engine.list_proposals(FittingProposalFilter(overfitting_risk=["high"]))
        assert len(items) == 1
        assert items[0].overfitting_risk == "high"

    _run(scenario())


def test_list_proposals_filter_by_period_range() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        from datetime import date

        await journal.add_fitting_proposal(
            _proposal(period_start=datetime(2026, 6, 15, tzinfo=UTC))
        )
        await journal.add_fitting_proposal(
            _proposal(period_start=datetime(2026, 6, 20, tzinfo=UTC))
        )
        flt = FittingProposalFilter(min_period=date(2026, 6, 18), max_period=date(2026, 6, 25))
        items = await engine.list_proposals(flt)
        assert len(items) == 1
        assert items[0].period_start.day == 20


def test_approve_does_not_modify_settings() -> None:
    """I-4 Hard Rule: approve() marks the proposal as approved in the
    journal but does NOT mutate any runtime settings. Auto-apply on
    live rules is explicitly forbidden (Caveat 4i.1 + 4i.8).

    Regression-test for Probe B in Block-5c integration check.
    """
    from xauusd_bot.common.config.settings import Settings

    async def scenario():
        journal = InMemoryJournalStore()
        engine = FittingProposalEngine(journal=journal, backtest=None)
        fp = _proposal()
        await journal.add_fitting_proposal(fp)

        # Snapshot the live settings singleton (or build a fresh one).
        before = Settings().model_dump()

        approved = await engine.approve(
            fp.id, operator="lucas", note="looks good"
        )
        assert approved.status == "approved"

        after = Settings().model_dump()
        assert before == after, (
            "FittingProposalEngine.approve() must not mutate Settings — "
            "Caveat 4i.1 / 4i.8 (no auto-apply on live rules). "
            f"Diff: {set(before.items()) ^ set(after.items())}"
        )

    _run(scenario())


def test_fitting_proposal_engine_does_not_import_settings() -> None:
    """AST-based guard: fitting_proposal.py must not import the
    settings module. This is the static-equivalent of the runtime
    test above. Belt + suspenders for I-4 / Caveat 4i.1.
    """
    import ast
    from pathlib import Path

    src_path = Path("src/xauusd_bot/review/fitting_proposal.py")
    tree = ast.parse(src_path.read_text())
    forbidden = {"settings", "Settings", "common.config", "common.config.settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(f in alias.name for f in forbidden), (
                    f"fitting_proposal.py imports {alias.name} — "
                    "violates I-4 no-auto-apply (Caveat 4i.1/4i.8)"
                )
        elif isinstance(node, ast.ImportFrom):
            assert not any(f in (node.module or "") for f in forbidden), (
                f"fitting_proposal.py imports from {node.module} — "
                "violates I-4 no-auto-apply (Caveat 4i.1/4i.8)"
            )