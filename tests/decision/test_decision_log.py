"""Unit tests for ``decision_engine._decision_log``.

Focus: a raw rule decision can say ``enter_short`` with no ``block_reason``
while the qualification engine vetoes it (e.g. ``no_clear_tp_target``). The
persisted record must surface a reason in that case so the dashboard never
shows an action with an empty reason. An empty reason means "actually traded".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xauusd_bot.decision_engine import _decision_log


@dataclass
class _Stub:
    """Minimal stand-in exposing ``model_dump(mode=...)`` like a pydantic model."""

    data: dict[str, Any]

    def model_dump(self, mode: str = "python") -> dict[str, Any]:  # noqa: ARG002
        return dict(self.data)


def _decision(action: str, block_reason: str | None = None) -> _Stub:
    return _Stub(
        {
            "action": action,
            "block_reason": block_reason,
            "source_direction": "short",
            "timestamp": "2026-06-19T15:14:00Z",
        }
    )


def _score(total: float = 68.5) -> _Stub:
    return _Stub({"total_score": total, "band": "prepare_65_74", "subscores": {"h1_zone": 85.0}})


def _qual(qualified: bool, block_reasons: list[str] | None = None) -> _Stub:
    return _Stub({"qualified": qualified, "block_reasons": block_reasons or [], "final_entry_type": None})


def test_vetoed_enter_short_inherits_qualification_reason() -> None:
    rec = _decision_log(
        _decision("enter_short", block_reason=None),
        _score(),
        _qual(False, ["no_clear_tp_target"]),
        "XAUUSD+",
        ref_price=4155.0,
    )
    # Action still shows the raw intent (he *saw* a short) ...
    assert rec.action == "enter_short"
    # ... but the reason is no longer empty.
    assert rec.block_reason == "no_clear_tp_target"
    assert rec.qualified is False


def test_multiple_block_reasons_joined() -> None:
    rec = _decision_log(
        _decision("enter_short", block_reason=None),
        _score(),
        _qual(False, ["no_clear_tp_target", "volatility_out_of_range"]),
        "XAUUSD+",
        ref_price=4155.0,
    )
    assert rec.block_reason == "no_clear_tp_target, volatility_out_of_range"


def test_qualified_trade_has_no_reason() -> None:
    rec = _decision_log(
        _decision("enter_short", block_reason=None),
        _score(),
        _qual(True, []),
        "XAUUSD+",
        ref_price=4155.0,
    )
    # A genuinely executed trade keeps an empty reason.
    assert rec.action == "enter_short"
    assert rec.block_reason is None


def test_existing_raw_reason_is_preserved() -> None:
    rec = _decision_log(
        _decision("no_trade", block_reason="score_below_threshold"),
        _score(62.4),
        _qual(False, ["score_below_threshold"]),
        "XAUUSD+",
        ref_price=4155.0,
    )
    # The raw reason wins; we do not double it from the qualification list.
    assert rec.block_reason == "score_below_threshold"
