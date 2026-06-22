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

import asyncio
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


def _comment_max_length() -> int | None:
    """Return the ``max_length`` of ``LLMDecision.comment`` (or None).

    Derived from the schema so the truncation guard in
    :meth:`OpenRouterClient.complete` never drifts from the field
    constraint.
    """

    from annotated_types import MaxLen

    for meta in LLMDecision.model_fields["comment"].metadata:
        if isinstance(meta, MaxLen):
            return meta.max_length
    return None


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


def _loads_lenient(content: str) -> Any:
    """Parse the model's JSON, tolerating markdown fences and surrounding prose.

    Some models (e.g. MiniMax M3) wrap the object in ```json … ``` or add a
    sentence around it, which broke a strict ``json.loads`` → the whole decision
    fell back to rules. Strip a leading/trailing code fence; if it still doesn't
    parse, extract the outermost ``{ … }`` span and try that.
    """

    t = content.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = (t[nl + 1:] if nl != -1 else t[3:])
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i : j + 1])
        raise


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
        usage_redis: Any = None,
    ) -> None:
        self._settings = settings
        self._prompt_path = Path(prompt_path)
        self._base_url = base_url
        self._default_timeout = float(timeout_seconds)
        # Optional Redis client for cumulative token-usage accounting.
        self._usage_redis = usage_redis
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

        # Runtime reasoning toggle (operator lever, dashboard-controlled).
        # Read best-effort from the trading Redis (same handle used for usage
        # accounting); on any error keep the static settings default so a Redis
        # blip never silently changes decision quality.
        reasoning_enabled = bool(self._settings.ai_layer_reasoning_enabled)
        if self._usage_redis is not None:
            try:
                from xauusd_bot.common.runtime_config import get_reasoning_enabled

                reasoning_enabled = await get_reasoning_enabled(
                    self._usage_redis, default=reasoning_enabled
                )
            except Exception as exc:  # noqa: BLE001 - toggle read must never break a decision
                log.debug("openrouter_reasoning_flag_read_failed", error=str(exc))

        # Build the request body. The system prompt is sent in the
        # standard "system" role; the user payload is JSON-stringified
        # into the "user" role.
        body = self._build_request_body(
            system_prompt=system_prompt or self._system_prompt,
            user_payload=user_payload,
            reasoning_enabled=reasoning_enabled,
        )
        headers = self._build_headers(api_key=api_key)
        effective_timeout = float(timeout) if timeout is not None else self._default_timeout

        # Async HTTP call via httpx.AsyncClient. The client is created
        # per-call (lightweight; no connection pooling needed at this
        # call rate) — keeps the client simple and avoids the
        # "lifecycle of a shared client" trap.
        try:
            # asyncio.wait_for enforces a HARD total wall-clock cap. The httpx
            # ``timeout`` float is only per-operation (connect/read/write), so a
            # slowly-trickling response could otherwise block the decision loop
            # far longer than the timeout (observed: a single call stalling the
            # engine ~170s, delaying every downstream decision).
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await asyncio.wait_for(
                    client.post(self._base_url, headers=headers, json=body),
                    timeout=effective_timeout,
                )
        except (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError) as exc:
            log.warning("openrouter_timeout", timeout_s=effective_timeout, error=str(exc))
            raise LLMTimeoutError(
                f"OpenRouter request timed out after {effective_timeout:.1f}s"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("openrouter_http_error", error_type=type(exc).__name__, error=str(exc))
            raise LLMCallError(
                f"OpenRouter HTTP error: {type(exc).__name__}: {exc}"
            ) from exc

        # Token-usage accounting (best-effort, before parsing; httpx caches
        # response.json() so this does not double-parse).
        if self._usage_redis is not None and 200 <= response.status_code < 300:
            try:
                from xauusd_bot.common.runtime_config import record_llm_usage

                usage = (response.json() or {}).get("usage") or {}
                await record_llm_usage(self._usage_redis, usage)
            except Exception as exc:  # noqa: BLE001 - accounting must never break a decision
                log.debug("openrouter_usage_record_failed", error=str(exc))

        # Status-code dispatch.
        parsed = self._parse_response(response)
        # Side-channel for the dashboard: publish the LLM's verbal output so the
        # AI-decision detail (rationale / confidence / entry zone) is visible.
        if self._usage_redis is not None:
            try:
                from datetime import UTC, datetime

                from xauusd_bot.common.runtime_config import set_json

                ez = getattr(parsed, "entry_zone", None)
                # Use the BAR time (broker-server time, as the chart/decision feed
                # display it) so the dashboard's AI panel isn't 3h behind the rest;
                # datetime.now() here is real UTC and would mismatch. Fall back to
                # wall-clock only if the bar ts is unavailable.
                bar_ts = (user_payload.get("features") or {}).get("ts")
                await set_json(
                    self._usage_redis,
                    "state:last_ai",
                    {
                        "ts": bar_ts or datetime.now(tz=UTC).isoformat(),
                        # Real wall-clock: used by the decision-engine freshness
                        # gate (ts is broker-time and can't measure age).
                        "written_at": datetime.now(tz=UTC).isoformat(),
                        "decision": getattr(parsed, "decision", None),
                        "entry_side": getattr(parsed, "entry_side", None),
                        "entry_type": getattr(parsed, "entry_type", None),
                        "entry_zone": (
                            {"min": getattr(ez, "price_min", None), "max": getattr(ez, "price_max", None)}
                            if ez is not None
                            else None
                        ),
                        "confidence": getattr(parsed, "confidence", None),
                        "comment": getattr(parsed, "comment", None),
                        "invalidations": list(getattr(parsed, "invalidations", None) or []),
                    },
                    ttl=1800,
                )
            except Exception as exc:  # noqa: BLE001 - dashboard side-channel is best-effort
                log.debug("openrouter_last_ai_publish_failed", error=str(exc))
        return parsed

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
        reasoning_enabled: bool = True,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible chat-completions request body.

        Forces ``stream=False`` (the engine needs a single JSON
        response) and ``response_format={"type": "json_object"}``
        (the LLM is required to emit strict JSON).

        ``reasoning_enabled=False`` sends ``reasoning: {enabled: false}``
        — the only reasoning control the MiniMax (minimax/fp8) endpoint
        actually honours (``max_tokens`` / ``effort`` are silently
        ignored). Disabling it ~halves the m3 round-trip (no
        chain-of-thought) at the cost of analytical depth. Operators
        flip this at runtime from the dashboard.
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
        if not reasoning_enabled:
            body["reasoning"] = {"enabled": False}
        # Provider routing block. Combines two concerns into OpenRouter's
        # single ``provider`` object:
        #   * ZDR (body-level flag per the official docs): restrict to ZDR
        #     endpoints and deny data-collection.
        #   * Provider pinning: force a specific upstream (e.g. MiniMax's
        #     own endpoint) so a Bring-Your-Own-Key reaches that provider
        #     instead of OpenRouter routing to a cheaper reseller.
        # data_collection=deny is privacy-preserving AND compatible with a
        # pinned provider (incl. MiniMax) — always send it.
        provider: dict[str, Any] = {"data_collection": "deny"}
        # ZDR restricts routing to ZDR-certified endpoints. MiniMax's endpoint
        # (minimax/fp8) is NOT ZDR-listed, so zdr=true + the MiniMax pin yields
        # a 404 "no endpoints". Hence ZDR is opt-in (default off) — see
        # Settings.ai_layer_zdr.
        if self._settings.ai_layer_zdr:
            provider["zdr"] = True
        order = [p.strip() for p in self._settings.openrouter_provider_order.split(",") if p.strip()]
        if order:
            provider["order"] = order
            provider["allow_fallbacks"] = self._settings.openrouter_allow_fallbacks
        body["provider"] = provider
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
            content_obj = _loads_lenient(content_str)
        except json.JSONDecodeError as exc:
            raise LLMValidationError(
                f"OpenRouter message content is not valid JSON: {content_str[:200]}"
            ) from exc

        # Verbose models (e.g. MiniMax M3) routinely blow past the
        # ``comment`` cap with multi-sentence rationale. ``comment`` is
        # advisory only — Brain vs Hands means it never drives execution —
        # so clamp it to the schema limit rather than rejecting an
        # otherwise-valid decision over free-text length. Derived from the
        # field so it never drifts from the schema.
        if isinstance(content_obj, dict) and isinstance(content_obj.get("comment"), str):
            max_comment = _comment_max_length()
            if max_comment is not None and len(content_obj["comment"]) > max_comment:
                content_obj["comment"] = content_obj["comment"][:max_comment]

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
            # asyncio.wait_for enforces a HARD total wall-clock cap. The httpx
            # ``timeout`` float is only per-operation (connect/read/write), so a
            # slowly-trickling response could otherwise block the decision loop
            # far longer than the timeout (observed: a single call stalling the
            # engine ~170s, delaying every downstream decision).
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await asyncio.wait_for(
                    client.post(self._base_url, headers=headers, json=body),
                    timeout=effective_timeout,
                )
        except (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError) as exc:
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
