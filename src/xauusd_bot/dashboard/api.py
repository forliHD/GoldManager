"""REST API surface for the dashboard (Block 9).

All endpoints are mounted under ``/api``. The router is registered in
:func:`xauusd_bot.dashboard.app.create_app`.

Endpoint overview
-----------------
* ``/api/auth/login``, ``/api/auth/logout``, ``/api/auth/me``
* ``/api/health`` — always 200 if the app is up.
* ``/api/chart/candles`` and ``/api/chart/overlays``
* ``/api/journal/trades`` and ``/api/journal/aggregate``
* ``/api/backtest/list``, ``/api/backtest/run``, ``/api/backtest/status``
* ``/api/review/daily`` and ``/api/review/weekly``
* ``/api/fitting-proposal/list``, ``/api/fitting-proposal/approve``,
  ``/api/fitting-proposal/reject``, ``/api/fitting-proposal/validate``
* ``/api/mode/toggle``

Hard rules (see AGENTS.md §4j)
------------------------------
* When :attr:`Settings.dashboard_enabled` is False, every endpoint
  except ``/api/health`` returns 404 — implemented as a top-level
  middleware check in :mod:`xauusd_bot.dashboard.app` so we don't
  scatter conditionals across the router.
* Plaintext passwords are accepted ONLY on ``/api/auth/login`` and
  are NEVER logged (structlog field is the username only).
* ``/api/mode/toggle`` requires ``admin`` role AND
  :attr:`Settings.dashboard_live_mode_enabled` is True. Otherwise 403.
* ``/api/fitting-proposal/approve|reject`` require ``operator`` role.
* The mode toggle writes ``settings.connector_mode`` to a Redis cache
  key ``dashboard:connector_mode`` so the trading process can hot-
  reload (Block-10 follow-up).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from xauusd_bot.common.config import ConnectorMode, Settings
from xauusd_bot.common.runtime_config import (
    STATE_KEY_ACCOUNT,
    STATE_KEY_POSITIONS,
    STATE_KEY_RISK,
    get_ai_enabled,
    get_emergency_stop,
    get_json,
    get_llm_usage,
    set_ai_enabled,
    set_emergency_stop,
)
from xauusd_bot.common.schemas.review import (
    FittingProposal,
    FittingProposalFilter,
    ReviewRun,
)
from xauusd_bot.dashboard.auth import (
    DashboardAuth,
    InvalidCredentialsError,
    SESSION_COOKIE,
    UserSession,
    current_session,
    require_role,
)

log = structlog.get_logger(__name__)
_ = logging

router = APIRouter(prefix="/api", tags=["dashboard"])

# Redis key for the cached connector mode (mode/toggle writes here).
_CONNECTOR_MODE_KEY = "dashboard:connector_mode"

# In-process backtest task registry (TaskID → status). For Block 9 a
# simple in-memory dict is enough — for production a Redis-backed
# queue is the Block-10 follow-up.
_backtest_tasks: dict[str, dict[str, Any]] = {}
_backtest_tasks_lock = asyncio.Lock()


# ============================================================ Auth endpoints


@router.post("/auth/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> JSONResponse:
    """Authenticate and set the session cookie.

    Accepts ``application/x-www-form-urlencoded`` (the canonical
    browser-login shape — saves us a JSON parsing edge case in
    curl/script tests).
    """

    auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if auth is None or settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard auth not initialized",
        )
    if not auth.verify_password(username, password):
        # Log only the username (NEVER the password).
        log.info("dashboard_login_failed", username=username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
        )
    try:
        session = await auth.create_session(username)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    log.info("dashboard_login_success", username=username, role=session.role)
    response = JSONResponse(
        status_code=200,
        content={
            "session_id": session.session_id,
            "username": session.username,
            "role": session.role,
            "created_at": session.created_at.isoformat(),
        },
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session.session_id,
        max_age=settings.dashboard_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        path="/",
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke the session and clear the cookie (idempotent)."""

    cookie_value = request.cookies.get(SESSION_COOKIE)
    auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
    if auth is not None and cookie_value:
        await auth.revoke_session(cookie_value)
    log.info("dashboard_logout", session_id=cookie_value)
    response = JSONResponse(status_code=200, content={"status": "ok"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/me")
async def me(session: UserSession = Depends(current_session)) -> dict[str, Any]:
    """Return the current session (for client-side role display)."""

    return {
        "session_id": session.session_id,
        "username": session.username,
        "role": session.role,
        "created_at": session.created_at.isoformat(),
        "last_seen": session.last_seen.isoformat(),
    }


# ============================================================ Health


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Always-200 health endpoint (even if dashboard_enabled is False)."""

    settings: Settings | None = getattr(request.app.state, "settings", None)
    return {
        "status": "ok",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "version": "block9-0.1.0",
        "dashboard_enabled": bool(settings and settings.dashboard_enabled),
        # The configured trading symbol + connector mode so the frontend
        # requests the right instrument (e.g. "XAUUSD+") instead of a
        # hard-coded "XAUUSD", and shows the live/replay badge correctly.
        "symbol": (settings.symbol if settings else "XAUUSD"),
        "mode": (settings.connector_mode.value if settings else "replay"),
    }


# ============================================================ Chart


class CandleDict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int


class OverlayLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vah: float | None
    vpoc: float | None
    val: float | None
    state: Literal["developing", "locked"] = "locked"


class OverlayDict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timestamp: datetime
    vwaps: dict[str, float | None]
    volume_profile: dict[str, OverlayLevel]
    fvg_zones: list[dict[str, Any]]


# Timeframe → minutes per bar. The data-collector feeds M1; higher
# timeframes are aggregated on read.
_TF_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}


def _resample_candles(candles: list[CandleDict], minutes: int) -> list[CandleDict]:
    """Aggregate ascending M1 candles into ``minutes``-sized OHLC buckets."""

    if minutes <= 1 or not candles:
        return candles
    bucket = minutes * 60
    groups: dict[int, list[CandleDict]] = {}
    for c in candles:
        key = (int(c.time.timestamp()) // bucket) * bucket
        groups.setdefault(key, []).append(c)
    out: list[CandleDict] = []
    for key in sorted(groups):
        g = groups[key]  # ascending within the bucket
        out.append(
            CandleDict(
                time=datetime.fromtimestamp(key, tz=UTC),
                open=g[0].open,
                high=max(x.high for x in g),
                low=min(x.low for x in g),
                close=g[-1].close,
                tick_volume=sum(x.tick_volume for x in g),
            )
        )
    return out


@router.get("/chart/candles")
async def chart_candles(
    request: Request,
    symbol: str = "XAUUSD",
    timeframe: str = "M1",
    count: int = Query(default=500, ge=1, le=5000),
    session: UserSession = Depends(current_session),
) -> list[CandleDict]:
    """Return the last ``count`` candles for ``symbol``.

    Primary source is the ``market_ticks`` Redis stream — the live bar
    feed the data-collector publishes. Bars are de-duplicated by time
    (the replay loop re-emits the same bars) and returned chronologically.
    Falls back to the journal's feature snapshots when the stream is
    empty/unavailable.
    """

    minutes = _TF_MINUTES.get(timeframe.upper(), 1)
    # --- Primary: read closed M1 bars straight from the market_ticks stream.
    stream_redis = getattr(request.app.state, "streams_redis", None)
    if stream_redis is not None:
        # Pull enough M1 bars to yield ~count candles at the target timeframe.
        fetch_n = min(count * minutes * 2, 50000)
        try:
            entries = await stream_redis.xrevrange("market_ticks", count=fetch_n)
        except Exception as exc:  # noqa: BLE001 - fall through to the journal store
            log.warning("chart_candles_stream_read_failed", error=str(exc))
            entries = []
        by_time: dict[Any, CandleDict] = {}
        for _entry_id, fields in entries:
            raw = fields.get("payload")
            if not raw:
                continue
            try:
                bar = (json.loads(raw).get("bar")) or {}
                if bar.get("symbol") != symbol:
                    continue
                candle = CandleDict(
                    time=bar["time"],
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    tick_volume=int(bar.get("tick_volume", 0)),
                )
                by_time[candle.time] = candle  # last write wins (identical OHLC per loop)
            except (KeyError, TypeError, ValueError):
                continue
        if by_time:
            m1 = sorted(by_time.values(), key=lambda c: c.time)
            return _resample_candles(m1, minutes)[-count:]

    # --- Fallback: journal feature snapshots (carry OHLC when persisted).
    store = _get_journal_store()
    end = datetime.now(tz=UTC)
    start = end - timedelta(days=max(int(count) // 1440 + 2, 7))
    snaps = await store.list_snapshots(start=start, end=end, symbol=symbol, limit=count)
    out: list[CandleDict] = []
    for s in snaps:
        f = s.features or {}
        ohlc = f.get("ohlc") if isinstance(f.get("ohlc"), dict) else None
        if ohlc is None:
            continue
        try:
            out.append(
                CandleDict(
                    time=s.bar_time,
                    open=float(ohlc["open"]),
                    high=float(ohlc["high"]),
                    low=float(ohlc["low"]),
                    close=float(ohlc["close"]),
                    tick_volume=int(ohlc.get("tick_volume", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c.time)
    return out[-count:]


@router.get("/chart/overlays")
async def chart_overlays(
    symbol: str = "XAUUSD",
    session: UserSession = Depends(current_session),
) -> OverlayDict:
    """Return the most-recent overlay snapshot for ``symbol``.

    The overlay is the JSON payload the Block-7
    :mod:`xauusd_bot.viz.overlay_writer` writes to
    ``MQL5/Files/overlay_levels.json``. We read the most-recent one
    embedded in the journal's feature snapshot features.
    """

    store = _get_journal_store()
    end = datetime.now(tz=UTC)
    start = end - timedelta(days=2)
    snaps = await store.list_snapshots(start=start, end=end, symbol=symbol, limit=200)
    overlays = [s for s in snaps if isinstance(s.features.get("overlays"), dict)]
    overlays.sort(key=lambda s: s.bar_time, reverse=True)
    if not overlays:
        return OverlayDict(
            symbol=symbol,
            timestamp=datetime.now(tz=UTC),
            vwaps={"utc00": None, "utc07": None, "utc12": None},
            volume_profile={},
            fvg_zones=[],
        )
    latest = overlays[0].features["overlays"]
    # build_overlay_payload emits the VWAP section under "vwap" (singular); the
    # API response field is "vwaps". Read the payload key, not the response key.
    vwaps = latest.get("vwap", latest.get("vwaps", {"utc00": None, "utc07": None, "utc12": None}))
    vp = latest.get("volume_profile", {})
    fvg = latest.get("fvg_zones", [])
    return OverlayDict(
        symbol=symbol,
        timestamp=overlays[0].bar_time,
        vwaps=vwaps,
        volume_profile={
            k: OverlayLevel(
                vah=(v.get("vah") if v else None),
                vpoc=(v.get("vpoc") if v else None),
                val=(v.get("val") if v else None),
                state=v.get("state", "locked") if v else "locked",
            )
            for k, v in vp.items()
        },
        fvg_zones=list(fvg) if isinstance(fvg, list) else [],
    )


# ============================================================ Journal


@router.get("/journal/trades")
async def journal_trades(
    limit: int = Query(default=20, ge=1, le=1000),
    symbol: str = "XAUUSD",
    session: UserSession = Depends(current_session),
) -> list[dict[str, Any]]:
    """List the most recent trades for the dashboard table."""

    store = _get_journal_store()
    trades = await store.list_trades(symbol=symbol, limit=limit)
    trades.sort(key=lambda t: t.timestamp_open, reverse=True)
    out: list[dict[str, Any]] = []
    for t in trades:
        out.append(
            {
                "id": str(t.id),
                "timestamp_open": t.timestamp_open.isoformat(),
                "timestamp_close": (
                    t.timestamp_close.isoformat() if t.timestamp_close else None
                ),
                "symbol": t.symbol,
                "side": t.side,
                "entry": float(t.entry_price),
                "sl": float(t.stop_loss),
                "tp": [float(x) for x in t.take_profits],
                "exit": float(t.exit_price) if t.exit_price is not None else None,
                "pnl_r": t.r_multiple,
                "pnl_realized": float(t.pnl_realized) if t.pnl_realized is not None else None,
                "score": t.score,
                "band": t.band.value,
                "entry_type": t.entry_type.value,
                "engine_source": t.engine_source,
                "decision_kind": "ai" if t.engine_source == "ai" else "rule",
                "llm_used": t.engine_source == "ai",
                "exit_reason": t.exit_reason.value if t.exit_reason else None,
            }
        )
    return out


@router.get("/journal/aggregate")
async def journal_aggregate(
    period: str = Query(default="last_week"),
    session: UserSession = Depends(current_session),
) -> dict[str, Any]:
    """Aggregate KPIs over the requested period.

    Period tokens: ``today``, ``last_week``, ``last_month``,
    ``ytd``, ``all``. We deliberately keep this simple — the
    Block-9+ follow-up adds a real date-range picker.
    """

    store = _get_journal_store()
    end = datetime.now(tz=UTC)
    if period == "today":
        start = datetime(end.year, end.month, end.day, tzinfo=UTC)
    elif period == "last_week":
        start = end - timedelta(days=7)
    elif period == "last_month":
        start = end - timedelta(days=30)
    elif period == "ytd":
        start = datetime(end.year, 1, 1, tzinfo=UTC)
    elif period == "all":
        start = datetime(1970, 1, 1, tzinfo=UTC)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unknown period: {period!r}",
        )
    trades = await store.list_trades(start=start, end=end, limit=10_000)

    from xauusd_bot.journal.queries import (
        compute_equity_curve,
        compute_max_drawdown,
        compute_r_distribution,
        compute_session_stats,
        compute_setup_breakdown,
        compute_sharpe,
        compute_sortino,
    )

    equity_curve = compute_equity_curve(trades)
    sharpe = compute_sharpe(equity_curve)
    sortino = compute_sortino(equity_curve)
    max_dd, _, _ = compute_max_drawdown(equity_curve)
    r_dist = compute_r_distribution(trades)

    # Sample the equity curve to ~60 points for a compact sparkline.
    ec = [(t.isoformat(), float(eq)) for t, eq in equity_curve]
    if len(ec) > 60:
        step = max(1, len(ec) // 60)
        ec = ec[::step][:60]

    def _safe(fn):
        try:
            return fn(trades)
        except Exception:  # noqa: BLE001 - breakdowns are best-effort enrichment
            return {}

    setup_breakdown = _safe(compute_setup_breakdown)
    session_stats = _safe(compute_session_stats)

    closed = [t for t in trades if t.r_multiple is not None and t.pnl_realized is not None]
    wins = sum(1 for t in closed if (t.pnl_realized or Decimal("0")) > 0)
    losses = sum(1 for t in closed if (t.pnl_realized or Decimal("0")) < 0)
    winrate = wins / len(closed) if closed else 0.0
    total_pnl = float(sum((t.pnl_realized or Decimal("0")) for t in closed))
    pos_sum = float(sum(t.pnl_realized for t in closed if (t.pnl_realized or Decimal("0")) > 0))
    neg_sum = float(
        sum(-(t.pnl_realized or Decimal("0")) for t in closed if (t.pnl_realized or Decimal("0")) < 0)
    )
    profit_factor = pos_sum / neg_sum if neg_sum > 0 else 0.0
    avg_r = sum(t.r_multiple or 0.0 for t in closed) / len(closed) if closed else 0.0

    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_trades": len(trades),
        "n_closed": len(closed),
        "n_wins": wins,
        "n_losses": losses,
        "winrate": winrate,
        "expectancy": avg_r,
        "avg_r": avg_r,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "r_distribution": r_dist,
        "equity_curve": ec,
        "setup_breakdown": setup_breakdown,
        "session_stats": session_stats,
    }


# ============================================================ Backtest


class BacktestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: datetime = Field(description="Window start (UTC, inclusive).")
    end_date: datetime = Field(description="Window end (UTC, exclusive).")
    warmup_bars: int = Field(default=500, ge=10, le=5000)
    max_bars: int | None = Field(default=None, ge=10, le=200_000)
    walk_forward: bool = Field(default=False)
    in_sample_days: int = Field(default=14, ge=1, le=365)
    out_of_sample_days: int = Field(default=7, ge=1, le=365)
    step_days: int = Field(default=7, ge=1, le=365)


class BacktestStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["running", "completed", "failed"]
    progress_percent: float = Field(ge=0, le=100)
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class BacktestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    started_at: datetime
    period_start: datetime
    period_end: datetime
    n_trades: int
    sharpe: float
    is_overfit: bool
    status: str


@router.get("/backtest/list")
async def backtest_list(
    session: UserSession = Depends(current_session),
) -> list[BacktestMetadata]:
    """List backtest runs the dashboard has triggered (in-process registry)."""

    async with _backtest_tasks_lock:
        items = list(_backtest_tasks.values())
    items.sort(key=lambda t: t.get("started_at", datetime.min), reverse=True)
    out: list[BacktestMetadata] = []
    for t in items:
        result = t.get("result") or {}
        stats = (result.get("stats") or {}) if isinstance(result, dict) else {}
        out.append(
            BacktestMetadata(
                id=t["task_id"],
                started_at=t["started_at"],
                period_start=t.get("period_start", t["started_at"]),
                period_end=t.get("period_end", t["started_at"]),
                n_trades=int(stats.get("n_trades", 0)),
                sharpe=float(stats.get("sharpe", 0.0)),
                is_overfit=bool(result.get("is_overfit", False)),
                status=t.get("status", "running"),
            )
        )
    return out


@router.post("/backtest/run")
async def backtest_run(
    request: Request,
    body: BacktestRunRequest,
    background_tasks: BackgroundTasks,
    session: UserSession = Depends(current_session),
) -> dict[str, str]:
    """Start a backtest as a background task; return task_id for polling.

    Operators only (or above). The actual BacktestEngine.run is invoked
    in :func:`_run_backtest_task` so the request returns immediately.
    """

    if session.role == "viewer":
        raise HTTPException(status_code=403, detail="viewer cannot trigger backtests")
    task_id = uuid.uuid4().hex
    now = datetime.now(tz=UTC)
    async with _backtest_tasks_lock:
        _backtest_tasks[task_id] = {
            "task_id": task_id,
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "progress_percent": 0.0,
            "period_start": body.start_date,
            "period_end": body.end_date,
            "result": None,
            "error": None,
        }
    background_tasks.add_task(_run_backtest_task, request.app, task_id, body)
    log.info(
        "backtest_triggered",
        task_id=task_id,
        username=session.username,
        start=body.start_date.isoformat(),
        end=body.end_date.isoformat(),
        walk_forward=body.walk_forward,
    )
    return {"task_id": task_id, "status": "running"}


@router.get("/backtest/status")
async def backtest_status(
    task_id: str = Query(...),
    session: UserSession = Depends(current_session),
) -> BacktestStatus:
    """Poll status of a previously-started backtest task."""

    async with _backtest_tasks_lock:
        t = _backtest_tasks.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"unknown task_id: {task_id}")
    return BacktestStatus(
        task_id=task_id,
        status=t.get("status", "running"),
        progress_percent=float(t.get("progress_percent", 0.0)),
        started_at=t["started_at"],
        finished_at=t.get("finished_at"),
        error=t.get("error"),
        result=t.get("result"),
    )


async def _run_backtest_task(app, task_id: str, body: BacktestRunRequest) -> None:
    """Background-task runner: invokes BacktestEngine.run and persists result."""

    from xauusd_bot.backtest.engine import BacktestEngine

    try:
        connector_factory = getattr(app.state, "replay_connector_factory", None)
        if connector_factory is None:
            raise RuntimeError(
                "replay_connector_factory not configured on app.state; "
                "cannot run backtests in dashboard mode without sample data."
            )
        connector = connector_factory()
        journal = _get_journal_store()
        engine = BacktestEngine(connector=connector, journal=journal)
        if body.walk_forward:
            from xauusd_bot.backtest.walkforward import WalkForwardEngine

            wf = WalkForwardEngine(
                engine=engine,
                in_sample_days=body.in_sample_days,
                out_of_sample_days=body.out_of_sample_days,
                step_days=body.step_days,
            )
            wf_result = wf.run(
                start_date=body.start_date,
                end_date=body.end_date,
                warmup_bars=body.warmup_bars,
            )
            payload: dict[str, Any] = {
                "type": "walk_forward",
                "n_windows": wf_result.n_windows,
                "is_overfit": wf_result.is_overfit,
                "robustness_matrix": wf_result.robustness_matrix,
                "windows": [
                    {
                        "window_index": w.window_index,
                        "in_sample_sharpe": w.in_sample_sharpe,
                        "out_of_sample_sharpe": w.out_of_sample_sharpe,
                        "oos_degradation_pct": w.oos_degradation_pct,
                        "n_trades_is": w.in_sample_stats.n_trades,
                        "n_trades_oos": w.out_of_sample_stats.n_trades,
                    }
                    for w in wf_result.windows
                ],
            }
        else:
            result = engine.run(
                start_date=body.start_date,
                end_date=body.end_date,
                warmup_bars=body.warmup_bars,
                max_bars=body.max_bars,
            )
            payload = {
                "type": "single",
                "n_bars_processed": result.n_bars_processed,
                "n_trades": result.n_trades,
                "stats": result.stats.model_dump(),
            }
        async with _backtest_tasks_lock:
            _backtest_tasks[task_id].update(
                {
                    "status": "completed",
                    "progress_percent": 100.0,
                    "finished_at": datetime.now(tz=UTC),
                    "result": payload,
                }
            )
        log.info("backtest_completed", task_id=task_id)
    except Exception as exc:  # noqa: BLE001
        async with _backtest_tasks_lock:
            _backtest_tasks[task_id].update(
                {
                    "status": "failed",
                    "finished_at": datetime.now(tz=UTC),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        log.error(
            "backtest_failed",
            task_id=task_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )


# ============================================================ Review


@router.get("/review/daily")
async def review_daily(
    day: date = Query(...),
    session: UserSession = Depends(current_session),
) -> ReviewRun:
    """Run (or fetch) the daily review for ``day``."""

    engine = _get_review_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="review engine not configured")
    return await engine.run_daily(day)


@router.get("/review/weekly")
async def review_weekly(
    week_start: date = Query(...),
    session: UserSession = Depends(current_session),
) -> ReviewRun:
    """Run (or fetch) the weekly review for the 7-day window starting ``week_start``."""

    engine = _get_review_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="review engine not configured")
    return await engine.run_weekly(week_start)


# ============================================================ FittingProposal


class FittingProposalApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    operator: str | None = Field(default=None, description="Defaults to session username.")
    note: str | None = None


class FittingProposalRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    operator: str | None = None
    note: str | None = None


class FittingProposalValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID


@router.post("/fitting-proposal/list")
async def fitting_proposal_list(
    body: FittingProposalFilter,
    session: UserSession = Depends(current_session),
) -> list[FittingProposal]:
    """List fitting proposals (with optional filter)."""

    engine = _get_fitting_proposal_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="fitting-proposal engine not configured")
    return await engine.list_proposals(filter=body)


@router.post("/fitting-proposal/approve")
async def fitting_proposal_approve(
    body: FittingProposalApproveRequest,
    session: UserSession = Depends(require_role("operator")),
) -> FittingProposal:
    """Approve a fitting proposal (operator+ only — see AGENTS.md §4j.4)."""

    engine = _get_fitting_proposal_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="fitting-proposal engine not configured")
    operator = body.operator or session.username
    return await engine.approve(
        body.proposal_id,
        operator=operator,
        note=body.note,
    )


@router.post("/fitting-proposal/reject")
async def fitting_proposal_reject(
    body: FittingProposalRejectRequest,
    session: UserSession = Depends(require_role("operator")),
) -> FittingProposal:
    """Reject a fitting proposal (operator+ only)."""

    engine = _get_fitting_proposal_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="fitting-proposal engine not configured")
    operator = body.operator or session.username
    return await engine.reject(
        body.proposal_id,
        operator=operator,
        note=body.note,
    )


@router.post("/fitting-proposal/validate")
async def fitting_proposal_validate(
    body: FittingProposalValidateRequest,
    session: UserSession = Depends(require_role("operator")),
) -> FittingProposal:
    """Run the validation backtest for a proposal (operator+ only)."""

    engine = _get_fitting_proposal_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="fitting-proposal engine not configured")
    existing = await engine.get(body.proposal_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="fitting proposal not found")
    return await engine.run_validation(existing)


# ============================================================ Mode toggle


class ModeToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_mode: Literal["replay", "live"]
    confirm: bool = Field(
        default=False,
        description="Must be true. Defensive: prevents accidental toggles.",
    )


class ModeToggleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    previous_mode: str
    new_mode: str
    operator: str
    timestamp: datetime
    redis_key: str


@router.post("/mode/toggle")
async def mode_toggle(
    request: Request,
    body: ModeToggleRequest,
    session: UserSession = Depends(require_role("admin")),
) -> ModeToggleResult:
    """Switch the connector mode (replay ↔ live). Admin-only AND
    requires :attr:`Settings.dashboard_live_mode_enabled` is True.

    The new mode is persisted in Redis under
    ``dashboard:connector_mode`` so the trading process can hot-reload
    (Block-10 follow-up — for now a restart suffices).
    """

    settings: Settings = request.app.state.settings
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required to toggle the connector mode",
        )
    if body.target_mode == "live" and not settings.dashboard_live_mode_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "live-mode toggle is disabled in settings "
                "(set DASHBOARD_LIVE_MODE_ENABLED=true to enable)"
            ),
        )
    redis_client = getattr(request.app.state, "dashboard_redis", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="dashboard redis not initialized")

    previous = settings.connector_mode.value
    await redis_client.set(_CONNECTOR_MODE_KEY, body.target_mode)
    log.warning(
        "connector_mode_toggled",
        previous_mode=previous,
        new_mode=body.target_mode,
        operator=session.username,
        role=session.role,
    )
    return ModeToggleResult(
        previous_mode=previous,
        new_mode=body.target_mode,
        operator=session.username,
        timestamp=datetime.now(tz=UTC),
        redis_key=_CONNECTOR_MODE_KEY,
    )


# ---------------------------------------------------------------- AI layer toggle


class AIToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="Turn the AI decision layer on (True) or off (False).")


class AIStateResult(BaseModel):
    enabled: bool = Field(description="Current effective runtime state.")
    available: bool = Field(description="Whether the AI layer can run at all (OPENROUTER_API_KEY set).")
    model: str = Field(description="Configured OpenRouter model string.")
    default: bool = Field(description="The static settings default the services start from.")


def _streams_redis(request: Request):
    client = getattr(request.app.state, "streams_redis", None)
    if client is None:
        raise HTTPException(status_code=503, detail="trading redis not initialized")
    return client


@router.get("/ai/state")
async def ai_state(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> AIStateResult:
    """Report the live AI-layer toggle state (any authenticated user)."""

    settings: Settings = request.app.state.settings
    enabled = await get_ai_enabled(_streams_redis(request), default=settings.ai_layer_enabled)
    return AIStateResult(
        enabled=enabled,
        available=settings.openrouter_api_key is not None,
        model=settings.openrouter_model,
        default=settings.ai_layer_enabled,
    )


@router.post("/ai/toggle")
async def ai_toggle(
    request: Request,
    body: AIToggleRequest,
    session: UserSession = Depends(require_role("operator")),
) -> AIStateResult:
    """Flip the AI decision layer on/off at runtime (operator or admin).

    Writes ``runtime:ai_layer_enabled`` on the trading Redis; the
    decision-engine picks it up within a couple of seconds — no restart.
    Turning it *on* only has effect if ``OPENROUTER_API_KEY`` is set on
    the services (otherwise they stay on the rule fallback).
    """

    settings: Settings = request.app.state.settings
    redis_client = _streams_redis(request)
    await set_ai_enabled(redis_client, body.enabled)
    log.warning(
        "ai_layer_toggled",
        enabled=body.enabled,
        operator=session.username,
        role=session.role,
    )
    return AIStateResult(
        enabled=body.enabled,
        available=settings.openrouter_api_key is not None,
        model=settings.openrouter_model,
        default=settings.ai_layer_enabled,
    )


# ---------------------------------------------------------------- live ops state
#
# The execution-engine publishes account / positions / risk snapshots to
# Redis (TTL'd). These endpoints just surface the latest snapshot — an
# empty/null result means the publisher hasn't run (e.g. execution-engine
# down) rather than "zero".


@router.get("/account")
async def account_state(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> dict[str, Any]:
    """Latest account snapshot (balance/equity/margin/PnL)."""

    return await get_json(_streams_redis(request), STATE_KEY_ACCOUNT) or {}


@router.get("/positions")
async def positions_state(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> list[dict[str, Any]]:
    """Currently open positions (live blotter)."""

    return await get_json(_streams_redis(request), STATE_KEY_POSITIONS) or []


@router.get("/risk")
async def risk_state(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> dict[str, Any]:
    """Risk usage snapshot: daily/weekly PnL vs caps, position counts."""

    return await get_json(_streams_redis(request), STATE_KEY_RISK) or {}


class EmergencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engaged: bool = Field(description="Engage (True) or clear (False) the kill-switch.")


@router.get("/emergency")
async def emergency_state(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> dict[str, bool]:
    """Whether the operator kill-switch is currently engaged."""

    return {"engaged": await get_emergency_stop(_streams_redis(request))}


@router.post("/emergency")
async def emergency_toggle(
    request: Request,
    body: EmergencyRequest,
    session: UserSession = Depends(require_role("operator")),
) -> dict[str, bool]:
    """Engage/clear the kill-switch (operator+). The execution-engine acts on it."""

    await set_emergency_stop(_streams_redis(request), body.engaged)
    log.warning(
        "emergency_stop_toggled", engaged=body.engaged, operator=session.username, role=session.role
    )
    return {"engaged": body.engaged}


# ---------------------------------------------------------------- LLM usage / cost

# Approximate MiniMax M3 pricing on OpenRouter (USD per 1M tokens). Rough —
# adjust if OpenRouter's rate changes; used only for a cost *estimate*.
_M3_PRICE_IN_PER_MTOK = 0.30
_M3_PRICE_OUT_PER_MTOK = 1.20


@router.get("/usage")
async def llm_usage(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> dict[str, Any]:
    """Cumulative OpenRouter usage + a rough cost estimate."""

    u = await get_llm_usage(_streams_redis(request))
    cost = (
        u["prompt_tokens"] / 1_000_000 * _M3_PRICE_IN_PER_MTOK
        + u["completion_tokens"] / 1_000_000 * _M3_PRICE_OUT_PER_MTOK
    )
    return {**u, "est_cost_usd": round(cost, 4)}


# ---------------------------------------------------------------- decision / order feeds


async def _read_stream(redis_client, topic: str, count: int) -> list[dict[str, Any]]:
    """Return up to ``count`` most-recent decoded payloads from a stream (newest first)."""

    try:
        entries = await redis_client.xrevrange(topic, count=count)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for entry_id, fields in entries:
        raw = fields.get("payload")
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            obj["_id"] = entry_id
            out.append(obj)
        except (TypeError, ValueError):
            continue
    return out


@router.get("/decisions/recent")
async def decisions_recent(
    request: Request,
    count: int = Query(default=40, ge=1, le=200),
    session: UserSession = Depends(require_role("viewer")),
) -> list[dict[str, Any]]:
    """Recent decisions (newest first) with score breakdown for the live feed."""

    events = await _read_stream(_streams_redis(request), "decisions", count)
    feed: list[dict[str, Any]] = []
    for ev in events:
        dec = ev.get("decision") or {}
        score = ev.get("score") or {}
        qual = ev.get("qualification") or {}
        feed.append(
            {
                "ts": dec.get("timestamp") or ev.get("produced_at"),
                "action": dec.get("action"),
                "block_reason": dec.get("block_reason"),
                "direction": dec.get("source_direction"),
                "score": score.get("total_score"),
                "band": score.get("band"),
                "subscores": score.get("subscores") or {},
                "source_ai": dec.get("source_ai", False),
                "qualified": bool(qual.get("qualified")) if qual else False,
                "entry_type": qual.get("final_entry_type") if qual else None,
            }
        )
    return feed


@router.get("/orders/recent")
async def orders_recent(
    request: Request,
    count: int = Query(default=40, ge=1, le=200),
    session: UserSession = Depends(require_role("viewer")),
) -> list[dict[str, Any]]:
    """Recent submitted orders (newest first) for the blotter."""

    events = await _read_stream(_streams_redis(request), "orders", count)
    out: list[dict[str, Any]] = []
    for ev in events:
        o = ev.get("order") or {}
        out.append(
            {
                "ts": o.get("timestamp") or ev.get("produced_at"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "volume": o.get("volume"),
                "fill_price": o.get("fill_price"),
                "requested_price": o.get("requested_price"),
                "slippage_pips": o.get("slippage_pips"),
                "status": o.get("status"),
                "error": o.get("error"),
            }
        )
    return out


# ---------------------------------------------------------------- service health


def _stream_age_seconds(last_id: str | None) -> float | None:
    """Age in seconds of a Redis stream id (``<ms>-<seq>``)."""

    if not last_id:
        return None
    try:
        ms = int(str(last_id).split("-", 1)[0])
    except (ValueError, IndexError):
        return None
    return max(0.0, (datetime.now(tz=UTC).timestamp() * 1000 - ms) / 1000.0)


@router.get("/health/services")
async def health_services(
    request: Request,
    session: UserSession = Depends(require_role("viewer")),
) -> dict[str, Any]:
    """Infer service health from Redis: stream activity + execution-engine state freshness."""

    rc = _streams_redis(request)
    result: dict[str, Any] = {"redis": False, "streams": {}, "execution_alive": False}
    try:
        await rc.ping()
        result["redis"] = True
    except Exception:  # noqa: BLE001
        return result

    # Each stream maps to the producing service; "fresh" = recent activity.
    topic_service = {
        "market_ticks": "data-collector",
        "features": "feature-engine",
        "decisions": "decision-engine",
        "orders": "execution-engine",
    }
    for topic, svc in topic_service.items():
        try:
            length = await rc.xlen(topic)
            last = await rc.xrevrange(topic, count=1)
            last_id = last[0][0] if last else None
        except Exception:  # noqa: BLE001
            length, last_id = 0, None
        age = _stream_age_seconds(last_id)
        result["streams"][topic] = {
            "service": svc,
            "len": length,
            "last_age_s": round(age, 1) if age is not None else None,
        }
    # execution-engine liveness: it publishes state:account every 3s (TTL 15s).
    result["execution_alive"] = bool(await get_json(rc, STATE_KEY_ACCOUNT))
    return result


# ============================================================ helpers


def _get_journal_store():
    """Fetch the JournalStore from app.state (set by install_helpers)."""

    raise RuntimeError(
        "_get_journal_store must be replaced by install_helpers()"
    )


def _get_review_engine():
    return None


def _get_fitting_proposal_engine():
    return None


def install_helpers(app) -> None:
    """Replace the module-level placeholder helpers with closures that read app.state.

    Called from :func:`xauusd_bot.dashboard.app.create_app` after
    JournalStore / ReviewEngine / FittingProposalEngine are wired in.
    """

    global _get_journal_store, _get_review_engine, _get_fitting_proposal_engine

    def _journal():
        store = getattr(app.state, "journal_store", None)
        if store is None:
            raise HTTPException(
                status_code=503, detail="journal store not configured"
            )
        return store

    def _review():
        eng = getattr(app.state, "review_engine", None)
        return eng

    def _fitting():
        eng = getattr(app.state, "fitting_proposal_engine", None)
        return eng

    _get_journal_store = _journal
    _get_review_engine = _review
    _get_fitting_proposal_engine = _fitting


__all__ = ["install_helpers", "router"]

