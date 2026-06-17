"""Review / FittingProposal schema tests — Block 5c."""

from __future__ import annotations

from datetime import UTC, datetime, date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
)
from xauusd_bot.common.schemas.journal import (
    LLMFallbackDiscrepancyV2,
    DiscrepancyResolutionTag,
)
from xauusd_bot.common.schemas.review import (
    FeatureSnapshotLite,
    FittingProposal,
    FittingProposalFilter,
    KPISummary,
    LLMFallbackDiscrepancyLite,
    ReviewOutput,
    ReviewProposal,
    ReviewRequest,
    ReviewRun,
    TradeSummary,
)


# ----------------------------------------------------------------- helpers


def _ts(year: int = 2026, month: int = 6, day: int = 15, hour: int = 13, minute: int = 30) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _kpis(**overrides) -> KPISummary:
    base = dict(
        n_trades=10, n_closed=10, n_wins=6, n_losses=4,
        winrate=0.6, avg_r=0.5, total_r=5.0, profit_factor=1.5,
        sharpe=1.2, sortino=1.5, max_drawdown=100.0, total_pnl=250.0,
    )
    base.update(overrides)
    return KPISummary(**base)


def _review_request(**overrides) -> ReviewRequest:
    base = dict(
        period_start=_ts(),
        period_end=_ts(day=16),
        period_kind="daily",
        kpis=_kpis(),
    )
    base.update(overrides)
    return ReviewRequest(**base)


def _review_proposal(**overrides) -> ReviewProposal:
    base = dict(
        proposal_number=1,
        category="score_threshold",
        observation="N=42 trades, winrate=0.55 vs 0.62 for band 65+",
        hypothesis="Increase score_threshold from 65 to 70.",
        validation_test="score_threshold=70, IS=4w, OOS=1w",
        overfitting_risk="low",
        overfitting_rationale="N=42 is above the 30-trend floor and the signal is consistent across sessions.",
    )
    base.update(overrides)
    return ReviewProposal(**base)


# ----------------------------------------------------------------- ReviewRequest


def test_review_request_happy_path() -> None:
    req = _review_request()
    assert req.period_kind == "daily"
    assert req.min_sample_size_for_proposals == 30  # default
    assert req.kpis.n_trades == 10
    assert req.trades == []
    assert req.discrepancies == []


def test_review_request_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        _review_request(period_start=datetime(2026, 6, 15))  # noqa: DTZ001


def test_review_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _review_request(unknown_field="x")


def test_review_request_min_sample_size_default_is_30() -> None:
    req = _review_request()
    assert req.min_sample_size_for_proposals == 30


def test_review_request_period_kind_must_be_literal() -> None:
    with pytest.raises(ValidationError):
        _review_request(period_kind="monthly")  # type: ignore[arg-type]


# ----------------------------------------------------------------- ReviewProposal / ReviewOutput


def test_review_proposal_happy_path() -> None:
    p = _review_proposal()
    assert p.proposal_number == 1
    assert p.category == "score_threshold"


def test_review_proposal_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _review_proposal(unexpected="x")


def test_review_proposal_category_must_be_literal() -> None:
    with pytest.raises(ValidationError):
        _review_proposal(category="make_money")  # type: ignore[arg-type]


def test_review_proposal_observation_required() -> None:
    with pytest.raises(ValidationError):
        _review_proposal(observation="")


def test_review_proposal_overfitting_risk_must_be_literal() -> None:
    with pytest.raises(ValidationError):
        _review_proposal(overfitting_risk="extreme")  # type: ignore[arg-type]


def test_review_output_data_sufficiency_must_be_literal() -> None:
    with pytest.raises(ValidationError):
        ReviewOutput(
            overall_assessment="ok",
            data_sufficiency="kinda",  # type: ignore[arg-type]
            summary="ok",
        )


def test_review_output_with_proposals() -> None:
    out = ReviewOutput(
        proposals=[_review_proposal(proposal_number=1), _review_proposal(proposal_number=2)],
        overall_assessment="Sufficient.",
        data_sufficiency="sufficient",
        summary="3 proposals emerged.",
    )
    assert len(out.proposals) == 2
    assert out.proposals[1].proposal_number == 2


# ----------------------------------------------------------------- FittingProposal


def _fitting_proposal(**overrides) -> FittingProposal:
    base = dict(
        period_start=_ts(),
        period_end=_ts(day=16),
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


def test_fitting_proposal_default_status_is_proposed() -> None:
    p = _fitting_proposal()
    assert p.status == "proposed"
    assert p.backtest_result is None
    assert p.decided_at is None
    assert p.decided_by is None


def test_fitting_proposal_status_transitions_to_backtested() -> None:
    p = _fitting_proposal()
    assert p.status == "proposed"
    p2 = p.model_copy(update={"status": "backtested", "backtest_result": {"n_trades": 10}})
    assert p2.status == "backtested"
    assert p2.backtest_result is not None


def test_fitting_proposal_status_transitions_to_approved_with_decision_fields() -> None:
    p = _fitting_proposal()
    p2 = p.model_copy(
        update={
            "status": "approved",
            "decided_at": datetime.now(tz=UTC),
            "decided_by": "operator",
            "decision_note": "looks good",
        }
    )
    assert p2.status == "approved"
    assert p2.decided_by == "operator"
    assert p2.decision_note == "looks good"


def test_fitting_proposal_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        _fitting_proposal(auto_apply=True)


def test_fitting_proposal_id_is_uuid() -> None:
    p = _fitting_proposal()
    assert isinstance(p.id, type(uuid4()))


def test_fitting_proposal_review_id_optional() -> None:
    p = _fitting_proposal(review_id=uuid4())
    assert p.review_id is not None
    p2 = _fitting_proposal()
    assert p2.review_id is None


# ----------------------------------------------------------------- FittingProposalFilter


def test_filter_empty_means_no_constraint() -> None:
    flt = FittingProposalFilter()
    assert flt.status is None
    assert flt.category is None
    assert flt.overfitting_risk is None
    assert flt.min_period is None
    assert flt.max_period is None


def test_filter_with_status_and_category() -> None:
    flt = FittingProposalFilter(
        status=["proposed"],
        category=["score_threshold"],
        overfitting_risk=["low"],
    )
    assert flt.status == ["proposed"]
    assert flt.category == ["score_threshold"]
    assert flt.overfitting_risk == ["low"]


def test_filter_with_min_max_period() -> None:
    flt = FittingProposalFilter(
        min_period=date(2026, 6, 1),
        max_period=date(2026, 6, 30),
    )
    assert flt.min_period == date(2026, 6, 1)
    assert flt.max_period == date(2026, 6, 30)


def test_filter_status_must_be_literal() -> None:
    with pytest.raises(ValidationError):
        FittingProposalFilter(status=["done"])  # type: ignore[list-item]


def test_filter_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        FittingProposalFilter(unknown="x")


# ----------------------------------------------------------------- TradeSummary / KPISummary / Lite / DiscrepancyLite


def test_trade_summary_happy_path() -> None:
    s = TradeSummary(
        timestamp_open=_ts(),
        side="long",
        score=75.0,
        band=ScoreBand.REDUCED_75_84,
        entry_type=EntryType.REDUCED,
    )
    assert s.symbol == "XAUUSD"  # default
    assert s.structure_at_entry == "range"  # default


def test_trade_summary_score_bounded() -> None:
    with pytest.raises(ValidationError):
        TradeSummary(
            timestamp_open=_ts(),
            side="long",
            score=120.0,
            band=ScoreBand.FULL_85_PLUS,
            entry_type=EntryType.FULL,
        )


def test_feature_snapshot_lite_happy() -> None:
    s = FeatureSnapshotLite(bar_time=_ts(), score=70.0, band=ScoreBand.PREPARE_65_74)
    assert s.atr is None
    assert s.in_blackout is None


def test_feature_snapshot_lite_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        FeatureSnapshotLite(bar_time=_ts(), extra="x")


def test_kpis_setup_breakdown_dict() -> None:
    k = _kpis(setup_breakdown={"scout": {"count": 5, "winrate": 0.6}})
    assert k.setup_breakdown["scout"]["count"] == 5


def test_discrepancy_lite_happy() -> None:
    d = LLMFallbackDiscrepancyLite(
        timestamp=_ts(),
        decision_id=uuid4(),
        score=75.0,
        rule_decision="enter_long",
    )
    assert d.llm_decision is None
    assert d.fallback_reason is None


def test_review_run_default_insufficient_data_false() -> None:
    run = ReviewRun(
        period_start=_ts(),
        period_end=_ts(day=16),
        period_kind="daily",
        trade_count=0,
        snapshot_count=0,
        discrepancy_count=0,
    )
    assert run.insufficient_data is False
    assert run.output is None
    assert run.error is None