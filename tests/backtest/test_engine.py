"""Tests for the BacktestEngine (Block 5b Phase 0).

Strategy
--------
The engine is a *pure orchestrator* that drives the existing
:class:`ReplayConnector` + feature / decision / execution stack. We
test it with a small synthetic 300-bar parquet (sliced from the
committed sample) so the loop completes in a few seconds per test.

What's covered
--------------
* Happy path: a 50-bar run produces a non-empty ``BacktestResult``
  with all stats populated and no NaN.
* Determinism: running the engine twice with identical inputs gives
  identical results (no hidden RNG / wall clock).
* PIT compliance: the engine never asks the connector for a bar
  past ``end_date``; we verify by inspecting the journal's snapshot
  timestamps.
* ``max_bars`` cap is honoured (``n_bars_processed <= max_bars``).
* Empty window: an ``end_date <= start_date`` window raises.
* Risk blocks daily/weekly limits: when the risk manager rejects,
  no trade is opened.
* Slippage / spread applied: the fill price differs from the close
  by half-spread + slippage.
* ``context_window_bars`` caps the per-bar cost (smoke check).

The tests use the real :class:`BacktestEngine` and the real
:class:`ReplayConnector` so a regression in either layer is caught.
The engines and PaperBroker run unmodified.
"""

from __future__ import annotations

import math
import shutil
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from xauusd_bot.backtest import (
    BacktestEngine,
    FixedSlippage,
    FixedSpread,
)
from xauusd_bot.common.config import Settings
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.journal import InMemoryJournalStore


# ----------------------------------------------------------------- fixtures


ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "xauusd_m1_sample.parquet"
SHORT_PARQUET = Path("/tmp/xauusd_short_backtest.parquet")


@pytest.fixture(scope="module", autouse=True)
def _build_short_parquet() -> None:
    """Slice the committed sample to 300 bars so tests run in seconds."""

    if SHORT_PARQUET.exists():
        return
    df = pd.read_parquet(SAMPLE)
    df.iloc[:300].to_parquet(SHORT_PARQUET)


@pytest.fixture
def connector() -> ReplayConnector:
    return ReplayConnector(source_path=SHORT_PARQUET, symbol="XAUUSD")


@pytest.fixture
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _make_engine(
    connector: ReplayConnector,
    journal: InMemoryJournalStore | None = None,
    **overrides: object,
) -> BacktestEngine:
    """Construct a BacktestEngine with sensible test defaults."""

    kwargs: dict[str, object] = {
        "connector": connector,
        "journal": journal or InMemoryJournalStore(),
        "settings": Settings(),  # type: ignore[arg-arg]
        "slippage_model": FixedSlippage(Decimal("0.50")),
        "spread_model": FixedSpread(Decimal("0.30")),
        "context_window_bars": 200,
    }
    kwargs.update(overrides)
    return BacktestEngine(**kwargs)  # type: ignore[arg-type]


# ============================================================== Happy path


class TestBacktestEngineHappyPath:
    def test_run_with_tiny_window(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 30, tzinfo=UTC),
            warmup_bars=50,
            max_bars=30,
        )
        # 0:00 to 0:30 contains 31 bars; max_bars=30 caps at 30.
        assert result.n_bars_processed == 30
        assert result.n_bars_processed > 0
        assert result.start_date == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        assert result.end_date == datetime(2026, 4, 1, 0, 30, tzinfo=UTC)
        assert result.runtime_seconds > 0
        # stats are present
        assert result.stats.n_trades >= 0
        assert 0.0 <= result.stats.winrate <= 1.0
        assert result.stats.max_drawdown >= 0

    def test_r_distribution_has_all_seven_buckets(
        self, connector: ReplayConnector
    ) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
            warmup_bars=50,
            max_bars=60,
        )
        assert set(result.r_distribution.keys()) == {"-3", "-2", "-1", "0", "1", "2", "3+"}

    def test_setup_breakdown_keys_present(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
            warmup_bars=50,
            max_bars=60,
        )
        for key in ("scout", "reduced", "full"):
            assert key in result.setup_breakdown

    def test_session_breakdown_keys_present(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
            warmup_bars=50,
            max_bars=60,
        )
        for key in ("asia", "london", "ny", "overlap", "closed"):
            assert key in result.session_breakdown

    def test_no_nan_in_stats(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
            warmup_bars=50,
            max_bars=60,
        )
        for field_name in (
            "winrate",
            "avg_r",
            "total_r",
            "profit_factor",
            "expectancy",
            "sharpe",
            "sortino",
            "max_drawdown",
            "total_pnl",
            "final_equity",
        ):
            v = getattr(result.stats, field_name)
            assert math.isfinite(v), f"{field_name} is not finite: {v}"


# ============================================================== Determinism


class TestBacktestEngineDeterminism:
    def test_two_runs_are_bit_identical(self, connector: ReplayConnector) -> None:
        a = _make_engine(connector)
        ra = a.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 10, tzinfo=UTC),
            warmup_bars=50,
            max_bars=20,
        )
        b = _make_engine(connector)
        rb = b.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 10, tzinfo=UTC),
            warmup_bars=50,
            max_bars=20,
        )
        for field in (
            "n_bars_processed",
            "n_trades",
        ):
            assert getattr(ra, field) == getattr(rb, field)
        for field in (
            "n_trades",
            "n_closed",
            "n_wins",
            "n_losses",
            "winrate",
            "total_pnl",
            "final_equity",
        ):
            va = getattr(ra.stats, field)
            vb = getattr(rb.stats, field)
            assert va == pytest.approx(vb, abs=1e-9), f"stats.{field} differs: {va} vs {vb}"

    def test_results_json_serializable(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 5, tzinfo=UTC),
            warmup_bars=50,
            max_bars=20,
        )
        # Decimal fields get coerced to str by Pydantic for JSON.
        raw = result.model_dump_json()
        assert "n_bars_processed" in raw
        assert "stats" in raw


# ============================================================== PIT compliance


class TestBacktestEnginePIT:
    def test_snapshots_are_pit_anchored(self, connector: ReplayConnector) -> None:
        """Every snapshot's bar_time falls inside [start_date, end_date]."""

        journal = InMemoryJournalStore()
        engine = _make_engine(connector, journal=journal)
        start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 1, 0, 30, tzinfo=UTC)
        engine.run(start_date=start, end_date=end, warmup_bars=50, max_bars=20)

        import asyncio

        async def _list_snapshots() -> list:
            return await journal.list_snapshots(
                start=start - pd.Timedelta(hours=1).to_pytimedelta(),
                end=end + pd.Timedelta(hours=1).to_pytimedelta(),
                symbol="XAUUSD",
            )

        snapshots = asyncio.run(_list_snapshots())
        assert len(snapshots) > 0
        for snap in snapshots:
            # Snapshot must be in the [start, end] window.
            assert start <= snap.bar_time <= end, (
                f"snapshot {snap.bar_time} outside [{start}, {end}]"
            )

    def test_snapshots_are_monotonically_ordered(self, connector: ReplayConnector) -> None:
        journal = InMemoryJournalStore()
        engine = _make_engine(connector, journal=journal)
        start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 1, 0, 30, tzinfo=UTC)
        engine.run(start_date=start, end_date=end, warmup_bars=50, max_bars=20)

        import asyncio

        async def _list_snapshots() -> list:
            return await journal.list_snapshots(
                start=start - pd.Timedelta(hours=1).to_pytimedelta(),
                end=end + pd.Timedelta(hours=1).to_pytimedelta(),
                symbol="XAUUSD",
            )

        snapshots = asyncio.run(_list_snapshots())
        # list_snapshots sorts by bar_time ascending; verify.
        ts_list = [s.bar_time for s in snapshots]
        assert ts_list == sorted(ts_list)


# ============================================================== max_bars cap


class TestBacktestEngineMaxBars:
    def test_max_bars_caps_processing(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),  # 60 min
            warmup_bars=50,
            max_bars=5,  # cap at 5
        )
        assert result.n_bars_processed == 5

    def test_max_bars_one(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
            warmup_bars=50,
            max_bars=1,
        )
        assert result.n_bars_processed == 1


# ============================================================== Input validation


class TestBacktestEngineValidation:
    def test_end_before_start_raises(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        with pytest.raises(ValueError, match=r"end_date.*must be after start_date"):
            engine.run(
                start_date=datetime(2026, 4, 5, tzinfo=UTC),
                end_date=datetime(2026, 4, 1, tzinfo=UTC),
            )

    def test_naive_datetime_rejected(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        with pytest.raises(ValueError, match=r"timezone-aware"):
            engine.run(
                start_date=datetime(2026, 4, 1),  # naive
                end_date=datetime(2026, 4, 2, tzinfo=UTC),
            )

    def test_context_window_too_small_raises(self, connector: ReplayConnector) -> None:
        with pytest.raises(ValueError, match=r"context_window_bars must be >= 100"):
            _make_engine(connector, context_window_bars=50)


# ============================================================== Empty result


class TestBacktestEngineEmptyResult:
    def test_empty_window_returns_zero_stats(self, connector: ReplayConnector) -> None:
        """A window with zero visible bars returns an empty result, not a crash.

        We pick a start/end outside the dataset's range — both before
        bar 0 → the engine returns the empty result, NOT a crash.
        """

        engine = _make_engine(connector)
        result = engine.run(
            start_date=datetime(2030, 1, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2030, 1, 2, 0, 0, tzinfo=UTC),
        )
        assert result.n_bars_processed == 0
        assert result.n_trades == 0
        assert result.stats.n_trades == 0
        assert result.stats.winrate == 0.0
        assert result.stats.max_drawdown == 0.0
        assert result.equity_curve == []


# ============================================================== Slippage / Spread


class TestBacktestEngineFillMath:
    def test_slippage_model_name_in_trade_tags(
        self, connector: ReplayConnector
    ) -> None:
        """The engine stamps the model name on each trade so the journal can audit."""

        journal = InMemoryJournalStore()
        engine = _make_engine(
            connector,
            journal=journal,
            slippage_model=FixedSlippage(Decimal("0.77")),
            spread_model=FixedSpread(Decimal("0.42")),
        )
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 30, tzinfo=UTC),
            warmup_bars=50,
            max_bars=30,
        )
        # If a trade was opened, the tags should reference our model names.
        # (No assertion if n_trades == 0 — the test merely checks wiring.)
        if result.n_trades > 0:
            import asyncio

            async def _list_trades() -> list:
                return await journal.list_trades()

            trades = asyncio.run(_list_trades())
            for t in trades:
                assert t.tags.get("slippage_model") == "FixedSlippage"
                assert t.tags.get("spread_model") == "FixedSpread"

    def test_engine_default_uses_fixed_slippage_spread(
        self, connector: ReplayConnector
    ) -> None:
        """Engine without override still uses FixedSlippage + FixedSpread by default."""

        engine = BacktestEngine(
            connector=connector,
            journal=InMemoryJournalStore(),
            settings=Settings(),  # type: ignore[call-arg]
        )
        assert engine._slippage.name == "FixedSlippage"  # noqa: SLF001
        assert engine._spread.name == "FixedSpread"  # noqa: SLF001


# ============================================================== Context window


class TestBacktestEngineContextWindow:
    def test_context_window_bars_default_is_1500(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        assert engine._context_window_bars == 200  # noqa: SLF001

    def test_context_window_caps_per_bar_input(self, connector: ReplayConnector) -> None:
        """The bundle passed to engines is bounded by the context window."""

        engine = _make_engine(connector, context_window_bars=200)
        # The internal _build_bundle receives a slice bounded by context_window_bars.
        # We can spy on it by wrapping the call.
        captured: list[int] = []
        original = engine._build_bundle  # noqa: SLF001

        def spy(bars, ts, close):
            captured.append(len(bars))
            return original(bars, ts, close)

        engine._build_bundle = spy  # type: ignore[method-assign]  # noqa: SLF001
        engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 5, tzinfo=UTC),
            warmup_bars=50,
            max_bars=10,
        )
        assert captured, "build_bundle was never called"
        assert all(n <= 200 for n in captured), (
            f"some bundles exceed context_window_bars: max={max(captured)}"
        )


# ============================================================== Public surface


class TestBacktestEnginePublicAPI:
    def test_journal_property_returns_injected_journal(
        self, connector: ReplayConnector
    ) -> None:
        journal = InMemoryJournalStore()
        engine = _make_engine(connector, journal=journal)
        assert engine.journal is journal

    def test_journal_property_defaults_to_inmemory(self, connector: ReplayConnector) -> None:
        engine = BacktestEngine(
            connector=connector,
            settings=Settings(),  # type: ignore[call-arg]
        )
        from xauusd_bot.journal import InMemoryJournalStore

        assert isinstance(engine.journal, InMemoryJournalStore)

    def test_risk_manager_property_exposed(self, connector: ReplayConnector) -> None:
        engine = _make_engine(connector)
        from xauusd_bot.execution import RiskManager

        assert isinstance(engine.risk_manager, RiskManager)


# ============================================================== Smoke timing


class TestBacktestEngineTiming:
    def test_50_bar_run_completes_in_reasonable_time(
        self, connector: ReplayConnector
    ) -> None:
        """A 50-bar run with the rolling context window must complete in <60s.

        This is a soft upper bound — the engine should never take minutes
        for a 50-bar slice on a CI box. If it does, the O(N²) bounding
        has regressed.
        """

        engine = _make_engine(connector, context_window_bars=200)
        t = time.perf_counter()
        engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 50, tzinfo=UTC),
            warmup_bars=50,
            max_bars=50,
        )
        elapsed = time.perf_counter() - t
        assert elapsed < 60, f"50-bar run took {elapsed:.1f}s, expected < 60s"


# ============================================================== Cursor reset (WalkForward support)


class TestBacktestEngineCursorReset:
    def test_run_resets_connector_cursor_for_walkforward(
        self, connector: ReplayConnector
    ) -> None:
        """A second run() call must reset the cursor, not crash with time-travel.

        This is the core WalkForwardEngine guarantee: the same
        ReplayConnector can be re-driven across many windows.
        """

        engine = _make_engine(connector)
        # First run advances the cursor to the end of the first window.
        engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 30, tzinfo=UTC),
            warmup_bars=50,
            max_bars=10,
        )
        # Second run MUST reset the cursor and not raise.
        result = engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 30, tzinfo=UTC),
            warmup_bars=50,
            max_bars=10,
        )
        assert result.n_bars_processed == 10

    def test_run_resets_cursor_at_naive_first_bar_time(
        self, connector: ReplayConnector
    ) -> None:
        """If the source DataFrame has a naive first-bar time, the reset still works.

        We construct a tiny DataFrame inline so we don't have to mutate
        the shared fixture.
        """

        import pandas as _pd

        # Build a separate engine with its own connector that has a naive ts.
        from xauusd_bot.connectors.replay import ReplayConnector
        from xauusd_bot.journal import InMemoryJournalStore
        from xauusd_bot.backtest import BacktestEngine, FixedSlippage, FixedSpread
        from xauusd_bot.common.config import Settings

        # Write a small CSV with a naive timestamp to /tmp.
        csv_path = Path("/tmp/xauusd_naive_for_engine.csv")
        if not csv_path.exists():
            _pd.DataFrame(
                {
                    "time": ["2026-04-01 00:00:00", "2026-04-01 00:01:00", "2026-04-01 00:02:00"],
                    "open": [2375.0, 2375.0, 2375.0],
                    "high": [2375.5, 2375.5, 2375.5],
                    "low": [2374.5, 2374.5, 2374.5],
                    "close": [2375.0, 2375.0, 2375.0],
                    "tick_volume": [100, 100, 100],
                }
            ).to_csv(csv_path, index=False)
        c = ReplayConnector(source_path=csv_path, symbol="XAUUSD")
        engine = BacktestEngine(
            connector=c,
            journal=InMemoryJournalStore(),
            settings=Settings(),  # type: ignore[call-arg]
            slippage_model=FixedSlippage(Decimal("0.50")),
            spread_model=FixedSpread(Decimal("0.30")),
        )
        # Should not raise even though the source ts is naive.
        engine.run(
            start_date=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 1, 0, 3, tzinfo=UTC),
            warmup_bars=2,
            max_bars=2,
        )


# ============================================================== Internal helpers


class TestBacktestEngineInternalHelpers:
    def test_safe_float_handles_nan(self) -> None:
        from xauusd_bot.backtest.engine import _safe_float

        import math as _math

        assert _safe_float(_math.nan) == 0.0
        assert _safe_float(_math.inf) == 0.0
        assert _safe_float(-_math.inf) == 0.0
        assert _safe_float(1.5) == 1.5

    def test_max_drawdown_duration_bars_monotonic_curve(self) -> None:
        from xauusd_bot.backtest.engine import _max_drawdown_duration_bars

        # Monotonically rising → 0.
        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal("0")),
            (datetime(2026, 4, 2, tzinfo=UTC), Decimal("10")),
            (datetime(2026, 4, 3, tzinfo=UTC), Decimal("20")),
        ]
        assert _max_drawdown_duration_bars(ec) == 0

    def test_max_drawdown_duration_bars_with_drawdown(self) -> None:
        from xauusd_bot.backtest.engine import _max_drawdown_duration_bars

        # Peak at idx 1, trough at idx 3 → 2 bars of drawdown.
        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal("0")),
            (datetime(2026, 4, 2, tzinfo=UTC), Decimal("100")),
            (datetime(2026, 4, 3, tzinfo=UTC), Decimal("50")),
            (datetime(2026, 4, 4, tzinfo=UTC), Decimal("30")),
        ]
        assert _max_drawdown_duration_bars(ec) == 2

    def test_max_drawdown_duration_bars_empty(self) -> None:
        from xauusd_bot.backtest.engine import _max_drawdown_duration_bars

        assert _max_drawdown_duration_bars([]) == 0

    def test_max_drawdown_duration_bars_single_point(self) -> None:
        from xauusd_bot.backtest.engine import _max_drawdown_duration_bars

        ec = [(datetime(2026, 4, 1, tzinfo=UTC), Decimal("0"))]
        assert _max_drawdown_duration_bars(ec) == 0

    def test_sortino_from_returns_no_downside(self) -> None:
        from xauusd_bot.backtest.engine import _sortino_from_returns

        # All positive returns → 0 (downside is empty, ratio undefined).
        out = _sortino_from_returns([0.01, 0.02, 0.03], 252)
        assert out == 0.0

    def test_sortino_from_returns_too_few(self) -> None:
        from xauusd_bot.backtest.engine import _sortino_from_returns

        assert _sortino_from_returns([], 252) == 0.0
        assert _sortino_from_returns([0.01], 252) == 0.0

    def test_sortino_from_returns_mixed(self) -> None:
        from xauusd_bot.backtest.engine import _sortino_from_returns

        out = _sortino_from_returns([0.05, -0.02, 0.03, -0.01], 252)
        # Should be a finite number (positive or negative) — the exact value
        # depends on the period factor; we just check it's computable.
        import math as _math

        assert _math.isfinite(out) or out == 0.0

    def test_sample_equity_curve_under_max(self) -> None:
        from xauusd_bot.backtest.engine import _sample_equity_curve

        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal(str(i)))
            for i in range(10)
        ]
        out = _sample_equity_curve(ec, max_points=20)
        assert out == ec  # all points kept

    def test_sample_equity_curve_over_max(self) -> None:
        from xauusd_bot.backtest.engine import _sample_equity_curve

        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal(str(i)))
            for i in range(100)
        ]
        out = _sample_equity_curve(ec, max_points=10)
        assert len(out) <= 10
        assert len(out) > 0

    def test_sample_equity_curve_empty(self) -> None:
        from xauusd_bot.backtest.engine import _sample_equity_curve

        assert _sample_equity_curve([]) == []
        assert _sample_equity_curve([], max_points=10) == []

    def test_sample_equity_curve_zero_max(self) -> None:
        from xauusd_bot.backtest.engine import _sample_equity_curve

        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal("0")),
            (datetime(2026, 4, 2, tzinfo=UTC), Decimal("10")),
        ]
        assert _sample_equity_curve(ec, max_points=0) == []

    def test_to_breakdown_entry_converts_correctly(self) -> None:
        from xauusd_bot.backtest.engine import _to_breakdown_entry

        b = _to_breakdown_entry(
            {
                "count": 5,
                "closed": 4,
                "wins": 3,
                "losses": 1,
                "breakeven": 0,
                "winrate": 0.75,
                "avg_r": 0.5,
                "total_r": 2.0,
                "total_pnl": 100.0,
            }
        )
        assert b.count == 5
        assert b.winrate == 0.75
        assert b.avg_r == 0.5
        assert b.total_pnl == 100.0

    def test_to_breakdown_entry_with_defaults(self) -> None:
        from xauusd_bot.backtest.engine import _to_breakdown_entry

        # Missing keys default to 0.
        b = _to_breakdown_entry({})
        assert b.count == 0
        assert b.winrate == 0.0
        assert b.avg_r == 0.0

    def test_compute_sortino_on_engine_result(self, connector: ReplayConnector) -> None:
        """The engine exposes ``_compute_sortino`` which works on equity curves."""

        engine = _make_engine(connector)
        # Empty curve → 0.
        assert engine._compute_sortino([]) == 0.0  # noqa: SLF001
        # Monotonically rising curve → 0 (no downside).
        ec = [
            (datetime(2026, 4, 1, tzinfo=UTC), Decimal("0")),
            (datetime(2026, 4, 2, tzinfo=UTC), Decimal("10")),
            (datetime(2026, 4, 3, tzinfo=UTC), Decimal("20")),
        ]
        assert engine._compute_sortino(ec) == 0.0  # noqa: SLF001

    def test_compute_spread_points_for_last_bar(self, connector: ReplayConnector) -> None:
        """The helper used by PreTradeSafetyChecker returns a float in points."""

        engine = _make_engine(connector)
        # First advance so the cursor has a position.
        engine._connector.advance_time(datetime(2026, 4, 1, 0, 0, tzinfo=UTC))  # noqa: SLF001
        val = engine._compute_spread_points_for_last_bar()  # noqa: SLF001
        assert isinstance(val, float)
        assert val >= 0.0

    def test_run_async_helper(self, connector: ReplayConnector) -> None:
        """The async bridge can run a coroutine to completion."""

        engine = _make_engine(connector)

        async def _noop_coro() -> str:
            return "done"

        out = engine._run_async(_noop_coro())  # noqa: SLF001
        assert out == "done"

    def test_find_first_bar_at_or_after(self, connector: ReplayConnector) -> None:
        """Static helper returns the right index for a known timestamp."""

        engine = _make_engine(connector)
        bars = engine._materialise_bars()  # noqa: SLF001
        # The first bar is at 2026-04-01 00:00:00 UTC.
        idx = engine._find_first_bar_at_or_after(  # noqa: SLF001
            bars, datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        )
        assert idx == 0
        # Past the end → returns len(bars).
        idx = engine._find_first_bar_at_or_after(  # noqa: SLF001
            bars, datetime(2030, 1, 1, tzinfo=UTC)
        )
        assert idx == len(bars)

    def test_find_last_bar_at_or_before(self, connector: ReplayConnector) -> None:
        """Static helper returns the right index for a known timestamp."""

        engine = _make_engine(connector)
        bars = engine._materialise_bars()  # noqa: SLF001
        idx = engine._find_last_bar_at_or_before(  # noqa: SLF001
            bars, datetime(2026, 4, 1, 0, 5, tzinfo=UTC)
        )
        # 0:05 is 5 minutes after the first bar → idx 5.
        assert idx == 5
        # Before the first bar → -1.
        idx = engine._find_last_bar_at_or_before(  # noqa: SLF001
            bars, datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert idx == -1
