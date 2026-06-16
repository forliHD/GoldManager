"""Pydantic schema tests for the AI Decision Layer (Block 6).

Covers the :class:`LLMDecision` / :class:`EntryZone` /
:class:`ManagementBlock` schemas in
:mod:`xauusd_bot.common.schemas.ai_decision`. These are the
*contract* between the LLM and the rest of the engine — any
regression here breaks the orchestrator and the OpenRouter
client's parsing path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xauusd_bot.common.schemas.ai_decision import (
    EntryZone,
    LLMDecision,
    ManagementBlock,
)


def _valid_payload(**overrides):
    """Return a valid :class:`LLMDecision` payload with optional overrides."""

    base = {
        "decision": "scout",
        "entry_type": "pullback",
        "entry_side": "long",
        "entry_zone": {"price_min": 2373.0, "price_max": 2375.0},
        "invalidations": ["close_below_2370"],
        "management": {"tp1_rr": 1.5, "tp2_rr": 3.0, "runner_to": "prev_week.vah"},
        "confidence": 70,
        "comment": "High-15min setup with vol expansion",
    }
    base.update(overrides)
    return base


class TestLLMDecisionHappyPath:
    def test_valid_full_payload(self):
        d = LLMDecision.model_validate(_valid_payload())
        assert d.decision == "scout"
        assert d.entry_type == "pullback"
        assert d.entry_side == "long"
        assert d.entry_zone.price_min == 2373.0
        assert d.invalidations == ["close_below_2370"]
        assert d.management.tp1_rr == 1.5
        assert d.management.tp2_rr == 3.0
        assert d.management.runner_to == "prev_week.vah"
        assert d.confidence == 70

    def test_minimal_no_trade_payload(self):
        d = LLMDecision.model_validate(
            {
                "decision": "no_trade",
                "confidence": 0,
                "comment": "",
            }
        )
        assert d.decision == "no_trade"
        assert d.entry_type is None
        assert d.entry_side is None
        assert d.entry_zone.price_min is None
        assert d.entry_zone.price_max is None
        assert d.invalidations == []
        assert d.management.tp1_rr is None
        assert d.management.tp2_rr is None
        assert d.management.runner_to is None
        assert d.management.protect_before_news_min is None

    def test_all_decision_literals(self):
        for decision in (
            "no_trade", "watch", "prepare",
            "scout", "reduced_entry", "full_entry",
        ):
            d = LLMDecision.model_validate(
                _valid_payload(decision=decision, confidence=50, entry_type=None, entry_side=None)
            )
            assert d.decision == decision

    def test_management_protect_before_news(self):
        d = LLMDecision.model_validate(
            _valid_payload(
                management={"tp1_rr": 1.0, "tp2_rr": 2.0, "runner_to": None, "protect_before_news_min": 15}
            )
        )
        assert d.management.protect_before_news_min == 15


class TestLLMDecisionRejects:
    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(
                _valid_payload(some_extra_field="i_should_not_be_here")
            )
        # Pydantic's error mentions "extra" or the field name
        assert "some_extra_field" in str(exc_info.value) or "extra" in str(exc_info.value).lower()

    def test_rejects_invalid_decision_literal(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(_valid_payload(decision="random_action"))
        assert "decision" in str(exc_info.value).lower()

    def test_rejects_invalid_entry_type_literal(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(_valid_payload(entry_type="market_order"))
        assert "entry_type" in str(exc_info.value).lower()

    def test_rejects_invalid_entry_side_literal(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(_valid_payload(entry_side="sideways"))
        assert "entry_side" in str(exc_info.value).lower()

    def test_rejects_negative_confidence(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(_valid_payload(confidence=-1))
        assert "confidence" in str(exc_info.value).lower()

    def test_rejects_confidence_above_100(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(_valid_payload(confidence=101))
        assert "confidence" in str(exc_info.value).lower()

    def test_rejects_comment_over_500_chars(self):
        long_comment = "x" * 501
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(_valid_payload(comment=long_comment))
        assert "comment" in str(exc_info.value).lower()

    def test_rejects_tp_rr_negative(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(
                _valid_payload(management={"tp1_rr": -1.0, "tp2_rr": 2.0, "runner_to": None})
            )
        assert "tp1_rr" in str(exc_info.value).lower() or "tp" in str(exc_info.value).lower()

    def test_rejects_protect_before_news_negative(self):
        with pytest.raises(ValidationError) as exc_info:
            LLMDecision.model_validate(
                _valid_payload(
                    management={"tp1_rr": 1.0, "tp2_rr": 2.0, "runner_to": None, "protect_before_news_min": -5}
                )
            )
        assert "protect_before_news_min" in str(exc_info.value).lower() or "protect" in str(exc_info.value).lower()


class TestEntryZone:
    def test_both_bounds(self):
        z = EntryZone(price_min=2370.0, price_max=2375.0)
        assert z.price_min == 2370.0
        assert z.price_max == 2375.0

    def test_both_none(self):
        z = EntryZone()
        assert z.price_min is None
        assert z.price_max is None

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            EntryZone.model_validate({"price_min": 2370.0, "extra": "x"})

    def test_rejects_inf_out_of_float_range(self):
        """``float('inf')`` is out of the representable price range.

        Pydantic v2 with ``allow_inf_nan=False`` (set on the field)
        rejects ``NaN`` / ``inf`` for float fields. This test pins
        that behaviour so a future config flip doesn't silently
        allow non-finite prices into the entry_zone (which would
        bypass the AIDecisionLayer's zone check downstream).
        """
        with pytest.raises(ValidationError):
            EntryZone(price_min=float("inf"), price_max=2375.0)
        with pytest.raises(ValidationError):
            EntryZone(price_min=2370.0, price_max=float("inf"))
        with pytest.raises(ValidationError):
            EntryZone(price_min=float("nan"), price_max=2375.0)


class TestManagementBlock:
    def test_defaults(self):
        m = ManagementBlock()
        assert m.tp1_rr is None
        assert m.tp2_rr is None
        assert m.runner_to is None
        assert m.protect_before_news_min is None

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            ManagementBlock.model_validate({"tp1_rr": 1.0, "tp3_rr": 3.0})

    def test_tp1_rr_zero_allowed(self):
        # 0 is the floor; some setups may intentionally have 0 TP1
        # (e.g. wait for runner only).
        m = ManagementBlock(tp1_rr=0.0, tp2_rr=2.0)
        assert m.tp1_rr == 0.0
