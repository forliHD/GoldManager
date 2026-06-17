"""FittingProposalEngine — Block 5c Phase 3.

Manages the :class:`FittingProposal` lifecycle:

    proposed ──► backtested ──► approved   (operator signal)
        │           │
        └───────────┴────────► rejected    (operator signal)

The transitions are explicit. **Only the operator** (via CLI /
Block-9 dashboard) drives them — the engine itself never
auto-applies ``approved`` proposals to live settings. Per
``review_agent.md`` Zeile 45:

    > Statuswechsel nur durch dich (Human), nie automatisch.

What this engine does
---------------------
* :meth:`from_review` — convert every :class:`ReviewProposal` in a
  :class:`ReviewRun` into a persisted :class:`FittingProposal`
  (status='proposed').
* :meth:`run_validation` — parse the proposal's
  ``validation_test`` via :func:`parse_validation_test` and run a
  backtest when the spec is parseable. Updates ``status='backtested'``
  + ``backtest_result``.
* :meth:`approve` / :meth:`reject` — operator-only transitions.
* :meth:`list_proposals` — read with optional filter.

Invariant enforcement
--------------------
* **I-4:** the engine NEVER reads ``status == 'approved'`` and
  mutates live settings. The ``approved`` state is a *signal* to
  the operator; the operator (or future Block-9 dashboard) is
  responsible for translating it into a code change.
* **No auto-apply:** even an ``approved`` proposal is just a row
  in the journal. There is no :meth:`apply` method (see Caveat 4i.8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog

from xauusd_bot.backtest.engine import BacktestEngine
from xauusd_bot.common.schemas.review import (
    FittingProposal,
    FittingProposalFilter,
    ReviewProposal,
    ReviewRun,
)
from xauusd_bot.journal.store import (
    FittingProposalNotFoundError,
    InvalidStatusTransitionError,
    JournalStore,
)
from xauusd_bot.review.backtest_spec_parser import (
    BacktestSpec,
    parse_validation_test,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- errors


class FittingProposalError(RuntimeError):
    """Base error for the engine."""


class IllegalStatusError(FittingProposalError):
    """The requested status transition is not in the state machine."""


# ---------------------------------------------------------------- engine


class FittingProposalEngine:
    """The fitting-proposal state-machine manager.

    Parameters
    ----------
    journal:
        :class:`JournalStore` (or any object that satisfies the
        ``add_fitting_proposal`` / ``update_fitting_proposal`` /
        ``get_fitting_proposal`` / ``list_fitting_proposals``
        surface). The engine calls :meth:`update_fitting_proposal`
        for status changes — the store enforces the transition
        rules in :data:`xauusd_bot.journal.store._FITTING_PROPOSAL_VALID_TRANSITIONS`
        and raises :class:`InvalidStatusTransitionError` on illegal
        transitions. The engine catches those and re-raises as
        :class:`IllegalStatusError`.
    backtest:
        Optional :class:`BacktestEngine`. When provided,
        :meth:`run_validation` parses the proposal's
        ``validation_test`` and runs the backtest. When ``None``,
        validation is always skipped (the proposal stays
        ``proposed``).
    """

    def __init__(
        self,
        *,
        journal: JournalStore,
        backtest: BacktestEngine | None = None,
    ) -> None:
        self._journal = journal
        self._backtest = backtest

    # ============================================================ from_review

    async def from_review(
        self,
        review: ReviewRun,
    ) -> list[FittingProposal]:
        """Materialise each :class:`ReviewProposal` in ``review`` as a
        persisted :class:`FittingProposal` (status='proposed').

        Returns the list of created proposals (also persisted).

        Edge cases
        ----------
        * ``review.output is None`` → return ``[]`` (insufficient_data).
        * ``review.output.proposals == []`` → return ``[]``.
        """

        if review.output is None:
            log.info(
                "fitting_proposal_from_review_skipped",
                reason="insufficient_data",
                review_id=str(review.id),
            )
            return []
        if not review.output.proposals:
            log.info(
                "fitting_proposal_from_review_empty",
                review_id=str(review.id),
            )
            return []

        created: list[FittingProposal] = []
        for prop in review.output.proposals:
            fp = self._build_proposal(review=review, proposal=prop)
            new_id = await self._journal.add_fitting_proposal(fp)
            # The store assigns the same id (we set it from
            # ``uuid4()`` in the builder) — keep our reference.
            assert new_id == fp.id
            created.append(fp)
            log.info(
                "fitting_proposal_created",
                proposal_id=str(fp.id),
                review_id=str(review.id),
                proposal_number=fp.proposal_number,
                category=fp.category,
            )
        return created

    @staticmethod
    def _build_proposal(
        *,
        review: ReviewRun,
        proposal: ReviewProposal,
    ) -> FittingProposal:
        """Build a :class:`FittingProposal` from a review + proposal."""

        return FittingProposal(
            period_start=review.period_start,
            period_end=review.period_end,
            proposal_number=proposal.proposal_number,
            category=proposal.category,
            observation=proposal.observation,
            hypothesis=proposal.hypothesis,
            validation_test=proposal.validation_test,
            overfitting_risk=proposal.overfitting_risk,
            overfitting_rationale=proposal.overfitting_rationale,
            status="proposed",
            backtest_result=None,
            decided_at=None,
            decided_by=None,
            decision_note=None,
            review_id=review.id,
            source_period_kind=review.period_kind,
        )

    # ============================================================ run_validation

    async def run_validation(
        self,
        proposal: FittingProposal,
        *,
        spec_override: BacktestSpec | None = None,
    ) -> FittingProposal:
        """Validate ``proposal`` by running a backtest.

        Behaviour
        ---------
        1. Resolve the spec via :func:`parse_validation_test`
           (unless ``spec_override`` is provided).
        2. If the spec is empty AND ``spec_override`` is None →
           return the proposal unchanged (``status='proposed'``,
           ``backtest_result=None``).
        3. If a backtest engine is wired in → run it. The
           ``BacktestEngine.run`` signature in Block 5b requires a
           start_date + end_date. We derive a window from
           ``proposal.period_start`` / ``proposal.period_end`` —
           for now we run on a short synthetic window around the
           proposal's period. If the engine is ``None`` → keep
           proposal as 'proposed'.
        4. On success → ``status='backtested'`` + populated
           ``backtest_result``. On error → keep 'proposed' +
           log a warning (operator can re-run).

        Returns
        -------
        :class:`FittingProposal`
            The updated proposal (also persisted via
            :meth:`JournalStore.update_fitting_proposal`).
        """

        spec = spec_override if spec_override is not None else parse_validation_test(
            proposal.validation_test
        )
        if spec is None or spec.is_empty():
            log.info(
                "fitting_proposal_validation_skipped",
                proposal_id=str(proposal.id),
                reason="empty_or_unparseable_spec",
            )
            return proposal

        if self._backtest is None:
            log.info(
                "fitting_proposal_validation_skipped",
                proposal_id=str(proposal.id),
                reason="no_backtest_engine",
            )
            return proposal

        # Run the validation backtest. We re-use the proposal's
        # period as the backtest window — Block 5c is honest that
        # the operator may want to widen this via the dashboard in
        # Block 9 (see Caveat 4i.5).
        try:
            result = self._backtest.run(
                start_date=proposal.period_start,
                end_date=proposal.period_end,
                warmup_bars=200,
                max_bars=200,
            )
            backtest_result: dict[str, Any] = {
                "n_trades": result.stats.n_trades,
                "winrate": result.stats.winrate,
                "avg_r": result.stats.avg_r,
                "sharpe": result.stats.sharpe,
                "sortino": result.stats.sortino,
                "max_drawdown": result.stats.max_drawdown,
                "profit_factor": result.stats.profit_factor,
                "total_r": result.stats.total_r,
                "total_pnl": result.stats.total_pnl,
                "final_equity": result.stats.final_equity,
                "spec": spec.to_dict(),
            }
            new_proposal = proposal.model_copy(
                update={
                    "status": "backtested",
                    "backtest_result": backtest_result,
                }
            )
            await self._journal.update_fitting_proposal(new_proposal)
            log.info(
                "fitting_proposal_backtested",
                proposal_id=str(proposal.id),
                n_trades=backtest_result["n_trades"],
                sharpe=backtest_result["sharpe"],
            )
            return new_proposal
        except InvalidStatusTransitionError as exc:
            log.error(
                "fitting_proposal_validation_failed_transition",
                proposal_id=str(proposal.id),
                error=str(exc),
            )
            raise IllegalStatusError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — defensiv
            log.warning(
                "fitting_proposal_validation_failed",
                proposal_id=str(proposal.id),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return proposal

    # ============================================================ approve / reject

    async def approve(
        self,
        proposal_id: UUID,
        *,
        operator: str,
        note: str | None = None,
    ) -> FittingProposal:
        """Operator-only: mark ``proposal_id`` as approved.

        Allowed transitions:
            proposed → approved
            backtested → approved
        Raises :class:`IllegalStatusError` for other transitions.
        """

        return await self._decide(
            proposal_id=proposal_id,
            new_status="approved",
            operator=operator,
            note=note,
        )

    async def reject(
        self,
        proposal_id: UUID,
        *,
        operator: str,
        note: str | None = None,
    ) -> FittingProposal:
        """Operator-only: mark ``proposal_id`` as rejected.

        Allowed transitions:
            proposed → rejected
            backtested → rejected
        Raises :class:`IllegalStatusError` for other transitions.
        """

        return await self._decide(
            proposal_id=proposal_id,
            new_status="rejected",
            operator=operator,
            note=note,
        )

    async def _decide(
        self,
        *,
        proposal_id: UUID,
        new_status: str,
        operator: str,
        note: str | None,
    ) -> FittingProposal:
        existing = await self._journal.get_fitting_proposal(proposal_id)
        if existing is None:
            raise FittingProposalNotFoundError(
                f"fitting_proposal {proposal_id} not found"
            )
        updated = existing.model_copy(
            update={
                "status": new_status,
                "decided_at": datetime.now(tz=UTC),
                "decided_by": operator,
                "decision_note": note,
            }
        )
        try:
            await self._journal.update_fitting_proposal(updated)
        except InvalidStatusTransitionError as exc:
            raise IllegalStatusError(str(exc)) from exc
        log.info(
            "fitting_proposal_decided",
            proposal_id=str(proposal_id),
            new_status=new_status,
            operator=operator,
        )
        return updated

    # ============================================================ list

    async def list_proposals(
        self,
        filter: FittingProposalFilter | None = None,
    ) -> list[FittingProposal]:
        """Return all proposals matching ``filter`` (newest first)."""

        return await self._journal.list_fitting_proposals(filter=filter)

    async def get(self, proposal_id: UUID) -> FittingProposal | None:
        """Fetch a single proposal by id."""

        return await self._journal.get_fitting_proposal(proposal_id)


# ---------------------------------------------------------------- re-exports


__all__ = [
    "FittingProposalEngine",
    "FittingProposalError",
    "IllegalStatusError",
]