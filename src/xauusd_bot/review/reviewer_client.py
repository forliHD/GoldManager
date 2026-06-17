"""ReviewerOpenRouterClient — Block 5c Phase 1.

Thin wrapper around the Block-6 :class:`OpenRouterClient` that:

1. Loads the ``review_agent.md`` system prompt once at init time.
2. Builds a PII-free user payload from a :class:`ReviewRequest`.
3. Calls OpenRouter with a longer timeout (30s) — reviews are
   more complex than per-bar decisions.
4. Validates the output against :class:`ReviewOutput` (strict
   Pydantic).
5. On validation error: **1 retry** with an extended hint
   appended to the user payload. On second failure:
   :class:`ReviewerError`.

Design rules
------------
* **I-1:** this module never imports ``MetaTrader5``.
* **I-4 (Brain vs Hands):** the reviewer LLM is a *hypothesis
  engine* — its output is a list of FittingProposal candidates.
  No code path reads ``status == 'approved'`` and mutates live
  settings; the FittingProposalEngine state machine enforces
  human-in-the-loop (see :mod:`xauusd_bot.review.fitting_proposal`).
* **No PII:** the user payload contains only the redacted
  summaries (:class:`TradeSummary`, :class:`FeatureSnapshotLite`,
  :class:`KPISummary`, :class:`LLMFallbackDiscrepancyLite`). No
  account login, balance, equity, or broker name.
* **Reuse over rewrite:** we *reuse* the Block-6
  :class:`OpenRouterClient` exactly. The settings, ZDR, headers,
  and timeout policy live there. The reviewer only adds the
  longer timeout + the validation-retry layer.

Caveat 4i.9: timeout / 5xx / auth are NOT retried (mirrors Block-6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from xauusd_bot.common.schemas.review import ReviewOutput, ReviewRequest
from xauusd_bot.decision.openrouter_client import (
    LLMCallError,
    LLMValidationError,
    OpenRouterClient,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- errors


class ReviewerError(RuntimeError):
    """Base error for the reviewer layer.

    Subclasses are exposed so callers can decide whether to retry
    (catch :class:`ReviewerError` for "give up") or treat the
    specific subtype.
    """


class ReviewerLLMError(ReviewerError):
    """Wraps a transport / auth / server error from OpenRouter.

    Bubbles through unchanged from the base client — the caller
    gets to see the original exception via ``__cause__``.
    """


class ReviewerValidationError(ReviewerError):
    """The LLM output did not match :class:`ReviewOutput` after retry."""


# ---------------------------------------------------------------- client


class ReviewerOpenRouterClient:
    """Async LLM wrapper for daily / weekly reviews.

    Parameters
    ----------
    base_client:
        A :class:`OpenRouterClient` instance. The reviewer REUSES
        the Block-6 client — no separate HTTP / settings / ZDR
        stack.
    prompt_path:
        Filesystem path to ``review_agent.md``. The file is read
        **once at init time** and cached. Defaults to
        ``review_agent.md`` in the current working directory (same
        convention as Block 6).
    timeout_seconds:
        Per-call timeout. Defaults to 30 s — reviews are larger
        payloads and more reasoning than per-bar decisions, so the
        Block-6 default of 10 s is too tight.
    max_validation_retries:
        How many retries on Pydantic validation failure. Defaults
        to 1 (per Caveat 4i.9).
    """

    def __init__(
        self,
        base_client: OpenRouterClient,
        prompt_path: Path | str = Path("review_agent.md"),
        *,
        timeout_seconds: float = 30.0,
        max_validation_retries: int = 1,
    ) -> None:
        self._base = base_client
        self._prompt_path = Path(prompt_path)
        self._timeout = float(timeout_seconds)
        self._max_validation_retries = int(max_validation_retries)
        # Cached system prompt — extracted the same way as Block 6.
        # We re-use the base client's loader to keep behaviour
        # consistent (find `## System Prompt` → fenced block).
        self._system_prompt: str = base_client._load_system_prompt(self._prompt_path)  # noqa: SLF001

    # ============================================================ public

    @property
    def system_prompt(self) -> str:
        """The cached system prompt (read-only)."""
        return self._system_prompt

    async def review(self, request: ReviewRequest) -> ReviewOutput:
        """Call the LLM and return a validated :class:`ReviewOutput`.

        Parameters
        ----------
        request:
            The :class:`ReviewRequest` payload. Serialized as a
            JSON-safe dict (Decimal → str, datetime → ISO 8601).

        Returns
        -------
        :class:`ReviewOutput`
            A fully-validated Pydantic model.

        Raises
        ------
        :class:`ReviewerLLMError`
            Transport / auth / server error from OpenRouter.
        :class:`ReviewerValidationError`
            The LLM output failed Pydantic validation after all
            retries were exhausted.
        """

        # 1. Build the user payload. No PII — by construction
        # (ReviewRequest only carries flat summaries).
        user_payload = self._request_to_payload(request)

        # 2. Call the LLM with retry on validation error only.
        #    Transport errors (timeout / 5xx / auth) bubble up.
        last_error: Exception | None = None
        attempts = self._max_validation_retries + 1  # 1 initial + N retries
        for attempt in range(1, attempts + 1):
            try:
                content_obj = await self._base.complete_raw(
                    system_prompt=self._system_prompt,
                    user_payload=user_payload,
                    timeout=self._timeout,
                )
            except LLMCallError as exc:
                # Transport / auth / server. No retry (per Caveat 4i.9).
                log.warning(
                    "reviewer_llm_error",
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise ReviewerLLMError(
                    f"Reviewer LLM call failed: {type(exc).__name__}: {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 — defensiv
                log.warning(
                    "reviewer_unexpected_error",
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise ReviewerLLMError(
                    f"Reviewer LLM call raised unexpected error: {type(exc).__name__}: {exc}"
                ) from exc

            # 3. Validate against ReviewOutput.
            try:
                return ReviewOutput.model_validate(content_obj)
            except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError
                last_error = exc
                log.warning(
                    "reviewer_validation_error_retry",
                    attempt=attempt,
                    max_attempts=attempts,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if attempt < attempts:
                    # On retry, append a hint that names the schema
                    # fields the LLM should re-emit.
                    user_payload = dict(user_payload)
                    user_payload["_retry_hint"] = (
                        "Your previous response failed Pydantic validation against the "
                        "ReviewOutput schema. Re-emit a strict JSON object with these top-level "
                        "fields: 'proposals' (list of {proposal_number:int>=1, "
                        "category:Literal['score_threshold','news_blackout','level_usage',"
                        "'bin_size','value_area','entry_type','session_filter','sl_tp','execution',"
                        "'other'], observation:str, hypothesis:str, validation_test:str, "
                        "overfitting_risk:Literal['low','medium','high'], overfitting_rationale:str}), "
                        "'overall_assessment':str, "
                        "'data_sufficiency':Literal['sufficient','marginal','insufficient'], "
                        "'summary':str (1-3 sentences). No other fields."
                    )
                    continue
                break

        # All retries exhausted.
        log.error(
            "reviewer_validation_exhausted",
            attempts=attempts,
            last_error=str(last_error),
        )
        raise ReviewerValidationError(
            f"Reviewer LLM output failed Pydantic validation after {attempts} attempts: "
            f"{type(last_error).__name__ if last_error else 'Unknown'}: {last_error}"
        )

    # ============================================================ payload / extraction

    @staticmethod
    def _request_to_payload(request: ReviewRequest) -> dict[str, Any]:
        """Serialize a :class:`ReviewRequest` to a JSON-safe payload.

        The payload is a plain dict-of-dicts. No Decimal, no datetime
        objects — Pydantic's ``model_dump(mode='json')`` handles that.
        """

        data = request.model_dump(mode="json")
        # Top-level keys → explicit "user_payload" envelope for the LLM.
        return {
            "task": "review",
            "period_kind": data["period_kind"],
            "period_start": data["period_start"],
            "period_end": data["period_end"],
            "trade_count": len(data["trades"]),
            "snapshot_count": len(data["snapshots_sample"]),
            "discrepancy_count": len(data["discrepancies"]),
            "min_sample_size_for_proposals": data["min_sample_size_for_proposals"],
            "trades": data["trades"],
            "snapshots_sample": data["snapshots_sample"],
            "kpis": data["kpis"],
            "discrepancies": data["discrepancies"],
            "instructions": (
                "Antworte ausschließlich als JSON-Objekt passend zum ReviewOutput-Schema. "
                "Keine Markdown-Wrapper, kein Code-Fence, kein zusätzlicher Text."
            ),
        }


# ---------------------------------------------------------------- re-exports


__all__ = [
    "ReviewerError",
    "ReviewerLLMError",
    "ReviewerOpenRouterClient",
    "ReviewerValidationError",
]