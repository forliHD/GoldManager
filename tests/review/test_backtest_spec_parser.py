"""Tests for the BacktestSpec-Parser — Block 5c Phase 4."""

from __future__ import annotations

import pytest

from xauusd_bot.review.backtest_spec_parser import (
    BacktestSpec,
    parse_validation_test,
)


def test_parse_score_threshold() -> None:
    spec = parse_validation_test("score_threshold=70")
    assert spec is not None
    assert spec.score_threshold == 70
    assert spec.is_empty() is False


def test_parse_is_oos_weeks() -> None:
    spec = parse_validation_test("IS=4w OOS=1w")
    assert spec is not None
    assert spec.is_weeks == 4
    assert spec.oos_weeks == 1
    assert spec.is_days is None
    assert spec.oos_days is None


def test_parse_is_oos_days() -> None:
    spec = parse_validation_test("IS=5d OOS=2d")
    assert spec is not None
    assert spec.is_days == 5
    assert spec.oos_days == 2
    assert spec.is_weeks is None


def test_parse_session() -> None:
    spec = parse_validation_test("session=ny")
    assert spec is not None
    assert spec.session == "ny"


def test_parse_garbage_returns_empty_spec() -> None:
    spec = parse_validation_test("asdf")
    assert spec is not None
    assert spec.is_empty() is True
    assert spec.score_threshold is None
    assert spec.is_weeks is None
    assert spec.session is None


def test_parse_empty_returns_none() -> None:
    assert parse_validation_test("") is None


def test_parse_whitespace_returns_none() -> None:
    assert parse_validation_test("   ") is None


def test_parse_combined_spec() -> None:
    spec = parse_validation_test("score_threshold=75, IS=4w, OOS=1w, session=london")
    assert spec is not None
    assert spec.score_threshold == 75
    assert spec.is_weeks == 4
    assert spec.oos_weeks == 1
    assert spec.session == "london"


def test_parse_is_case_insensitive() -> None:
    spec = parse_validation_test("SCORE_THRESHOLD=70, is=4w, oos=1w")
    assert spec is not None
    assert spec.score_threshold == 70
    assert spec.is_weeks == 4
    assert spec.oos_weeks == 1


def test_parse_tolerates_whitespace() -> None:
    spec = parse_validation_test("score_threshold = 70 ,  IS = 4w")
    assert spec is not None
    assert spec.score_threshold == 70
    assert spec.is_weeks == 4


def test_parse_does_not_match_substrings() -> None:
    # "score_threshold=70" matches but "score_threshold70" (no =)
    # should not.
    spec = parse_validation_test("score_threshold70")
    assert spec is not None
    assert spec.is_empty() is True


def test_parse_raw_preserved() -> None:
    text = "score_threshold=70, IS=4w, OOS=1w"
    spec = parse_validation_test(text)
    assert spec is not None
    assert spec.raw == text


def test_backtest_spec_to_dict_roundtrip() -> None:
    spec = parse_validation_test("score_threshold=70, IS=4w, OOS=1w")
    assert spec is not None
    d = spec.to_dict()
    assert d["score_threshold"] == 70
    assert d["is_weeks"] == 4
    assert d["oos_weeks"] == 1
    # Roundtrip via dict → ensure all keys are present
    spec2 = BacktestSpec(**{k: v for k, v in d.items() if k != "raw"})
    assert spec2.score_threshold == spec.score_threshold
    assert spec2.is_weeks == spec.is_weeks


def test_backtest_spec_is_frozen() -> None:
    spec = parse_validation_test("score_threshold=70")
    assert spec is not None
    with pytest.raises((AttributeError, Exception)):  # frozen dataclass
        spec.score_threshold = 80  # type: ignore[misc]


def test_parse_is_oos_with_lower_case() -> None:
    spec = parse_validation_test("is=4w oos=1w")
    assert spec is not None
    assert spec.is_weeks == 4
    assert spec.oos_weeks == 1


def test_parse_session_with_underscore() -> None:
    spec = parse_validation_test("session=us_session")
    assert spec is not None
    assert spec.session == "us_session"