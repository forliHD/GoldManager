"""Tests for the ReviewEngine — Block 5c Phase 2.

Covers the daily/weekly orchestration, the insufficient-data gate,
the cross-day pattern detection, and the ReviewRun value type.
The reviewer LLM is mocked via :class:`AsyncMock`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    EntryType,
    ScoreBand,
)
from xauusd_bot.common.schemas.journal import (
    ExitReasonTag,
    FeatureSnapshotRecord,
    LLMFallbackDiscrepancyV2,
    TradeRecord,
)
from xauusd_bot.common.schemas.review import (
    KPISummary,
    ReviewOutput,
    ReviewProposal,
    ReviewRequest,
)
from xauusd_bot.journal import InMemoryJournalStore
from xauusd_bot.review.engine import (
    ReviewEngine,
    _setup_breakdown_by_day,
    _score_band_drift_by_day,
    _discrepancy_summary_by_day,
)
from xauusd_bot.review.reviewer_client import (
    ReviewerOpenRouterClient,
    ReviewerLLMError,
)


def _run(coro):
    return asyncio.run(coro)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        environment="test",
    )


def _trade(i: int = 0, ts: datetime | None = None, **overrides) -> TradeRecord:
    base = dict(
        timestamp_open=ts or datetime(2026, 6, 15, 9 + i % 6, i * 11 % 60, tzinfo=UTC),
        timestamp_close=(ts or datetime(2026, 6, 15, 9 + i % 6, i * 11 % 60, tzinfo=UTC))
        + timedelta(minutes=15),
        side="long" if i % 2 == 0 else "short",
        entry_price=Decimal("2370.00"),
        exit_price=Decimal("2375.00") if i % 2 == 0 else Decimal("2365.00"),
        stop_loss=Decimal("2365.00"),
        take_profits=[Decimal("2375"), Decimal("2380")],
        volume_lots=Decimal("0.10"),
        risk_amount=Decimal("50"),
        setup_id=uuid4(),
        score=60.0 + (i % 35),
        band=ScoreBand.PREPARE_65_74 if i % 2 == 0 else ScoreBand.OBSERVE_55_64,
        entry_type=EntryType.SCOUT,
        fill_price=Decimal("2370.05"),
        pnl_realized=Decimal("50.0") if i % 2 == 0 else Decimal("-25.0"),
        r_multiple=1.0 if i % 2 == 0 else -0.5,
        exit_reason=ExitReasonTag.TP1_HIT if i % 2 == 0 else ExitReasonTag.SL_HIT,
        session="london",
    )
    base.update(overrides)
    return TradeRecord(**base)


def _snap(i: int = 0, ts: datetime | None = None) -> FeatureSnapshotRecord:
    return FeatureSnapshotRecord(
        timestamp=ts or datetime(2026, 6, 15, 9 + i % 6, i * 11 % 60, tzinfo=UTC),
        bar_time=ts or datetime(2026, 6, 15, 9 + i % 6, i * 11 % 60, tzinfo=UTC),
        has_data=True,
        features={
            "session": "london",
            "structure_trend": "up",
            "in_blackout": False,
            "atr": 0.35,
            "score": 70.0,
            "band": "PREPARE_65_74",
            "engine_source": "rule",
        },
    )


async def _seed(journal: InMemoryJournalStore, *, n_trades: int = 12, day: date = date(2026, 6, 15)):
    for i in range(n_trades):
        ts = datetime(day.year, day.month, day.day, 9 + i % 6, (i * 11) % 60, tzinfo=UTC)
        await journal.write_trade(_trade(i, ts))
        await journal.write_feature_snapshot(_snap(i, ts))


def _make_reviewer(return_value: Any | None = None, side_effect: Any | None = None) -> ReviewerOpenRouterClient:
    base = MagicMock()
    base._load_system_prompt = MagicMock(return_value="STUB")
    base.complete_raw = AsyncMock()
    if side_effect is not None:
        base.complete_raw.side_effect = side_effect
    else:
        base.complete_raw.return_value = return_value or {
            "proposals": [
                {
                    "proposal_number": 1,
                    "category": "score_threshold",
                    "observation": "N=12",
                    "hypothesis": "tighten",
                    "validation_test": "score_threshold=70",
                    "overfitting_risk": "low",
                    "overfitting_rationale": "N=12 is borderline",
                }
            ],
            "overall_assessment": "ok",
            "data_sufficiency": "sufficient",
            "summary": "1 proposal.",
        }
    return ReviewerOpenRouterClient(base_client=base, prompt_path=Path("review_agent.md"))


# ----------------------------------------------------------------- insufficient data


def test_run_daily_below_min_sample_marks_insufficient_data() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        # Seed 5 trades — below the default daily_min=10.
        for i in range(5):
            await journal.write_trade(_trade(i))

        reviewer = _make_reviewer()
        # Spy: ensure the reviewer's HTTP call is NEVER made.
        base = reviewer._base  # type: ignore[attr-defined]
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
            daily_min_sample_size=10,
        )
        run = await engine.run_daily(date(2026, 6, 15))
        assert run.insufficient_data is True
        assert run.output is None
        assert base.complete_raw.await_count == 0

    _run(scenario())


def test_run_weekly_below_min_sample_marks_insufficient_data() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        for i in range(10):
            ts = datetime(2026, 6, 15, 9, i * 5, tzinfo=UTC)
            await journal.write_trade(_trade(i, ts))
        reviewer = _make_reviewer()
        base = reviewer._base  # type: ignore[attr-defined]
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
            weekly_min_sample_size=30,
        )
        run = await engine.run_weekly(date(2026, 6, 15))
        assert run.insufficient_data is True
        assert base.complete_raw.await_count == 0

    _run(scenario())


# ----------------------------------------------------------------- happy paths


def test_run_daily_with_enough_trades_calls_reviewer() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        await _seed(journal, n_trades=12)
        reviewer = _make_reviewer()
        base = reviewer._base  # type: ignore[attr-defined]
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_daily(date(2026, 6, 15))
        assert run.insufficient_data is False
        assert run.output is not None
        assert len(run.output.proposals) == 1
        assert base.complete_raw.await_count == 1

    _run(scenario())


def test_run_daily_serializes_request_payload() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        await _seed(journal, n_trades=12)
        reviewer = _make_reviewer()
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        await engine.run_daily(date(2026, 6, 15))
        # Inspect the user_payload that was passed to the base client.
        base = reviewer._base  # type: ignore[attr-defined]
        call = base.complete_raw.await_args
        assert call is not None
        kwargs = call.kwargs
        payload = kwargs["user_payload"]
        assert payload["task"] == "review"
        assert payload["period_kind"] == "daily"
        assert payload["trade_count"] == 12
        assert "kpis" in payload
        assert "instructions" in payload

    _run(scenario())


def test_run_weekly_seven_day_window_with_enough_trades() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        # 35 trades across a 7-day window (5 per day).
        for i in range(35):
            day_offset = i % 7
            ts = datetime(2026, 6, 15, 9 + (i % 6), (i * 11) % 60, tzinfo=UTC) + timedelta(days=day_offset)
            await journal.write_trade(_trade(i, ts))
        reviewer = _make_reviewer()
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_weekly(date(2026, 6, 15))
        assert run.insufficient_data is False
        assert run.output is not None
        # Cross-day pattern detection populated.
        assert len(run.setup_breakdown_over_days) == 5  # Mon..Fri
        assert "Mon" in run.setup_breakdown_over_days

    _run(scenario())


def test_run_weekly_cross_day_patterns_present() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        for i in range(35):
            day_offset = i % 7
            ts = datetime(2026, 6, 15, 9, (i * 5) % 60, tzinfo=UTC) + timedelta(days=day_offset)
            await journal.write_trade(_trade(i, ts))
        reviewer = _make_reviewer()
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_weekly(date(2026, 6, 15))
        # score-band drift per day
        assert "Mon" in run.score_band_drift
        # discrepancy_summary at minimum has 'total'
        assert "total" in run.discrepancy_summary

    _run(scenario())


def test_run_daily_reviewer_error_caught_and_returned() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        await _seed(journal, n_trades=12)
        reviewer = _make_reviewer(side_effect=ReviewerLLMError("LLM down"))
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_daily(date(2026, 6, 15))
        assert run.insufficient_data is False
        assert run.output is None
        assert "ReviewerLLMError" in (run.error or "")

    _run(scenario())


def test_run_daily_insufficient_data_does_not_call_reviewer() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        # 3 trades — below threshold.
        for i in range(3):
            await journal.write_trade(_trade(i))
        reviewer = _make_reviewer()
        base = reviewer._base  # type: ignore[attr-defined]
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_daily(date(2026, 6, 15))
        assert run.insufficient_data is True
        assert base.complete_raw.await_count == 0
        assert run.output is None

    _run(scenario())


def test_run_daily_returns_review_run_with_counts() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        await _seed(journal, n_trades=12)
        reviewer = _make_reviewer()
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_daily(date(2026, 6, 15))
        assert run.trade_count == 12
        assert run.snapshot_count == 12
        assert run.min_sample_size == 10
        assert run.period_kind == "daily"

    _run(scenario())


def test_review_run_serializes_to_json() -> None:
    async def scenario():
        journal = InMemoryJournalStore()
        await _seed(journal, n_trades=12)
        reviewer = _make_reviewer()
        engine = ReviewEngine(
            journal=journal,
            backtest=None,
            reviewer=reviewer,
            settings=_settings(),
        )
        run = await engine.run_daily(date(2026, 6, 15))
        d = run.model_dump(mode="json")
        assert d["period_kind"] == "daily"
        assert d["trade_count"] == 12
        assert d["insufficient_data"] is False
        assert "proposals" in d["output"]

    _run(scenario())


# ----------------------------------------------------------------- cross-day helpers (unit)


def test_setup_breakdown_by_day_groups_correctly() -> None:
    trades = [
        _trade(0, datetime(2026, 6, 15, 9, 0, tzinfo=UTC)),  # Mon
        _trade(1, datetime(2026, 6, 16, 9, 0, tzinfo=UTC)),  # Tue
        _trade(2, datetime(2026, 6, 15, 14, 0, tzinfo=UTC)),  # Mon
    ]
    out = _setup_breakdown_by_day(trades)
    assert out["Mon"]["count"] == 2
    assert out["Tue"]["count"] == 1
    assert out["Wed"]["count"] == 0


def test_score_band_drift_by_day_groups_correctly() -> None:
    trades = [
        _trade(0, datetime(2026, 6, 15, 9, 0, tzinfo=UTC)),
        _trade(1, datetime(2026, 6, 16, 9, 0, tzinfo=UTC)),
    ]
    out = _score_band_drift_by_day(trades)
    assert out["Mon"]
    assert out["Tue"]


def test_discrepancy_summary_by_day_groups_correctly() -> None:
    async def scenario():
        d1 = LLMFallbackDiscrepancyV2(
            timestamp=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
            decision_id=uuid4(),
            score=70.0,
            fallback_reason="score_below_threshold",
            rule_decision="enter_long",
            llm_decision=None,
        )
        d2 = LLMFallbackDiscrepancyV2(
            timestamp=datetime(2026, 6, 16, 9, 0, tzinfo=UTC),
            decision_id=uuid4(),
            score=70.0,
            fallback_reason="score_below_threshold",
            rule_decision="enter_long",
            llm_decision=None,
        )
        out = _discrepancy_summary_by_day([d1, d2])
        assert out["Mon"] == 1
        assert out["Tue"] == 1
        assert out["total"] == 2

    _run(scenario())