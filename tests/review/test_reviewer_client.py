"""Tests for the ReviewerOpenRouterClient — Block 5c Phase 1.

All tests mock the underlying :class:`OpenRouterClient` — no real
HTTP. The reviewer REUSES Block-6's transport, ZDR, and timeout
settings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from xauusd_bot.common.schemas.review import (
    KPISummary,
    ReviewOutput,
    ReviewProposal,
    ReviewRequest,
)
from xauusd_bot.decision.openrouter_client import (
    LLMCallError,
    LLMServerError,
    LLMValidationError,
)
from xauusd_bot.review.reviewer_client import (
    ReviewerError,
    ReviewerLLMError,
    ReviewerOpenRouterClient,
    ReviewerValidationError,
)


def _run(coro):
    """Run an async coroutine synchronously for test purposes."""

    return asyncio.run(coro)


# ----------------------------------------------------------------- helpers


def _settings_mock() -> Any:
    s = MagicMock()
    s.openrouter_api_key = "sk-or-test"
    s.openrouter_model = "minimax/minimax-m2"
    s.ai_layer_zdr = True
    return s


def _kpis() -> KPISummary:
    return KPISummary(
        n_trades=10, n_closed=10, n_wins=6, n_losses=4,
        winrate=0.6, avg_r=0.5, total_r=5.0, profit_factor=1.5,
        sharpe=1.2, sortino=1.5, max_drawdown=100.0, total_pnl=250.0,
    )


def _req() -> ReviewRequest:
    return ReviewRequest(
        period_start=datetime(2026, 6, 15, tzinfo=UTC),
        period_end=datetime(2026, 6, 16, tzinfo=UTC),
        period_kind="daily",
        kpis=_kpis(),
    )


def _good_payload() -> dict[str, Any]:
    return {
        "proposals": [
            {
                "proposal_number": 1,
                "category": "score_threshold",
                "observation": "N=42",
                "hypothesis": "try threshold=70",
                "validation_test": "score_threshold=70, IS=4w, OOS=1w",
                "overfitting_risk": "low",
                "overfitting_rationale": "N sufficient",
            }
        ],
        "overall_assessment": "ok",
        "data_sufficiency": "sufficient",
        "summary": "1 proposal emerged.",
    }


def _make_client(
    *,
    complete_raw_return: Any | None = None,
    complete_raw_side_effect: Any | None = None,
) -> ReviewerOpenRouterClient:
    base = MagicMock()
    base._load_system_prompt = MagicMock(return_value="STUB SYSTEM PROMPT")
    base.complete_raw = AsyncMock()
    if complete_raw_side_effect is not None:
        base.complete_raw.side_effect = complete_raw_side_effect
    else:
        base.complete_raw.return_value = complete_raw_return or _good_payload()
    # Also expose complete (we expect it NOT to be called).
    base.complete = AsyncMock()
    return ReviewerOpenRouterClient(base_client=base, prompt_path=Path("review_agent.md"))


# ----------------------------------------------------------------- prompt


def test_client_loads_prompt_at_init() -> None:
    base = MagicMock()
    base._load_system_prompt = MagicMock(return_value="STUB SYSTEM PROMPT")
    base.complete_raw = AsyncMock(return_value=_good_payload())
    client = ReviewerOpenRouterClient(base_client=base, prompt_path=Path("fake.md"))
    assert client.system_prompt == "STUB SYSTEM PROMPT"
    base._load_system_prompt.assert_called_once()


def test_client_caches_prompt_per_instance() -> None:
    base = MagicMock()
    base._load_system_prompt = MagicMock(return_value="STUB")
    base.complete_raw = AsyncMock(return_value=_good_payload())
    client = ReviewerOpenRouterClient(base_client=base, prompt_path=Path("fake.md"))
    # system_prompt accessed multiple times → still 1 init-time read.
    _ = client.system_prompt
    _ = client.system_prompt
    assert base._load_system_prompt.call_count == 1


# ----------------------------------------------------------------- payload building


def test_client_builds_payload_from_request() -> None:
    client = _make_client()
    payload = client._request_to_payload(_req())
    assert payload["task"] == "review"
    assert payload["period_kind"] == "daily"
    assert payload["trade_count"] == 0
    assert payload["snapshot_count"] == 0
    assert payload["discrepancy_count"] == 0
    assert "trades" in payload
    assert "kpis" in payload
    assert "instructions" in payload


def test_client_payload_omits_account_pii() -> None:
    """Caveat 4i.8 — no AccountInfo in the payload."""

    client = _make_client()
    payload = client._request_to_payload(_req())
    forbidden = ("balance", "equity", "margin", "login", "broker", "daily_pnl", "weekly_pnl")
    payload_str = str(payload).lower()
    for needle in forbidden:
        assert needle not in payload_str, f"payload contains PII: {needle}"


def test_client_payload_serializes_datetime_to_iso() -> None:
    client = _make_client()
    payload = client._request_to_payload(_req())
    # period_start / period_end must be ISO strings (Pydantic JSON mode).
    assert isinstance(payload["period_start"], str)
    assert payload["period_start"].startswith("2026-06-15")


# ----------------------------------------------------------------- happy path


def test_review_parses_well_formed_payload() -> None:
    client = _make_client()
    out = client._request_to_payload(_req())  # noqa: F841 — sanity
    result = _run(client.review(_req()))
    assert isinstance(result, ReviewOutput)
    assert result.data_sufficiency == "sufficient"
    assert len(result.proposals) == 1
    assert result.proposals[0].category == "score_threshold"


def test_review_does_not_call_block_6_complete() -> None:
    """The reviewer must use complete_raw, NOT complete (which forces LLMDecision schema)."""

    client = _make_client()
    base = client._base  # type: ignore[attr-defined]
    _run(client.review(_req()))
    assert base.complete_raw.await_count == 1
    assert base.complete.await_count == 0


# ----------------------------------------------------------------- retry / error paths


def test_review_retries_once_on_validation_error_then_succeeds() -> None:
    bad = {"foo": "bar"}  # missing required fields → ValidationError
    client = _make_client(
        complete_raw_side_effect=[bad, _good_payload()],
    )
    out = _run(client.review(_req()))
    assert isinstance(out, ReviewOutput)
    assert out.data_sufficiency == "sufficient"


def test_review_raises_validation_error_after_two_failures() -> None:
    bad = {"foo": "bar"}
    client = _make_client(complete_raw_side_effect=[bad, bad])
    with pytest.raises(ReviewerValidationError):
        _run(client.review(_req()))


def test_review_does_not_retry_on_transport_error() -> None:
    client = _make_client(
        complete_raw_side_effect=LLMServerError("server down"),
    )
    with pytest.raises(ReviewerLLMError):
        _run(client.review(_req()))
    base = client._base  # type: ignore[attr-defined]
    assert base.complete_raw.await_count == 1  # NO retry


def test_review_wraps_timeout_error() -> None:
    from xauusd_bot.decision.openrouter_client import LLMTimeoutError

    client = _make_client(complete_raw_side_effect=LLMTimeoutError("timed out"))
    with pytest.raises(ReviewerLLMError):
        _run(client.review(_req()))


def test_review_wraps_auth_error() -> None:
    from xauusd_bot.decision.openrouter_client import LLMAuthError

    client = _make_client(complete_raw_side_effect=LLMAuthError("401"))
    with pytest.raises(ReviewerLLMError):
        _run(client.review(_req()))


# ----------------------------------------------------------------- settings sharing


def test_client_uses_settings_openrouter_model_via_base() -> None:
    """The reviewer REUSES the base client's settings — no separate model config."""

    client = _make_client()
    # The reviewer does not store a model name of its own — it
    # calls base.complete_raw which uses base._settings.openrouter_model.
    assert not hasattr(client, "_settings")
    assert not hasattr(client, "_model")
    # Sanity: invoke the call once and ensure base.complete_raw is called.
    _run(client.review(_req()))
    client._base.complete_raw.assert_awaited()  # type: ignore[attr-defined]


# ----------------------------------------------------------------- import sanity (PII-free)


def test_review_does_not_import_metatrader5() -> None:
    """I-1 audit: review module never imports MetaTrader5 (code only)."""

    import ast

    import xauusd_bot.review.reviewer_client as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "MetaTrader5" not in alias.name, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "MetaTrader5"
            for alias in node.names:
                assert "MetaTrader5" not in alias.name, alias.name
        elif isinstance(node, ast.Attribute):
            assert node.attr != "MetaTrader5", "MetaTrader5 attribute access"
        elif isinstance(node, ast.Name):
            assert node.id != "MetaTrader5", "MetaTrader5 identifier"