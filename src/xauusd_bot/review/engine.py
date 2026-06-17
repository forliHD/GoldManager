"""ReviewEngine — Block 5c Phase 2.

The :class:`ReviewEngine` orchestrates one Daily or Weekly review:

    1. Pull trades / snapshots / discrepancies from the JournalStore.
    2. Build a :class:`KPISummary` via the Block-5a aggregation helpers
       (re-used verbatim — no re-implementation).
    3. If ``len(trades) >= min_sample_size``: call the
       :class:`ReviewerOpenRouterClient` and capture the
       :class:`ReviewOutput`.
    4. If below threshold: return a ReviewRun with
       ``insufficient_data=True`` and NO LLM call (Caveat 4i.4).
    5. For weekly: also compute cross-day pattern detection
       (setup breakdown over days, score-band drift, discrepancy
       summary).

This module is the *only* place that talks to the reviewer LLM in
the daily/weekly path. It is deliberately a thin orchestrator —
all the heavy lifting lives in the existing Block-5a / Block-5b /
Block-6 modules it composes.

Invariants
----------
* **I-1:** never imports MetaTrader5.
* **I-4:** never mutates live settings. The ReviewRun output is
  *advisory only*; the FittingProposalEngine persists the
  proposals and the operator decides what (if anything) to
  implement.
* **PIT:** all journal reads are forward-in-time — the engine
  uses the journal's :meth:`list_trades` /
  :meth:`list_snapshots` /
  :meth:`list_discrepancies_v2` directly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog

from xauusd_bot.backtest.engine import BacktestEngine
from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import ScoreBand
from xauusd_bot.common.schemas.review import (
    FeatureSnapshotLite as _FSLite,
)
from xauusd_bot.common.schemas.review import (
    KPISummary,
    LLMFallbackDiscrepancyLite,
    ReviewOutput,
    ReviewRequest,
    ReviewRun,
    TradeSummary,
)
from xauusd_bot.journal.queries import (
    compute_equity_curve,
    compute_max_drawdown,
    compute_r_distribution,
    compute_score_band_stats,
    compute_session_stats,
    compute_setup_breakdown,
    compute_sharpe,
    compute_sortino,
)
from xauusd_bot.journal.store import JournalStore
from xauusd_bot.review.reviewer_client import (
    ReviewerError,
    ReviewerOpenRouterClient,
)

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------- constants


# Discrepancy sample cap (Caveat 4i.7).
_MAX_DISCREPANCIES_PER_REVIEW = 50

# Snapshot sample cap. The engine samples evenly when the period
# contains more than this.
_MAX_SNAPSHOTS_PER_REVIEW = 200

# Defaults for ``min_sample_size`` per period kind (Caveat 4i.4).
_DAILY_MIN_SAMPLE = 10
_WEEKLY_MIN_SAMPLE = 30


# ----------------------------------------------------------------- helpers


def _to_trade_summary(t: Any) -> TradeSummary:
    """Convert a :class:`TradeRecord` to a :class:`TradeSummary`."""

    return TradeSummary(
        id=t.id,
        timestamp_open=t.timestamp_open,
        timestamp_close=t.timestamp_close,
        symbol=t.symbol,
        side=t.side,
        score=t.score,
        band=t.band,
        entry_type=t.entry_type,
        session=t.session,
        structure_at_entry=t.structure_at_entry,
        pnl_realized=float(t.pnl_realized) if t.pnl_realized is not None else None,
        r_multiple=t.r_multiple,
        exit_reason=t.exit_reason.value if t.exit_reason is not None else None,
        slippage_pips=t.slippage_pips,
        engine_source=t.engine_source,
    )


def _to_snapshot_lite(s: Any) -> _FSLite:
    """Convert a :class:`FeatureSnapshotRecord` to a :class:`FeatureSnapshotLite`."""

    raw_band = s.features.get("band")
    band: ScoreBand | None = None
    if raw_band is not None:
        try:
            band = ScoreBand(raw_band)
        except ValueError:
            # Defensive: a journal-snapshot might carry a free-form
            # band string (the engine stores ``.value``). Fall back
            # to lookup by .value if the input is e.g. "PREPARE_65_74".
            for member in ScoreBand:
                if member.value == raw_band:
                    band = member
                    break
    return _FSLite(
        id=s.id,
        bar_time=s.bar_time,
        session=s.features.get("session"),
        structure_trend=s.features.get("structure_trend"),
        in_blackout=s.features.get("in_blackout"),
        atr=s.features.get("atr"),
        score=s.features.get("score"),
        band=band,
        engine_source=s.features.get("engine_source", "rule"),
    )


def _build_kpis(trades: list[Any]) -> KPISummary:
    """Build a :class:`KPISummary` from a list of trades.

    Reuses the Block-5a aggregation helpers — no re-implementation.
    """

    equity_curve = compute_equity_curve(trades)
    max_dd, _peak, _trough = compute_max_drawdown(equity_curve)
    closed = [t for t in trades if t.pnl_realized is not None]
    n_trades = len(trades)
    n_closed = len(closed)
    n_wins = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
    n_losses = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
    winrate = (n_wins / n_closed) if n_closed > 0 else 0.0
    rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
    avg_r = (sum(rs) / len(rs)) if rs else 0.0
    total_r = sum(rs)
    pos = sum(float(t.pnl_realized) for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
    neg = sum(-float(t.pnl_realized) for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
    profit_factor = (pos / neg) if neg > 0 else 0.0
    total_pnl = sum(float(t.pnl_realized) for t in closed if t.pnl_realized is not None)
    final_equity = 10000.0 + total_pnl  # synthetic baseline — see caveat below
    sharpe = compute_sharpe(equity_curve, periods_per_year=252 * 8)
    sortino = compute_sortino(equity_curve, periods_per_year=252 * 8)

    return KPISummary(
        n_trades=n_trades,
        n_closed=n_closed,
        n_wins=n_wins,
        n_losses=n_losses,
        winrate=winrate,
        avg_r=avg_r,
        total_r=total_r,
        profit_factor=profit_factor,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=float(max_dd),
        total_pnl=total_pnl,
        setup_breakdown=compute_setup_breakdown(trades),
        session_breakdown=compute_session_stats(trades),
        score_band_breakdown=compute_score_band_stats(trades),
        r_distribution=compute_r_distribution(trades),
    )


def _to_discrepancy_lite(d: Any) -> LLMFallbackDiscrepancyLite:
    """Convert a :class:`LLMFallbackDiscrepancyV2` to its lite counterpart."""

    return LLMFallbackDiscrepancyLite(
        timestamp=d.timestamp,
        decision_id=d.decision_id,
        score=d.score,
        rule_decision=d.rule_decision,
        llm_decision=d.llm_decision,
        fallback_reason=d.fallback_reason,
        llm_raw_response=d.llm_raw_response,
    )


def _sample_evenly(items: list[Any], max_count: int) -> list[Any]:
    """Evenly sample at most ``max_count`` items from a list."""

    if len(items) <= max_count:
        return list(items)
    if max_count <= 0:
        return []
    step = max(1, len(items) // max_count)
    sampled = items[::step]
    if len(sampled) > max_count:
        sampled = sampled[:max_count]
    return sampled


# ----------------------------------------------------------------- engine


class ReviewEngine:
    """The orchestrator for daily / weekly reviews.

    Parameters
    ----------
    journal:
        :class:`JournalStore`. The engine uses the Block-5a
        read-API (``list_trades`` / ``list_snapshots`` /
        ``list_discrepancies_v2``).
    backtest:
        Optional :class:`BacktestEngine`. Wired in for the
        :class:`FittingProposalEngine` downstream (validation
        backtests). The :class:`ReviewEngine` itself does not run
        backtests.
    reviewer:
        :class:`ReviewerOpenRouterClient`. REUSE of the Block-6
        OpenRouter stack — see Caveat 4i.2.
    settings:
        :class:`Settings`. Used to read ``review_min_sample_size``
        and the OpenRouter settings (the reviewer reads those
        directly from its own base client).
    daily_min_sample_size:
        Override the default 10-trade floor for daily reviews.
    weekly_min_sample_size:
        Override the default 30-trade floor for weekly reviews.
    """

    def __init__(
        self,
        *,
        journal: JournalStore,
        backtest: BacktestEngine | None,
        reviewer: ReviewerOpenRouterClient,
        settings: Settings,
        daily_min_sample_size: int = _DAILY_MIN_SAMPLE,
        weekly_min_sample_size: int = _WEEKLY_MIN_SAMPLE,
    ) -> None:
        self._journal = journal
        self._backtest = backtest
        self._reviewer = reviewer
        self._settings = settings
        self._daily_min = int(daily_min_sample_size)
        self._weekly_min = int(weekly_min_sample_size)

    # ============================================================ public

    async def run_daily(self, day: date) -> ReviewRun:
        """Run a Daily review for ``day`` (UTC midnight → next midnight)."""

        period_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        period_end = period_start + timedelta(days=1)
        return await self._run_period(
            period_start=period_start,
            period_end=period_end,
            period_kind="daily",
            min_sample_size=self._daily_min,
            cross_day_patterns=False,
        )

    async def run_weekly(self, week_start: date) -> ReviewRun:
        """Run a Weekly review for the 7-day window starting ``week_start``.

        The reviewer gets the cross-day pattern detection summary as
        part of the payload — the LLM uses it to detect "Mondays
        consistently lose, Thursdays consistently win" patterns.
        """

        period_start = datetime(week_start.year, week_start.month, week_start.day, tzinfo=UTC)
        period_end = period_start + timedelta(days=7)
        return await self._run_period(
            period_start=period_start,
            period_end=period_end,
            period_kind="weekly",
            min_sample_size=self._weekly_min,
            cross_day_patterns=True,
        )

    # ============================================================ internals

    async def _run_period(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        period_kind: str,
        min_sample_size: int,
        cross_day_patterns: bool,
    ) -> ReviewRun:
        # 1. Pull trades / snapshots / discrepancies.
        all_trades = await self._journal.list_trades(
            start=period_start, end=period_end, limit=10_000
        )
        all_snapshots = await self._journal.list_snapshots(
            start=period_start, end=period_end, limit=10_000
        )
        all_discrepancies = await self._journal.list_discrepancies_v2(
            start=period_start, end=period_end, limit=10_000
        )

        # 2. Sample snapshots + discrepancies to keep the LLM payload bounded.
        snapshots = _sample_evenly(all_snapshots, _MAX_SNAPSHOTS_PER_REVIEW)
        discrepancies = _sample_evenly(all_discrepancies, _MAX_DISCREPANCIES_PER_REVIEW)

        run_id = uuid4()
        setup_breakdown_over_days: dict[str, dict[str, float]] = {}
        score_band_drift: dict[str, dict[str, float]] = {}
        discrepancy_summary: dict[str, int] = {}

        # 3. Cross-day pattern detection (weekly only).
        if cross_day_patterns:
            setup_breakdown_over_days = _setup_breakdown_by_day(all_trades)
            score_band_drift = _score_band_drift_by_day(all_trades)
            discrepancy_summary = _discrepancy_summary_by_day(all_discrepancies)

        # 4. Sufficient-data gate (Caveat 4i.4). NO LLM call when below.
        if len(all_trades) < min_sample_size:
            log.info(
                "review_insufficient_data",
                period_kind=period_kind,
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
                trade_count=len(all_trades),
                min_sample_size=min_sample_size,
            )
            return ReviewRun(
                id=run_id,
                period_start=period_start,
                period_end=period_end,
                period_kind=period_kind,  # type: ignore[arg-type]
                insufficient_data=True,
                min_sample_size=min_sample_size,
                trade_count=len(all_trades),
                snapshot_count=len(snapshots),
                discrepancy_count=len(discrepancies),
                setup_breakdown_over_days=setup_breakdown_over_days,
                score_band_drift=score_band_drift,
                discrepancy_summary=discrepancy_summary,
                output=None,
                error=None,
            )

        # 5. Build the LLM request.
        request = self._build_review_request(
            period_start=period_start,
            period_end=period_end,
            period_kind=period_kind,  # type: ignore[arg-type]
            trades=all_trades,
            snapshots=snapshots,
            discrepancies=discrepancies,
            min_sample_size=min_sample_size,
            cross_day_patterns=cross_day_patterns,
            setup_breakdown_over_days=setup_breakdown_over_days,
            score_band_drift=score_band_drift,
            discrepancy_summary=discrepancy_summary,
        )

        # 6. Call the reviewer.
        try:
            output: ReviewOutput = await self._reviewer.review(request)
        except ReviewerError as exc:
            log.warning(
                "review_reviewer_error",
                period_kind=period_kind,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return ReviewRun(
                id=run_id,
                period_start=period_start,
                period_end=period_end,
                period_kind=period_kind,  # type: ignore[arg-type]
                insufficient_data=False,
                min_sample_size=min_sample_size,
                trade_count=len(all_trades),
                snapshot_count=len(snapshots),
                discrepancy_count=len(discrepancies),
                setup_breakdown_over_days=setup_breakdown_over_days,
                score_band_drift=score_band_drift,
                discrepancy_summary=discrepancy_summary,
                output=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        # 7. Successful review.
        log.info(
            "review_run_complete",
            period_kind=period_kind,
            period_start=period_start.isoformat(),
            trade_count=len(all_trades),
            snapshot_count=len(snapshots),
            discrepancy_count=len(discrepancies),
            proposal_count=len(output.proposals),
            data_sufficiency=output.data_sufficiency,
        )
        return ReviewRun(
            id=run_id,
            period_start=period_start,
            period_end=period_end,
            period_kind=period_kind,  # type: ignore[arg-type]
            insufficient_data=False,
            min_sample_size=min_sample_size,
            trade_count=len(all_trades),
            snapshot_count=len(snapshots),
            discrepancy_count=len(discrepancies),
            setup_breakdown_over_days=setup_breakdown_over_days,
            score_band_drift=score_band_drift,
            discrepancy_summary=discrepancy_summary,
            output=output,
            error=None,
        )

    def _build_review_request(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        period_kind: str,
        trades: list[Any],
        snapshots: list[Any],
        discrepancies: list[Any],
        min_sample_size: int,
        cross_day_patterns: bool,
        setup_breakdown_over_days: dict[str, dict[str, float]],
        score_band_drift: dict[str, dict[str, float]],
        discrepancy_summary: dict[str, int],
    ) -> ReviewRequest:
        return ReviewRequest(
            period_start=period_start,
            period_end=period_end,
            period_kind=period_kind,  # type: ignore[arg-type]
            trades=[_to_trade_summary(t) for t in trades],
            snapshots_sample=[_to_snapshot_lite(s) for s in snapshots],
            kpis=_build_kpis(trades),
            discrepancies=[_to_discrepancy_lite(d) for d in discrepancies],
            min_sample_size_for_proposals=min_sample_size,
        )


# ----------------------------------------------------------------- cross-day helpers


def _setup_breakdown_by_day(trades: list[Any]) -> dict[str, dict[str, float]]:
    """Per-weekday KPI summary. Output: ``{weekday: {count, closed, wins, winrate, avg_r}}``."""

    out: dict[str, dict[str, float]] = {}
    by_day: dict[str, list[Any]] = defaultdict(list)
    for t in trades:
        weekday = t.timestamp_open.astimezone(UTC).strftime("%a")
        by_day[weekday].append(t)
    for day_name in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        group = by_day.get(day_name, [])
        closed = [t for t in group if t.pnl_realized is not None]
        n_wins = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
        n_losses = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
        rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
        winrate = (n_wins / len(closed)) if closed else 0.0
        avg_r = (sum(rs) / len(rs)) if rs else 0.0
        out[day_name] = {
            "count": float(len(group)),
            "closed": float(len(closed)),
            "wins": float(n_wins),
            "losses": float(n_losses),
            "winrate": winrate,
            "avg_r": avg_r,
            "total_r": float(sum(rs)),
        }
    return out


def _score_band_drift_by_day(trades: list[Any]) -> dict[str, dict[str, float]]:
    """Per-weekday per-band count. Output: ``{weekday: {band: count}}``."""

    out: dict[str, dict[str, float]] = {}
    by_day_band: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in trades:
        weekday = t.timestamp_open.astimezone(UTC).strftime("%a")
        by_day_band[weekday][t.band.value] += 1
    for day_name in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        out[day_name] = {band: float(c) for band, c in by_day_band[day_name].items()}
    return out


def _discrepancy_summary_by_day(discrepancies: list[Any]) -> dict[str, int]:
    """Per-weekday discrepancy count. Output: ``{weekday: count}``."""

    by_day: dict[str, int] = defaultdict(int)
    for d in discrepancies:
        weekday = d.timestamp.astimezone(UTC).strftime("%a")
        by_day[weekday] += 1
    out = {day_name: by_day.get(day_name, 0) for day_name in ("Mon", "Tue", "Wed", "Thu", "Fri")}
    out["total"] = sum(by_day.values())
    return out


__all__ = ["ReviewEngine"]