"""Review / Fitting layer — Block 5c (Daily + Weekly review + FittingProposal state machine).

Public API
----------
* :class:`ReviewerOpenRouterClient` — :mod:`xauusd_bot.review.reviewer_client`
* :class:`ReviewEngine` — :mod:`xauusd_bot.review.engine`
* :class:`FittingProposalEngine` — :mod:`xauusd_bot.review.fitting_proposal`
* :func:`parse_validation_test` — :mod:`xauusd_bot.review.backtest_spec_parser`
* :class:`BacktestSpec` — :mod:`xauusd_bot.review.backtest_spec_parser`

Pydantic schemas live in :mod:`xauusd_bot.common.schemas.review`.

I-3 (PIT): this module never reads bars / connector state. The
ReviewEngine consumes the journal's append-only record stream
(trades / snapshots / discrepancies) and never enriches it with
forward-looking data.

I-4 (Brain vs Hands): the reviewer LLM produces *hypotheses* (a
:class:`FittingProposal` list). No code path reads
``status == 'approved'`` and mutates live settings — the operator
(via CLI / future Block-9 dashboard) is the only one that decides
which proposals become real changes.
"""

from xauusd_bot.review.backtest_spec_parser import (
    BacktestSpec,
    parse_validation_test,
)
from xauusd_bot.review.engine import ReviewEngine
from xauusd_bot.review.fitting_proposal import (
    FittingProposalEngine,
    FittingProposalError,
    IllegalStatusError,
)
from xauusd_bot.review.reviewer_client import (
    ReviewerError,
    ReviewerLLMError,
    ReviewerOpenRouterClient,
    ReviewerValidationError,
)

__all__ = [
    "BacktestSpec",
    "FittingProposalEngine",
    "FittingProposalError",
    "IllegalStatusError",
    "ReviewEngine",
    "ReviewerError",
    "ReviewerLLMError",
    "ReviewerOpenRouterClient",
    "ReviewerValidationError",
    "parse_validation_test",
]