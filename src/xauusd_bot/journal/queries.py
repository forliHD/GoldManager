"""Pure-function aggregations over :class:`TradeRecord` lists.

This module is the **read API** for the journal — the building
blocks that Block 5b (BacktestEngine) and Block 5c (Review) use to
turn raw trade records into KPI reports.

Design contract
---------------
* **Pure functions**: every function takes a ``List[TradeRecord]``
  (or a sub-list, e.g. an equity curve) and returns aggregated
  values. No DB access, no global state, no I/O. This is what makes
  them trivially unit-testable.
* **No double-computation**: ``r_multiple``, ``pnl_realized`` etc.
  are read from the trade record as-stored. We do not re-derive
  them. If a trade is still open (``timestamp_close is None``) it
  is skipped from winrate/avg-R calcs but counted in trade count.
* **Deterministic**: given the same input, the same output (no
  hash-map iteration order dependence, no random sampling).

Bucket convention
-----------------
* R-multiples are bucketed as ``{"-3": n, "-2": n, "-1": n, "0": n,
  "1": n, "2": n, "3+": n}``. ``-3`` covers R <= -3.0 (big losses);
  ``3+`` covers R >= 3.0 (big wins). The 0 bucket is exactly
  R == 0.0 (rounded to two decimal places).
* Score bands follow :class:`ScoreBand` (5 bands). They are returned
  in the same fixed order so JSON output is stable.
* Sessions follow the 5-value session enum. They are returned in a
  stable order: asia → london → ny → overlap → closed.

Conventions for empty input
---------------------------
Every function degrades gracefully on empty input:
* Histogram → empty dict
* Winrate / Avg-R → 0.0 (not NaN — JSON cannot encode NaN cleanly)
* Sharpe → 0.0 (no observations = no signal)
* Max drawdown → (0, None, None)
* Equity curve → empty list
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from xauusd_bot.common.schemas.decision import EntryType, ScoreBand
from xauusd_bot.common.schemas.journal import (
    SessionLiteral,
    TradeRecord,
)

# ----------------------------------------------------------------- helpers


def _closed_trades(trades: Iterable[TradeRecord]) -> list[TradeRecord]:
    """Return only trades with a realized PnL (closed trades)."""

    return [t for t in trades if t.pnl_realized is not None and t.r_multiple is not None]


def _safe_avg(values: list[float]) -> float:
    """Average of a list, or 0.0 for empty input. Never NaN."""

    if not values:
        return 0.0
    return float(sum(values) / len(values))


def _winrate(closed: list[TradeRecord]) -> float:
    """Fraction of closed trades with pnl > 0. Returns 0.0 for empty input."""

    if not closed:
        return 0.0
    wins = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
    return wins / len(closed)


# ----------------------------------------------------------------- R distribution


# R-multiple histogram buckets. Order matters — used for the JSON output.
_R_BUCKETS: list[str] = ["-3", "-2", "-1", "0", "1", "2", "3+"]


def _r_bucket(r: float) -> str:
    """Map an R-multiple to a bucket label."""

    if r <= -3.0:
        return "-3"
    if r <= -2.0:
        return "-2"
    if r <= -1.0:
        return "-1"
    if r < 1.0:
        return "0"
    if r < 2.0:
        return "1"
    if r < 3.0:
        return "2"
    return "3+"


def compute_r_distribution(trades: Iterable[TradeRecord]) -> dict[str, int]:
    """Histogram of closed-trade R-multiples.

    Returns a dict keyed by bucket label. Buckets with zero
    observations are present (so the JSON output is stable across
    runs).
    """

    out: dict[str, int] = {b: 0 for b in _R_BUCKETS}
    for t in _closed_trades(trades):
        assert t.r_multiple is not None  # _closed_trades invariant
        out[_r_bucket(t.r_multiple)] += 1
    return out


# ----------------------------------------------------------------- setup breakdown


# Stable ordering for entry-type breakdown.
_ENTRY_TYPE_ORDER: list[EntryType] = [EntryType.SCOUT, EntryType.REDUCED, EntryType.FULL]


def compute_setup_breakdown(trades: Iterable[TradeRecord]) -> dict[str, dict[str, float]]:
    """Per-entry-type KPI summary.

    Returns a dict ``{entry_type.value: {count, winrate, avg_r, total_r}}``
    for each :class:`EntryType`. Trades that are still open count
    in ``count`` but not in winrate/avg-r (we use the closed subset
    for those).

    Output keys (per entry type):
    * ``count`` — total trades (open + closed)
    * ``closed`` — closed-trade count
    * ``wins`` — closed-trade wins
    * ``losses`` — closed-trade losses
    * ``breakeven`` — closed trades with pnl == 0
    * ``winrate`` — wins / closed
    * ``avg_r`` — mean r_multiple over closed trades
    * ``total_r`` — sum of r_multiples (a proxy for total expectancy)
    * ``total_pnl`` — sum of pnl_realized in USD
    """

    by_type: dict[EntryType, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_type[t.entry_type].append(t)

    out: dict[str, dict[str, float]] = {}
    for et in _ENTRY_TYPE_ORDER:
        group = by_type.get(et, [])
        closed = _closed_trades(group)
        wins = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
        losses = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
        breakeven = sum(1 for t in closed if t.pnl_realized == 0)
        rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
        pnls = [float(t.pnl_realized) for t in closed if t.pnl_realized is not None]
        out[et.value] = {
            "count": float(len(group)),
            "closed": float(len(closed)),
            "wins": float(wins),
            "losses": float(losses),
            "breakeven": float(breakeven),
            "winrate": _winrate(closed),
            "avg_r": _safe_avg(rs),
            "total_r": float(sum(rs)),
            "total_pnl": float(sum(pnls)),
        }
    return out


# ----------------------------------------------------------------- equity curve


def compute_equity_curve(trades: Iterable[TradeRecord]) -> list[tuple[datetime, Decimal]]:
    """Cumulative realized PnL over time, ordered by close time.

    Returns a list of ``(timestamp_close, cumulative_pnl)`` tuples.
    The first entry is the first closed trade's pnl, each subsequent
    entry adds the next trade's pnl.

    Notes
    -----
    * Open trades (no ``pnl_realized``) are skipped.
    * If a ``TradeRecord`` has ``timestamp_close is None`` but
      ``pnl_realized is not None`` (defensive), the open time is
      used as a fallback to keep the curve total-ordered.
    """

    closed = sorted(
        _closed_trades(trades),
        key=lambda t: t.timestamp_close or t.timestamp_open,
    )
    out: list[tuple[datetime, Decimal]] = []
    cum = Decimal("0")
    for t in closed:
        assert t.pnl_realized is not None
        cum += t.pnl_realized
        ts = t.timestamp_close or t.timestamp_open
        out.append((ts, cum))
    return out


# ----------------------------------------------------------------- sharpe


def compute_sharpe(
    equity_curve: list[tuple[datetime, Decimal]],
    *,
    risk_free: float = 0.0,
    periods_per_year: int = 252 * 8,  # 252 trading days × 8 hourly bars; XAUUSD-specific default
) -> float:
    """Annualized Sharpe ratio of an equity curve.

    Method
    ------
    1. Take the per-step (per-bar) returns ``r_i = (equity_i -
       equity_{i-1}) / |equity_{i-1}|``.
    2. Mean and std-dev of the per-step returns.
    3. ``sharpe = (mean - risk_free) / std * sqrt(periods_per_year)``.

    Edge cases
    ----------
    * Fewer than 2 points → 0.0 (no signal).
    * Zero variance in returns → 0.0 (denominator is zero, so
      Sharpe is undefined; we return 0 for stability).
    * Negative starting equity is not expected (the journal only
      stores trade pnl, not account equity) but the function
      degrades gracefully.
    """

    if len(equity_curve) < 2:
        return 0.0
    # We need absolute magnitudes of equity at each step to compute
    # returns. Negative equity is pathological but possible in a
    # blown account; use abs() to avoid sign-flip artefacts.
    equities = [float(abs(eq)) for _, eq in equity_curve]
    returns: list[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev <= 0:
            continue
        ret = (equities[i] - prev) / prev
        returns.append(ret)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0 or not math.isfinite(var):
        return 0.0
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return float((mean - risk_free) / std * math.sqrt(periods_per_year))


# ----------------------------------------------------------------- max drawdown


def compute_max_drawdown(
    equity_curve: list[tuple[datetime, Decimal]],
) -> tuple[Decimal, datetime | None, datetime | None]:
    """Largest peak-to-trough drawdown in the equity curve.

    Returns ``(max_dd_amount, peak_time, trough_time)``.

    * ``max_dd_amount`` is always non-negative (= peak - trough).
    * If the curve is empty or monotonically rising, returns
      ``(Decimal('0'), None, None)``.
    * ``peak_time`` is the timestamp of the highest equity before
      the drawdown started; ``trough_time`` is the lowest point
      observed in the drawdown (which is what an investor would
      have *experienced* at the time — not the recovery point).
    """

    if not equity_curve:
        return (Decimal("0"), None, None)

    peak_eq = equity_curve[0][1]
    peak_ts: datetime | None = equity_curve[0][0]
    best_dd = Decimal("0")
    best_peak_ts: datetime | None = None
    best_trough_ts: datetime | None = None

    for ts, eq in equity_curve:
        if eq > peak_eq:
            peak_eq = eq
            peak_ts = ts
        dd = peak_eq - eq
        if dd > best_dd:
            best_dd = dd
            best_peak_ts = peak_ts
            best_trough_ts = ts

    return (best_dd, best_peak_ts, best_trough_ts)


# ----------------------------------------------------------------- session stats


_SESSION_ORDER: list[SessionLiteral] = ["asia", "london", "ny", "overlap", "closed"]


def compute_session_stats(trades: Iterable[TradeRecord]) -> dict[str, dict[str, float]]:
    """Per-session KPI summary.

    Output keys (per session) mirror the setup-breakdown shape:
    ``count, closed, wins, losses, winrate, avg_r, total_r, total_pnl``.
    """

    by_session: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_session[t.session].append(t)

    out: dict[str, dict[str, float]] = {}
    for s in _SESSION_ORDER:
        group = by_session.get(s, [])
        closed = _closed_trades(group)
        wins = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
        losses = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
        rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
        pnls = [float(t.pnl_realized) for t in closed if t.pnl_realized is not None]
        out[s] = {
            "count": float(len(group)),
            "closed": float(len(closed)),
            "wins": float(wins),
            "losses": float(losses),
            "winrate": _winrate(closed),
            "avg_r": _safe_avg(rs),
            "total_r": float(sum(rs)),
            "total_pnl": float(sum(pnls)),
        }
    return out


# ----------------------------------------------------------------- score-band stats


_BAND_ORDER: list[ScoreBand] = [
    ScoreBand.BELOW_55,
    ScoreBand.OBSERVE_55_64,
    ScoreBand.PREPARE_65_74,
    ScoreBand.REDUCED_75_84,
    ScoreBand.FULL_85_PLUS,
]


def compute_score_band_stats(trades: Iterable[TradeRecord]) -> dict[str, dict[str, float]]:
    """Per-score-band KPI summary.

    Output keys (per band) mirror the setup/session shape:
    ``count, closed, wins, losses, winrate, avg_r, total_r, total_pnl``.

    Note: every trade was already filtered by the score band at
    *open* time, so each trade belongs to exactly one band.
    """

    by_band: dict[ScoreBand, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        by_band[t.band].append(t)

    out: dict[str, dict[str, float]] = {}
    for band in _BAND_ORDER:
        group = by_band.get(band, [])
        closed = _closed_trades(group)
        wins = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
        losses = sum(1 for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
        rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
        pnls = [float(t.pnl_realized) for t in closed if t.pnl_realized is not None]
        out[band.value] = {
            "count": float(len(group)),
            "closed": float(len(closed)),
            "wins": float(wins),
            "losses": float(losses),
            "winrate": _winrate(closed),
            "avg_r": _safe_avg(rs),
            "total_r": float(sum(rs)),
            "total_pnl": float(sum(pnls)),
        }
    return out


# ----------------------------------------------------------------- re-exports

# ----------------------------------------------------------------- sortino


def compute_sortino(
    equity_curve: list[tuple[datetime, Decimal]],
    *,
    risk_free: float = 0.0,
    periods_per_year: int = 252 * 8,
) -> float:
    """Annualized Sortino ratio of an equity curve.

    Method
    ------
    1. Take the per-step returns ``r_i = (equity_i - equity_{i-1})
       / |equity_{i-1}|``.
    2. ``mean_return = mean(r)``.
    3. ``downside_deviation = sqrt(mean(min(r, 0)^2))`` — only
       negative returns contribute.
    4. ``sortino = (mean - risk_free) / downside * sqrt(periods_per_year)``.

    Edge cases
    ----------
    * Fewer than 2 points → 0.0.
    * No negative returns → 0.0 (downside is zero, ratio undefined).
    * Zero variance → 0.0.
    """

    if len(equity_curve) < 2:
        return 0.0
    equities = [float(abs(eq)) for _, eq in equity_curve]
    returns: list[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev <= 0:
            continue
        ret = (equities[i] - prev) / prev
        returns.append(ret)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    downside_var = sum(r * r for r in downside) / len(downside)
    if downside_var <= 0 or not math.isfinite(downside_var):
        return 0.0
    std_down = math.sqrt(downside_var)
    if std_down == 0:
        return 0.0
    return float((mean - risk_free) / std_down * math.sqrt(periods_per_year))


# ----------------------------------------------------------------- re-exports


__all__ = [
    "compute_equity_curve",
    "compute_max_drawdown",
    "compute_r_distribution",
    "compute_score_band_stats",
    "compute_session_stats",
    "compute_setup_breakdown",
    "compute_sharpe",
    "compute_sortino",
]
