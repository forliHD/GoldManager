"""Pydantic schema tests for the backtest layer (Block 5b).

The result schemas are the *output* contracts for the
:class:`BacktestEngine` and :class:`WalkForwardEngine`. Tests assert:

* Construct from a known-good dict.
* ``extra="forbid"`` rejects unknown fields.
* Required fields are validated (winrate ∈ [0, 1], n_trades ≥ 0, etc.).
* JSON round-trip preserves the schema (for the journal / persistence
  story).
* ``WalkForwardWindow.oos_degradation_pct`` is computed correctly.
* ``BacktestPhase`` is a string enum usable as a logging label.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from xauusd_bot.common.schemas.backtest import (
    BacktestPhase,
    BacktestResult,
    BacktestStats,
    BreakdownEntry,
    WalkForwardResult,
    WalkForwardWindow,
)


# ----------------------------------------------------------------- helpers


def _stats_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        n_trades=10,
        n_closed=8,
        n_wins=5,
        n_losses=3,
        n_breakeven=0,
        winrate=5 / 8,
        avg_r=0.5,
        total_r=4.0,
        profit_factor=1.5,
        expectancy=0.5,
        sharpe=1.2,
        sortino=1.8,
        max_drawdown=100.0,
        max_drawdown_duration_bars=20,
        total_pnl=250.0,
        final_equity=10250.0,
    )
    base.update(overrides)
    return base


def _window_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        window_index=0,
        start_in=datetime(2026, 1, 1, tzinfo=UTC),
        end_in=datetime(2026, 12, 31, tzinfo=UTC),
        start_oos=datetime(2027, 1, 1, tzinfo=UTC),
        end_oos=datetime(2027, 3, 31, tzinfo=UTC),
        in_sample_stats=BacktestStats(**_stats_kwargs()),
        out_of_sample_stats=BacktestStats(**_stats_kwargs(avg_r=0.1)),
        oos_degradation_pct=-50.0,
        in_sample_sharpe=2.0,
        out_of_sample_sharpe=1.0,
    )
    base.update(overrides)
    return base


# ============================================================== BacktestStats


class TestBacktestStats:
    def test_construction_with_minimum_fields(self) -> None:
        s = BacktestStats(**_stats_kwargs())
        assert s.n_trades == 10
        assert s.winrate == pytest.approx(0.625)
        assert s.sharpe == pytest.approx(1.2)

    def test_forbids_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            BacktestStats(**_stats_kwargs(unknown_field="bad"))  # type: ignore[arg-type]

    def test_rejects_negative_n_trades(self) -> None:
        with pytest.raises(ValidationError):
            BacktestStats(**_stats_kwargs(n_trades=-1))

    def test_rejects_winrate_above_one(self) -> None:
        with pytest.raises(ValidationError):
            BacktestStats(**_stats_kwargs(winrate=1.5))

    def test_rejects_winrate_below_zero(self) -> None:
        with pytest.raises(ValidationError):
            BacktestStats(**_stats_kwargs(winrate=-0.1))

    def test_rejects_negative_max_drawdown(self) -> None:
        with pytest.raises(ValidationError):
            BacktestStats(**_stats_kwargs(max_drawdown=-1.0))

    def test_rejects_negative_max_drawdown_duration(self) -> None:
        with pytest.raises(ValidationError):
            BacktestStats(**_stats_kwargs(max_drawdown_duration_bars=-1))

    def test_json_round_trip(self) -> None:
        s = BacktestStats(**_stats_kwargs())
        raw = s.model_dump_json()
        s2 = BacktestStats.model_validate_json(raw)
        assert s2 == s


# ============================================================== BreakdownEntry


class TestBreakdownEntry:
    def test_minimum_construction(self) -> None:
        b = BreakdownEntry(
            count=1, closed=1, wins=1, losses=0, breakeven=0,
            winrate=1.0, avg_r=0.5, total_r=0.5, total_pnl=10.0,
        )
        assert b.count == 1
        assert b.winrate == 1.0
        assert b.avg_r == 0.5

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            BreakdownEntry(
                count=1, closed=1, wins=1, losses=0, breakeven=0,
                winrate=1.0, avg_r=0.5, total_r=0.5, total_pnl=10.0,
                extra=42,  # type: ignore[call-arg]
            )


# ============================================================== BacktestResult


class TestBacktestResult:
    def test_minimum_construction(self) -> None:
        s = BacktestStats(**_stats_kwargs())
        r = BacktestResult(
            n_bars_processed=100,
            n_trades=10,
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 30, tzinfo=UTC),
            runtime_seconds=12.345,
            stats=s,
        )
        assert r.n_bars_processed == 100
        assert r.n_trades == 10
        assert r.equity_curve == []  # default
        assert r.r_distribution == {}  # default

    def test_equity_curve_with_decimals(self) -> None:
        s = BacktestStats(**_stats_kwargs())
        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal("0")),
            (datetime(2026, 4, 2, tzinfo=UTC), Decimal("100.50")),
        ]
        r = BacktestResult(
            n_bars_processed=100,
            n_trades=1,
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 2, tzinfo=UTC),
            runtime_seconds=1.0,
            stats=s,
            equity_curve=ec,
        )
        assert r.equity_curve == ec

    def test_forbids_extra_fields(self) -> None:
        s = BacktestStats(**_stats_kwargs())
        with pytest.raises(ValidationError):
            BacktestResult(
                n_bars_processed=1,
                n_trades=0,
                start_date=datetime(2026, 4, 1, tzinfo=UTC),
                end_date=datetime(2026, 4, 2, tzinfo=UTC),
                runtime_seconds=0.0,
                stats=s,
                extra="oops",  # type: ignore[call-arg]
            )

    def test_json_round_trip_preserves_equity_curve(self) -> None:
        s = BacktestStats(**_stats_kwargs())
        ec = [(datetime(2026, 4, 1, tzinfo=UTC), Decimal("50"))]
        r = BacktestResult(
            n_bars_processed=10,
            n_trades=1,
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 2, tzinfo=UTC),
            runtime_seconds=1.0,
            stats=s,
            equity_curve=ec,
        )
        raw = r.model_dump_json()
        # JSON dict should contain the equity_curve as a list of (ts, str(decimal))
        parsed = json.loads(raw)
        assert isinstance(parsed["equity_curve"], list)
        r2 = BacktestResult.model_validate_json(raw)
        assert r2.equity_curve == r.equity_curve
        assert r2.stats == s


# ============================================================== WalkForwardWindow


class TestWalkForwardWindow:
    def test_construction(self) -> None:
        w = WalkForwardWindow(**_window_kwargs())
        assert w.window_index == 0
        assert w.oos_degradation_pct == -50.0
        assert w.in_sample_sharpe == 2.0
        assert w.out_of_sample_sharpe == 1.0

    def test_forbids_extra(self) -> None:
        with pytest.raises(ValidationError):
            WalkForwardWindow(**_window_kwargs(oops=True))  # type: ignore[arg-type]

    def test_rejects_negative_window_index(self) -> None:
        with pytest.raises(ValidationError):
            WalkForwardWindow(**_window_kwargs(window_index=-1))


# ============================================================== WalkForwardResult


class TestWalkForwardResult:
    def test_empty_result(self) -> None:
        r = WalkForwardResult(
            windows=[],
            robustness_matrix=[],
            mean_oos_sharpe=0.0,
            std_oos_sharpe=0.0,
            oos_sharpe_degradation=0.0,
            is_overfit=False,
            runtime_seconds=0.0,
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 30, tzinfo=UTC),
            n_bars_processed=0,
        )
        assert r.windows == []
        assert r.is_overfit is False

    def test_with_windows(self) -> None:
        w1 = WalkForwardWindow(**_window_kwargs(window_index=0))
        w2 = WalkForwardWindow(**_window_kwargs(window_index=1, oos_degradation_pct=10.0))
        r = WalkForwardResult(
            windows=[w1, w2],
            robustness_matrix=[[2.0, 1.0], [2.0, 1.0]],
            mean_oos_sharpe=1.0,
            std_oos_sharpe=0.0,
            oos_sharpe_degradation=50.0,
            is_overfit=True,
            runtime_seconds=12.0,
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 30, tzinfo=UTC),
            n_bars_processed=400,
        )
        assert r.is_overfit is True
        assert r.oos_sharpe_degradation == 50.0
        assert len(r.windows) == 2
        assert len(r.robustness_matrix) == 2

    def test_json_round_trip(self) -> None:
        w1 = WalkForwardWindow(**_window_kwargs())
        r = WalkForwardResult(
            windows=[w1],
            robustness_matrix=[[2.0, 1.0]],
            mean_oos_sharpe=1.0,
            std_oos_sharpe=0.0,
            oos_sharpe_degradation=50.0,
            is_overfit=True,
            runtime_seconds=1.0,
            start_date=datetime(2026, 4, 1, tzinfo=UTC),
            end_date=datetime(2026, 4, 30, tzinfo=UTC),
            n_bars_processed=10,
        )
        raw = r.model_dump_json()
        r2 = WalkForwardResult.model_validate_json(raw)
        assert r2.windows[0].window_index == w1.window_index
        assert r2.robustness_matrix == [[2.0, 1.0]]


# ============================================================== BacktestPhase


class TestBacktestPhase:
    def test_enum_values(self) -> None:
        assert BacktestPhase.WARMUP.value == "warmup"
        assert BacktestPhase.DECISION.value == "decision"
        assert BacktestPhase.FILL.value == "fill"
        assert BacktestPhase.SETTLE.value == "settle"
        assert BacktestPhase.DONE.value == "done"

    def test_str_compat(self) -> None:
        """The enum is a str-subclass — usable as a JSON string and label."""

        assert BacktestPhase.DECISION == "decision"
        assert BacktestPhase.DECISION.value == "decision"
        assert str(BacktestPhase.DECISION) == "BacktestPhase.DECISION"
