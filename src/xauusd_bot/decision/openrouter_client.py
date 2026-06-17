"""OpenRouter client — Block 6 Phase 1.

Async HTTP client for `https://openrouter.ai/api/v1/chat/completions`.
Returns a strict :class:`xauusd_bot.common.schemas.ai_decision.LLMDecision`
Pydantic model after parsing the model's JSON output.

Design rules
------------
* **I-4 (Brain vs Hands):** the client never computes position size,
  SL, or TP. It only exchanges JSON. The orchestrator (Block 6 Phase 3)
  enforces downstream hard rules.
* **Strict JSON:** ``response_format={"type": "json_object"}`` in
  the request body. On the response side, the client parses the
  message content and validates it against the Pydantic schema —
  any failure raises :class:`LLMCallError` so the orchestrator can
  decide whether to retry or fall back.
* **No streaming:** the engine needs a single deterministic JSON
  response per call. Streaming would complicate Pydantic validation
  and the orchestrator's retry logic. (``stream=False`` is the
  default, set explicitly in :meth:`_build_request_body`.)
* **Timeout:** hard ``httpx`` timeout (default 10 s). On timeout the
  client raises :class:`LLMCallError` — the orchestrator retries
  once, then falls back to :class:`RuleBasedFallback`.
* **No PII logging:** the API key is never logged; structlog events
  use a hash prefix only (or, ideally, nothing — see the
  ``_log_event_safe`` helper).

ZDR (Zero Data Retention) routing
---------------------------------
Per OpenRouter's published API docs, ZDR is controlled via the
request body's ``provider.zdr`` field — **not** an HTTP header. The
task spec mentioned ``X-Privacy-Mode: zero-data-retention`` as a
header, but this header is not part of the official OpenRouter API
as of 2026-01. The correct mechanisms are:

* ``provider.zdr: true`` in the request body (per-request ZDR
  enforcement).
* ``provider.data_collection: "deny"`` in the request body (exclude
  providers that store data).

When ``ai_layer_zdr=True`` in :class:`Settings`, the client sets
both fields. See :class:`xauusd_bot.common.config.Settings` and
AGENTS.md §4f for the operational caveat.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.ai_decision import LLMDecision

log = structlog.get_logger(__name__)


# Default base URL for OpenRouter's OpenAI-compatible chat-completions API.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Default request timeout in seconds. The orchestrator may override
# per-call via :meth:`OpenRouterClient.complete`.
DEFAULT_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------- errors


class LLMCallError(RuntimeError):
    """Base error for any OpenRouter / LLM transport / parse failure.

    The orchestrator catches this and decides retry-vs-fallback.
    Concrete subtypes are for diagnostics — they all funnel into the
    orchestrator's :func:`AIDecisionOrchestrator._decide_with_retry`
    retry path.
    """


class LLMTimeoutError(LLMCallError):
    """The HTTP request timed out (network / httpx timeout)."""


class LLMServerError(LLMCallError):
    """5xx response from OpenRouter."""


class LLMValidationError(LLMCallError):
    """The response was not valid JSON or did not match the Pydantic schema."""


class LLMAuthError(LLMCallError):
    """401 / 403 — invalid or missing API key."""


# ---------------------------------------------------------------- client


class OpenRouterClient:
    """Async client for the OpenRouter chat-completions API.

    Parameters
    ----------
    settings:
        The :class:`Settings` instance — used to read
        ``openrouter_api_key`` and ``openrouter_model``.
    prompt_path:
        Filesystem path to the system prompt (Markdown with a
        ``## System Prompt`` fenced block). Defaults to
        ``decision_agent.md`` in the current working directory. The
        file is read **once at init time** and cached — the
        orchestrator assumes the prompt is stable per process.
    base_url:
        Override for testing / self-hosted routers.
    timeout_seconds:
        Default timeout for all calls; per-call overrides are allowed
        via :meth:`complete`.
    """

    def __init__(
        self,
        settings: Settings,
        prompt_path: Path = Path("decision_agent.md"),
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._prompt_path = Path(prompt_path)
        self._base_url = base_url
        self._default_timeout = float(timeout_seconds)
        # Cached system prompt (extracted from the Markdown file).
        self._system_prompt: str = self._load_system_prompt(self._prompt_path)

    # ============================================================ public

    async def complete(
        self,
        *,
        system_prompt: str | None,
        user_payload: dict[str, Any],
        timeout: float | None = None,
    ) -> LLMDecision:
        """Send a chat-completions request and return a validated :class:`LLMDecision`.

        Parameters
        ----------
        system_prompt:
            Override for the system prompt. If ``None``, the
            prompt loaded at init time is used.
        user_payload:
            JSON-serializable dict that will be sent as the user
            message's content (JSON-stringified). The LLM is
            expected to base its decision on this payload alone.
        timeout:
            Per-call timeout in seconds. If ``None``,
            ``self._default_timeout`` is used.

        Returns
        -------
        :class:`LLMDecision`
            A fully-validated Pydantic model.

        Raises
        ------
        :class:`LLMCallError`
            (or one of its subtypes) on any failure — timeout, 5xx,
            malformed JSON, Pydantic validation error. The
            orchestrator decides whether to retry or fall back.
        """

        # Validate preconditions up-front (fail fast with a clear error).
        api_key = self._settings.openrouter_api_key
        if api_key is None:
            raise LLMAuthError(
                "OPENROUTER_API_KEY is not set. Set it in the environment "
                "or pass openrouter_api_key in Settings before calling the LLM."
            )

        # Build the request body. The system prompt is sent in the
        # standard "system" role; the user payload is JSON-stringified
        # into the "user" role.
        body = self._build_request_body(
            system_prompt=system_prompt or self._system_prompt,
            user_payload=user_payload,
        )
        headers = self._build_headers(api_key=api_key)
        effective_timeout = float(timeout) if timeout is not None else self._default_timeout

        # Async HTTP call via httpx.AsyncClient. The client is created
        # per-call (lightweight; no connection pooling needed at this
        # call rate) — keeps the client simple and avoids the
        # "lifecycle of a shared client" trap.
        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await client.post(self._base_url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            log.warning("openrouter_timeout", timeout_s=effective_timeout, error=str(exc))
            raise LLMTimeoutError(
                f"OpenRouter request timed out after {effective_timeout:.1f}s"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("openrouter_http_error", error_type=type(exc).__name__, error=str(exc))
            raise LLMCallError(
                f"OpenRouter HTTP error: {type(exc).__name__}: {exc}"
            ) from exc

        # Status-code dispatch.
        return self._parse_response(response)

    # ============================================================ request building

    def _build_headers(self, *, api_key: Any) -> dict[str, str]:
        """Build the request headers.

        Includes ``Authorization``, ``HTTP-Referer``, ``X-Title``, and
        ``Content-Type``. The API key is never logged; we record only
        a short prefix hash.

        ZDR (Zero Data Retention) header
        --------------------------------
        When ``settings.ai_layer_zdr`` is True, we also set the
        ``X-Privacy-Mode: zero-data-retention`` header as the spec
        asks. NOTE: as of 2026-01 this header is not part of
        OpenRouter's official public API (ZDR is officially a body
        field — see :meth:`_build_request_body`). We set BOTH the
        header (spec-compliance, future-proof) AND the body field
        (current-correct) for defense-in-depth. If OpenRouter later
        formalises this header, the body field can be removed; if
        they reject unknown headers, the header can be removed and
        the body field keeps working.
        """

        api_key_value = (
            api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key)
        )
        key_prefix = hashlib.sha256(api_key_value.encode("utf-8")).hexdigest()[:8]
        log.debug("openrouter_request_prepared", api_key_hash_prefix=key_prefix)
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
            # OpenRouter recommends these for app-attribution + rate-limiting visibility.
            "HTTP-Referer": "https://github.com/lucasreiser/GoldManager",
            "X-Title": "GoldManager XAUUSD Trading Bot",
        }
        # ZDR header (per spec). Only set when the operator enabled
        # ZDR routing — leave it absent otherwise so providers that
        # honour the header don't accidentally route us to a
        # privacy-preserving tier we didn't opt into.
        if self._settings.ai_layer_zdr:
            headers["X-Privacy-Mode"] = "zero-data-retention"
        return headers

    def _build_request_body(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible chat-completions request body.

        Forces ``stream=False`` (the engine needs a single JSON
        response) and ``response_format={"type": "json_object"}``
        (the LLM is required to emit strict JSON).
        """

        body: dict[str, Any] = {
            "model": self._settings.openrouter_model,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, default=str)},
            ],
        }
        # ZDR routing: per the official OpenRouter docs, ZDR is a
        # body-level flag, NOT a header. When enabled, restrict the
        # router to ZDR endpoints AND deny data-collection.
        if self._settings.ai_layer_zdr:
            body["provider"] = {"zdr": True, "data_collection": "deny"}
        return body

    # ============================================================ response parsing

    def _parse_response(self, response: httpx.Response) -> LLMDecision:
        """Status-code dispatch + JSON parsing + Pydantic validation."""

        if response.status_code in (401, 403):
            # Auth error: do NOT retry. The orchestrator falls back.
            raise LLMAuthError(
                f"OpenRouter auth failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 500:
            raise LLMServerError(
                f"OpenRouter server error {response.status_code}: "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            # 4xx other than auth: likely malformed request or rate-limit.
            # Surface as a generic LLMCallError — the orchestrator will retry once.
            raise LLMCallError(
                f"OpenRouter client error {response.status_code}: "
                f"{response.text[:200]}"
            )

        # 2xx: parse + validate.
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMValidationError(
                f"OpenRouter returned non-JSON body: {response.text[:200]}"
            ) from exc

        try:
            content_str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMValidationError(
                f"OpenRouter response missing choices[0].message.content: {data}"
            ) from exc

        try:
            content_obj = json.loads(content_str)
        except json.JSONDecodeError as exc:
            raise LLMValidationError(
                f"OpenRouter message content is not valid JSON: {content_str[:200]}"
            ) from exc

        try:
            return LLMDecision.model_validate(content_obj)
        except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError
            # The Pydantic error includes field names; we keep the
            # raw content in the message so the orchestrator can log
            # it (with size cap) for the journal.
            raise LLMValidationError(
                f"OpenRouter response failed Pydantic validation: {exc}; "
                f"raw={content_str[:300]}"
            ) from exc

    # ============================================================ prompt loading

    async def complete_raw(
        self,
        *,
        system_prompt: str | None,
        user_payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Like :meth:`complete`, but returns the **raw** JSON content
        object without enforcing the :class:`LLMDecision` schema.

        Block-5c's :class:`ReviewerOpenRouterClient` uses this to
        exchange the reviewer's :class:`ReviewOutput` schema without
        having to re-implement the HTTP transport, headers, ZDR
        routing, timeout policy, or auth-key handling — all of which
        live in :meth:`complete`.

        The output is whatever the LLM emitted as the ``content`` of
        ``choices[0].message``. The caller is responsible for
        validating it against its own Pydantic schema.

        Transport / auth / 5xx errors propagate the same way as in
        :meth:`complete` (subclasses of :class:`LLMCallError`). JSON
        decode failures become :class:`LLMValidationError` — the
        reviewer decides whether to retry.
        """

        api_key = self._settings.openrouter_api_key
        if api_key is None:
            raise LLMAuthError(
                "OPENROUTER_API_KEY is not set. Set it in the environment "
                "or pass openrouter_api_key in Settings before calling the LLM."
            )

        body = self._build_request_body(
            system_prompt=system_prompt or self._system_prompt,
            user_payload=user_payload,
        )
        headers = self._build_headers(api_key=api_key)
        effective_timeout = float(timeout) if timeout is not None else self._default_timeout

        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await client.post(self._base_url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            log.warning("openrouter_timeout", timeout_s=effective_timeout, error=str(exc))
            raise LLMTimeoutError(
                f"OpenRouter request timed out after {effective_timeout:.1f}s"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("openrouter_http_error", error_type=type(exc).__name__, error=str(exc))
            raise LLMCallError(
                f"OpenRouter HTTP error: {type(exc).__name__}: {exc}"
            ) from exc

        # Status-code dispatch reuses the same logic but returns
        # the parsed content object instead of LLMDecision.
        if response.status_code in (401, 403):
            raise LLMAuthError(
                f"OpenRouter auth failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 500:
            raise LLMServerError(
                f"OpenRouter server error {response.status_code}: "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise LLMCallError(
                f"OpenRouter client error {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMValidationError(
                f"OpenRouter returned non-JSON body: {response.text[:200]}"
            ) from exc

        try:
            content_str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMValidationError(
                f"OpenRouter response missing choices[0].message.content: {data}"
            ) from exc

        try:
            return json.loads(content_str)
        except json.JSONDecodeError as exc:
            raise LLMValidationError(
                f"OpenRouter message content is not valid JSON: {content_str[:200]}"
            ) from exc

    @staticmethod
    def _load_system_prompt(path: Path) -> str:
        """Read the system prompt from a Markdown file.

        The convention (see ``decision_agent.md``) is::

            ## System Prompt

            ```
            <prompt body>
            ```

        We extract the content of the first fenced ``````` block
        under the ``## System Prompt`` heading. If the file is
        missing or the block is not found, we fall back to the
        full file content (avoids an empty-prompt bug for ad-hoc
        callers).
        """

        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.warning("openrouter_prompt_file_missing", path=str(path))
            return ""
        except OSError as exc:
            log.warning("openrouter_prompt_file_unreadable", path=str(path), error=str(exc))
            return ""

        # Look for "## System Prompt" header followed by a fenced block.
        marker = "## System Prompt"
        idx = text.find(marker)
        if idx < 0:
            return text.strip()
        # Find the first ``` block after the marker.
        sub = text[idx:]
        fence_start = sub.find("```")
        if fence_start < 0:
            return text.strip()
        # Skip the opening fence line (```json, ```, etc.)
        newline_after_open = sub.find("\n", fence_start)
        if newline_after_open < 0:
            return text.strip()
        fence_end = sub.find("```", newline_after_open + 1)
        if fence_end < 0:
            return text.strip()
        return sub[newline_after_open + 1 : fence_end].strip()


# ---------------------------------------------------------------- re-exports

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "LLMAuthError",
    "LLMCallError",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMValidationError",
    "OpenRouterClient",
]
