"""WalkForwardEngine — Block 5b Phase 1.

The :class:`WalkForwardEngine` runs the :class:`BacktestEngine` over
sliding (in_sample, out_of_sample) windows to evaluate whether a
strategy's performance holds up *outside* the calibration window.

Pipeline
--------
For ``[start_date, end_date]`` with the default knobs (12-month IS,
3-month OOS, 3-month step) the engine builds N windows:

* window 0: in=[start, start+12m), oos=[start+12m, start+15m)
* window 1: in=[start+3m, start+15m), oos=[start+15m, start+18m)
* window 2: in=[start+6m, start+18m), oos=[start+18m, start+21m)
* ...

The number of windows depends on the span. The engine stops
gracefully when the next IS window would extend past ``end_date``.

For each window the engine:

1. Runs :class:`BacktestEngine` on the IS slice → ``in_sample_stats``.
2. Runs :class:`BacktestEngine` on the OOS slice →
   ``out_of_sample_stats``.
3. Computes ``oos_degradation_pct`` (the % drop in Sharpe from IS
   to OOS, or the OOS-beat-IS if negative).

The engine does NOT need its own connector — it reuses the
:class:`ReplayConnector` the caller passed to it, and the
:class:`BacktestEngine` it constructed internally. This is the
*same code path* the live stack uses, with the only difference being
the time window the :class:`ReplayConnector` is asked to walk.

Robustness matrix
-----------------
The :class:`WalkForwardResult.robustness_matrix` is a list of
``[is_sharpe, oos_sharpe]`` rows — one per window. The canonical
"IS vs OOS" scatter plot uses this directly. Symmetry: equal
number of IS and OOS runs per window (always 1 IS → 1 OOS).

Determinism
-----------
Same inputs (connector, settings, slippage / spread models, in /
oos month knobs) → identical :class:`WalkForwardResult` on every
run. No RNG, no clock, no state.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from xauusd_bot.backtest.engine import BacktestEngine
from xauusd_bot.common.schemas.backtest import (
    WalkForwardResult,
    WalkForwardWindow,
)
from xauusd_bot.connectors.replay import ReplayConnector

log = structlog.get_logger(__name__)


# Default per Plan §6.4: 12m in / 3m out, step 3m.
DEFAULT_IN_SAMPLE_MONTHS = 12
DEFAULT_OUT_OF_SAMPLE_MONTHS = 3
DEFAULT_STEP_MONTHS = 3

# Heuristic threshold above which a window is flagged "overfit" /
# "suspicious". Matches the plan and the result schema's
# ``oos_sharpe_degradation`` heuristic.
OVERFIT_DEGRADATION_THRESHOLD_PCT = 30.0


def _add_months(t: datetime, months: int) -> datetime:
    """Add ``months`` to ``t`` (UTC-aware), handling month-end overflow.

    Pure date math — no calendar library needed. Day is clamped to
    the last valid day of the target month (e.g. Jan 31 + 1 month →
    Feb 28/29). Time component is preserved.
    """

    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    else:
        t = t.astimezone(UTC)
    # Compute the target year + month.
    total_months = t.month - 1 + months
    new_year = t.year + total_months // 12
    new_month = total_months % 12 + 1
    # Clamp the day to the last valid day of the new month.
    if new_month == 12:
        next_month_first = datetime(new_year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month_first = datetime(new_year, new_month + 1, 1, tzinfo=UTC)
    last_day = (next_month_first - timedelta(days=1)).day
    new_day = min(t.day, last_day)
    return t.replace(year=new_year, month=new_month, day=new_day)


class WalkForwardEngine:
    """Sliding-window backtest validator.

    Parameters
    ----------
    connector:
        The :class:`ReplayConnector` to use. The WalkForwardEngine
        drives it across multiple time windows but does NOT own it
        — the caller is responsible for its lifecycle.
    settings:
        Block-level :class:`Settings`. Threaded through to the
        inner :class:`BacktestEngine` instances.
    slippage_model, spread_model:
        Same defaults as :class:`BacktestEngine`. Threaded through
        identically.
    periods_per_year:
        Sharpe / Sortino annualization factor. Same default.
    initial_balance:
        Starting balance. Same default.
    warmup_bars:
        Warm-up window per inner backtest. Same default 500.
    strategy_version:
        Version tag. Same default ``"block5b-v1"``.
    in_sample_months, out_of_sample_months, step_months:
        Window knobs. Defaults = 12m / 3m / 3m per Plan §6.4.
    """

    def __init__(
        self,
        connector: ReplayConnector,
        settings: Any = None,
        *,
        slippage_model: Any = None,
        spread_model: Any = None,
        periods_per_year: int = 252 * 28,
        initial_balance: Any = None,
        warmup_bars: int = 500,
        context_window_bars: int = 1500,
        strategy_version: str = "block5b-v1",
        max_bars_per_window: int | None = None,
        in_sample_months: int | None = None,
        out_of_sample_months: int | None = None,
        step_months: int | None = None,
        in_sample_days: int | None = None,
        out_of_sample_days: int | None = None,
        step_days: int | None = None,
    ) -> None:
        # Accept either month-based (plan default) OR day-based (test
        # data is 30 days, not 30 months). If both are supplied, the
        # day-based values win.
        if in_sample_days is not None:
            self._in_sample_days: int | None = in_sample_days
            self._out_of_sample_days: int | None = out_of_sample_days or in_sample_days // 4
            self._step_days: int | None = step_days or in_sample_days // 4
        else:
            self._in_sample_days = None
            self._out_of_sample_days = None
            self._step_days = None
        if in_sample_months is not None:
            self._in_sample_months = in_sample_months
            self._out_of_sample_months = out_of_sample_months or in_sample_months // 4
            self._step_months = step_months or in_sample_months // 4
        else:
            self._in_sample_months = None
            self._out_of_sample_months = None
            self._step_months = None
        if self._in_sample_days is None and self._in_sample_months is None:
            self._in_sample_months = DEFAULT_IN_SAMPLE_MONTHS
            self._out_of_sample_months = DEFAULT_OUT_OF_SAMPLE_MONTHS
            self._step_months = DEFAULT_STEP_MONTHS

        # Validate.
        if self._in_sample_days is not None and self._in_sample_days <= 0:
            raise ValueError(f"in_sample_days must be > 0, got {self._in_sample_days}")
        if self._in_sample_months is not None and self._in_sample_months <= 0:
            raise ValueError(f"in_sample_months must be > 0, got {self._in_sample_months}")

        self._connector = connector
        self._settings = settings
        self._slippage_model = slippage_model
        self._spread_model = spread_model
        self._periods_per_year = periods_per_year
        self._initial_balance = initial_balance
        self._warmup_bars = warmup_bars
        self._context_window_bars = context_window_bars
        self._strategy_version = strategy_version
        self._max_bars_per_window = max_bars_per_window

    # ----------------------------------------------------------- public

    def run(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> WalkForwardResult:
        """Execute the walk-forward and return a :class:`WalkForwardResult`.

        Parameters
        ----------
        start_date:
            Window 0 in-sample start.
        end_date:
            Hard upper bound — the engine stops the moment a window's
            OOS slice would start after this date.
        """

        if start_date.tzinfo is None or end_date.tzinfo is None:
            raise ValueError("start_date / end_date must be timezone-aware (UTC).")
        if end_date <= start_date:
            raise ValueError(f"end_date ({end_date}) must be after start_date ({start_date}).")

        started = time.perf_counter()
        windows: list[WalkForwardWindow] = []
        n_bars_processed = 0
        win_idx = 0
        in_start = start_date
        use_days = self._in_sample_days is not None
        while True:
            if use_days:
                in_end_excl = in_start + timedelta(days=self._in_sample_days or 1)
                oos_start = in_end_excl
                oos_end_excl = oos_start + timedelta(days=self._out_of_sample_days or 1)
            else:
                in_end_excl = _add_months(in_start, self._in_sample_months or 1)
                oos_start = in_end_excl
                oos_end_excl = _add_months(oos_start, self._out_of_sample_months or 1)
            # Stop conditions: OOS end must fit inside the user's
            # end_date, AND the OOS span must be > 0.
            if oos_start >= end_date or oos_end_excl > end_date:
                break
            log.info(
                "walkforward_window",
                index=win_idx,
                in_start=in_start.isoformat(),
                in_end_excl=in_end_excl.isoformat(),
                oos_start=oos_start.isoformat(),
                oos_end_excl=oos_end_excl.isoformat(),
            )

            in_engine = self._build_backtest_engine(journal=None)
            in_result = in_engine.run(
                start_date=in_start,
                end_date=in_end_excl - timedelta(microseconds=1),
                warmup_bars=self._warmup_bars,
                max_bars=self._max_bars_per_window,
            )
            n_bars_processed += in_result.n_bars_processed

            oos_engine = self._build_backtest_engine(journal=None)
            oos_result = oos_engine.run(
                start_date=oos_start,
                end_date=oos_end_excl - timedelta(microseconds=1),
                warmup_bars=self._warmup_bars,
                max_bars=self._max_bars_per_window,
            )
            n_bars_processed += oos_result.n_bars_processed

            is_sharpe = in_result.stats.sharpe
            oos_sharpe = oos_result.stats.sharpe
            # degradation_pct: positive = IS > OOS (degraded), negative = OOS > IS.
            # Use max(|is_sharpe|, 1.0) as the denominator to avoid
            # divide-by-zero and to keep the heuristic readable.
            denom = max(abs(is_sharpe), 1.0)
            oos_degradation_pct = float((is_sharpe - oos_sharpe) / denom * 100.0)

            windows.append(
                WalkForwardWindow(
                    window_index=win_idx,
                    start_in=in_start,
                    end_in=in_end_excl - timedelta(microseconds=1),
                    start_oos=oos_start,
                    end_oos=oos_end_excl - timedelta(microseconds=1),
                    in_sample_stats=in_result.stats,
                    out_of_sample_stats=oos_result.stats,
                    oos_degradation_pct=oos_degradation_pct,
                    in_sample_sharpe=is_sharpe,
                    out_of_sample_sharpe=oos_sharpe,
                )
            )
            win_idx += 1
            if use_days:
                in_start = in_start + timedelta(days=self._step_days or 1)
            else:
                in_start = _add_months(in_start, self._step_months or 1)
            if in_start >= end_date:
                break

        # Aggregate KPIs.
        if windows:
            is_sharpes = [w.in_sample_sharpe for w in windows]
            oos_sharpes = [w.out_of_sample_sharpe for w in windows]
            mean_oos = sum(oos_sharpes) / len(oos_sharpes)
            mean_is = sum(is_sharpes) / len(is_sharpes)
            # Sample std-dev of OOS Sharpes (n-1 in denominator).
            if len(oos_sharpes) > 1:
                var = sum((s - mean_oos) ** 2 for s in oos_sharpes) / (len(oos_sharpes) - 1)
                std_oos = math.sqrt(var) if var > 0 else 0.0
            else:
                std_oos = 0.0
            denom = max(abs(mean_is), 1.0)
            oos_sharpe_degradation = float((mean_is - mean_oos) / denom * 100.0)
            is_overfit = oos_sharpe_degradation > OVERFIT_DEGRADATION_THRESHOLD_PCT
        else:
            mean_oos = 0.0
            std_oos = 0.0
            oos_sharpe_degradation = 0.0
            is_overfit = False

        robustness_matrix = [
            [w.in_sample_sharpe, w.out_of_sample_sharpe] for w in windows
        ]
        runtime = round(time.perf_counter() - started, 6)
        log.info(
            "walkforward_run_complete",
            n_windows=len(windows),
            mean_oos_sharpe=mean_oos,
            oos_sharpe_degradation=oos_sharpe_degradation,
            is_overfit=is_overfit,
            runtime=runtime,
        )
        return WalkForwardResult(
            windows=windows,
            robustness_matrix=robustness_matrix,
            mean_oos_sharpe=_safe(mean_oos),
            std_oos_sharpe=_safe(std_oos),
            oos_sharpe_degradation=oos_sharpe_degradation,
            is_overfit=is_overfit,
            runtime_seconds=runtime,
            start_date=start_date,
            end_date=end_date,
            n_bars_processed=n_bars_processed,
        )

    # ----------------------------------------------------------- helpers

    def _build_backtest_engine(self, journal: Any) -> BacktestEngine:
        """Construct a fresh :class:`BacktestEngine` for one window.

        Each window needs its own engine + journal — the inner state
        (PnL counters, open positions) must NOT leak across windows.
        Note: ``warmup_bars`` is on :meth:`BacktestEngine.run`, not
        on ``__init__``, so it's threaded into the call site, not here.
        """

        kwargs: dict[str, Any] = {
            "periods_per_year": self._periods_per_year,
            "context_window_bars": self._context_window_bars,
            "strategy_version": self._strategy_version,
        }
        if self._settings is not None:
            kwargs["settings"] = self._settings
        if self._slippage_model is not None:
            kwargs["slippage_model"] = self._slippage_model
        if self._spread_model is not None:
            kwargs["spread_model"] = self._spread_model
        if self._initial_balance is not None:
            kwargs["initial_balance"] = self._initial_balance
        if journal is not None:
            kwargs["journal"] = journal
        return BacktestEngine(self._connector, **kwargs)


def _safe(x: float) -> float:
    """Coerce NaN / inf to 0.0 for JSON cleanliness."""

    if not math.isfinite(x):
        return 0.0
    return float(x)


# ----------------------------------------------------------------- re-exports


__all__ = [
    "DEFAULT_IN_SAMPLE_MONTHS",
    "DEFAULT_OUT_OF_SAMPLE_MONTHS",
    "DEFAULT_STEP_MONTHS",
    "OVERFIT_DEGRADATION_THRESHOLD_PCT",
    "WalkForwardEngine",
    "_add_months",
]
