"""Integration tests for the dashboard REST API (Block 9).

Uses FastAPI's ``TestClient`` against a real ``create_app`` instance
with fakeredis for sessions. The journal store is the in-memory one
(no real TimescaleDB).

Coverage
--------
* Login / logout / me (auth flow).
* /api/health always 200 (even when dashboard_enabled=False).
* /api/chart/candles and /api/chart/overlays read from journal.
* /api/journal/trades + /api/journal/aggregate.
* /api/backtest/{list,run,status}.
* /api/review/daily + /api/review/weekly (with mocked engines).
* /api/fitting-proposal/{list,approve,reject,validate} role checks.
* /api/mode/toggle admin-only + ``dashboard_live_mode_enabled`` gate.
* Disabled-dashboard gate: all non-health endpoints return 404.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import EntryType, ScoreBand
from xauusd_bot.common.schemas.journal import ExitReasonTag, TradeRecord
from xauusd_bot.common.schemas.review import (
    FittingProposal,
    KPISummary,
    ReviewCategory,
    ReviewOutput,
    ReviewProposal,
    ReviewRun,
    TradeSummary,
)
from xauusd_bot.dashboard.auth import SESSION_COOKIE, _hash_password
from xauusd_bot.dashboard.redis_subscriber import RedisSubscriber
from xauusd_bot.journal.store import InMemoryJournalStore


# ----------------------------------------------------------------- helpers


def _make_settings(**overrides: Any) -> Settings:
    base = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql://xauusd:xauusd@localhost:5432/xauusd",
        "dashboard_enabled": True,
        "dashboard_users": {
            "viewer": {"password_hash": _hash_password("viewer-pw"), "role": "viewer"},
            "operator": {"password_hash": _hash_password("operator-pw"), "role": "operator"},
            "admin": {"password_hash": _hash_password("admin-pw"), "role": "admin"},
        },
        "environment": "development",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def journal_with_trades() -> InMemoryJournalStore:
    """A pre-populated in-memory journal with 3 trades."""

    store = InMemoryJournalStore()

    async def _populate():
        # Trade 1: long, closed, win.
        t1 = TradeRecord(
            id=uuid4(),
            timestamp_open=datetime(2026, 4, 15, 10, 0, tzinfo=UTC),
            timestamp_close=datetime(2026, 4, 15, 10, 30, tzinfo=UTC),
            symbol="XAUUSD",
            side="long",
            entry_price=Decimal("2350.00"),
            exit_price=Decimal("2355.00"),
            stop_loss=Decimal("2345.00"),
            take_profits=[Decimal("2355.00")],
            volume_lots=Decimal("0.10"),
            risk_amount=Decimal("50.00"),
            pnl_realized=Decimal("100.00"),
            pnl_unrealized=None,
            r_multiple=2.0,
            setup_id=uuid4(),
            score=80.0,
            band=ScoreBand.REDUCED_75_84,
            entry_type=EntryType.FULL,
            engine_source="rule",
            session="ny",
            atr_at_entry=2.5,
            structure_at_entry="up",
            feature_snapshot_id=uuid4(),
            fill_price=Decimal("2350.00"),
        )
        # Trade 2: short, closed, loss.
        t2 = TradeRecord(
            id=uuid4(),
            timestamp_open=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            timestamp_close=datetime(2026, 4, 15, 12, 30, tzinfo=UTC),
            symbol="XAUUSD",
            side="short",
            entry_price=Decimal("2360.00"),
            exit_price=Decimal("2365.00"),
            stop_loss=Decimal("2365.00"),
            take_profits=[Decimal("2355.00")],
            volume_lots=Decimal("0.10"),
            risk_amount=Decimal("50.00"),
            pnl_realized=Decimal("-50.00"),
            r_multiple=-1.0,
            setup_id=uuid4(),
            score=72.0,
            band=ScoreBand.PREPARE_65_74,
            entry_type=EntryType.REDUCED,
            engine_source="rule",
            session="overlap",
            atr_at_entry=2.7,
            structure_at_entry="range",
            feature_snapshot_id=uuid4(),
            fill_price=Decimal("2360.00"),
        )
        # Trade 3: long, AI source, win.
        t3 = TradeRecord(
            id=uuid4(),
            timestamp_open=datetime(2026, 4, 16, 14, 0, tzinfo=UTC),
            timestamp_close=datetime(2026, 4, 16, 14, 30, tzinfo=UTC),
            symbol="XAUUSD",
            side="long",
            entry_price=Decimal("2370.00"),
            exit_price=Decimal("2380.00"),
            stop_loss=Decimal("2365.00"),
            take_profits=[Decimal("2380.00")],
            volume_lots=Decimal("0.10"),
            risk_amount=Decimal("50.00"),
            pnl_realized=Decimal("200.00"),
            r_multiple=4.0,
            setup_id=uuid4(),
            score=92.0,
            band=ScoreBand.FULL_85_PLUS,
            entry_type=EntryType.FULL,
            engine_source="ai",
            session="ny",
            atr_at_entry=3.0,
            structure_at_entry="up",
            feature_snapshot_id=uuid4(),
            fill_price=Decimal("2370.00"),
        )
        await store.write_trade(t1)
        await store.write_trade(t2)
        await store.write_trade(t3)

    asyncio_run(_populate())
    return store


def asyncio_run(coro):
    """Helper to run an async coroutine in a synchronous test."""

    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ----------------------------------------------------------------- app fixtures


class _StubReviewEngine:
    """Minimal stub of ReviewEngine for API testing."""

    async def run_daily(self, day):
        return _stub_review_run("daily", day)

    async def run_weekly(self, week_start):
        return _stub_review_run("weekly", week_start)


def _stub_review_run(kind: str, day) -> ReviewRun:
    return ReviewRun(
        period_start=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        period_end=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(days=1),
        period_kind=kind,  # type: ignore[arg-type]
        insufficient_data=False,
        min_sample_size=1,
        trade_count=3,
        snapshot_count=0,
        discrepancy_count=0,
        output=ReviewOutput(
            proposals=[
                ReviewProposal(
                    proposal_number=1,
                    category="score_threshold",
                    observation="test",
                    hypothesis="test",
                    validation_test="score_threshold=70",
                    overfitting_risk="low",
                    overfitting_rationale="test",
                )
            ],
            overall_assessment="OK",
            data_sufficiency="sufficient",
            summary="Looks fine.",
        ),
    )


class _StubFittingProposalEngine:
    """In-memory fitting proposal store for API testing."""

    def __init__(self) -> None:
        self._items: dict[Any, FittingProposal] = {}

    async def list_proposals(self, filter=None):
        items = list(self._items.values())
        items.sort(key=lambda p: p.created_at, reverse=True)
        return items

    async def approve(self, proposal_id, *, operator, note=None):
        existing = self._items.get(proposal_id)
        if existing is None:
            raise ValueError("not found")
        updated = existing.model_copy(
            update={
                "status": "approved",
                "decided_at": datetime.now(tz=UTC),
                "decided_by": operator,
                "decision_note": note,
            }
        )
        self._items[proposal_id] = updated
        return updated

    async def reject(self, proposal_id, *, operator, note=None):
        existing = self._items.get(proposal_id)
        if existing is None:
            raise ValueError("not found")
        updated = existing.model_copy(
            update={
                "status": "rejected",
                "decided_at": datetime.now(tz=UTC),
                "decided_by": operator,
                "decision_note": note,
            }
        )
        self._items[proposal_id] = updated
        return updated

    async def get(self, proposal_id):
        return self._items.get(proposal_id)

    async def run_validation(self, proposal):
        # No-op for stub: leave status as-is.
        return proposal

    def add(self, proposal: FittingProposal) -> None:
        self._items[proposal.id] = proposal


@pytest.fixture
def proposal_engine() -> _StubFittingProposalEngine:
    return _StubFittingProposalEngine()


@pytest.fixture
def app_and_state(journal_with_trades, fake_redis, proposal_engine):
    """Build a create_app() instance with stubbed dependencies."""

    settings = _make_settings()
    # Patch make_dashboard_redis to return fake_redis.
    from xauusd_bot.dashboard import app as app_module

    orig = app_module.make_dashboard_redis
    app_module.make_dashboard_redis = lambda s: _async_return(fake_redis)
    try:
        # We need to ensure the lifespan doesn't try to make a real
        # redis connection. Since fake_redis is already constructed,
        # we just patch the factory.
        review_engine = _StubReviewEngine()
        app = app_module.create_app(
            settings=settings,
            journal_store=journal_with_trades,
            review_engine=review_engine,
            fitting_proposal_engine=proposal_engine,
        )
        yield app, settings
    finally:
        app_module.make_dashboard_redis = orig


async def _async_return(value):
    return value


@pytest.fixture
def client(app_and_state):
    """A TestClient wrapping the app, with lifespan events executed."""

    app, _ = app_and_state
    with TestClient(app) as c:
        yield c


@pytest.fixture
def login(client) -> dict[str, str]:
    """Login as operator and return a cookies dict for subsequent requests."""

    r = client.post(
        "/api/auth/login",
        data={"username": "operator", "password": "operator-pw"},
    )
    assert r.status_code == 200, r.text
    cookie = r.cookies.get(SESSION_COOKIE)
    assert cookie
    return {SESSION_COOKIE: cookie}


# ----------------------------------------------------------------- /api/health


class TestHealth:
    def test_health_ok_when_enabled(self, client) -> None:
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["dashboard_enabled"] is True
        assert "timestamp" in data

    def test_health_ok_when_disabled(self, fake_redis, journal_with_trades) -> None:
        """Health is the ONLY endpoint that returns 200 when disabled."""

        from xauusd_bot.dashboard import app as app_module

        settings = _make_settings(dashboard_enabled=False)
        app = app_module.create_app(
            settings=settings,
            journal_store=journal_with_trades,
        )
        with TestClient(app) as c:
            r = c.get("/api/health")
            assert r.status_code == 200
            data = r.json()
            assert data["dashboard_enabled"] is False
            # All other endpoints should be 404.
            assert c.post("/api/auth/login", data={"username": "x", "password": "y"}).status_code == 404
            assert c.get("/api/auth/me").status_code == 404


# ----------------------------------------------------------------- Auth flow


class TestAuthFlow:
    def test_login_success_returns_cookie(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "operator", "password": "operator-pw"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "operator"
        assert body["role"] == "operator"
        assert SESSION_COOKIE in r.cookies

    def test_login_wrong_password_401(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "operator", "password": "wrong"},
        )
        assert r.status_code == 401
        assert SESSION_COOKIE not in r.cookies

    def test_login_unknown_user_401(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "ghost", "password": "anything"},
        )
        assert r.status_code == 401

    def test_login_does_not_log_password(
        self, client, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Privacy: plaintext password never appears in logs (I-PII)."""

        # structlog uses standard logging under the hood.
        import logging

        caplog.set_level(logging.INFO, logger="xauusd_bot.dashboard.api")
        r = client.post(
            "/api/auth/login",
            data={"username": "operator", "password": "operator-pw"},
        )
        assert r.status_code == 200
        # Inspect all captured records.
        for record in caplog.records:
            assert "operator-pw" not in record.getMessage()

    def test_me_authenticated(self, client, login) -> None:
        r = client.get("/api/auth/me", cookies=login)
        assert r.status_code == 200
        assert r.json()["username"] == "operator"

    def test_me_no_cookie_401(self, client) -> None:
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_logout_clears_cookie(self, client, login) -> None:
        r = client.post("/api/auth/logout", cookies=login)
        assert r.status_code == 200
        # Calling /me again with the now-revoked cookie should 401.
        r2 = client.get("/api/auth/me", cookies=login)
        assert r2.status_code == 401


# ----------------------------------------------------------------- /api/chart


class TestChartEndpoints:
    def test_chart_candles_empty_returns_empty_list(self, client, login) -> None:
        r = client.get("/api/chart/candles?count=10", cookies=login)
        assert r.status_code == 200
        assert r.json() == []

    def test_chart_overlays_no_overlays_returns_default(self, client, login) -> None:
        r = client.get("/api/chart/overlays", cookies=login)
        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "XAUUSD"
        assert data["vwaps"] == {"utc00": None, "utc07": None, "utc12": None}
        assert data["volume_profile"] == {}
        assert data["fvg_zones"] == []


# ----------------------------------------------------------------- /api/journal


class TestJournalEndpoints:
    def test_trades_returns_summary_list(self, client, login) -> None:
        r = client.get("/api/journal/trades?limit=10", cookies=login)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 3
        # Most recent first.
        assert data[0]["side"] == "long"
        # Decision kind & llm_used mapping.
        ai_trade = next(t for t in data if t["engine_source"] == "ai")
        assert ai_trade["decision_kind"] == "ai"
        assert ai_trade["llm_used"] is True

    def test_trades_no_cookie_401(self, client) -> None:
        r = client.get("/api/journal/trades")
        assert r.status_code == 401

    def test_aggregate_known_period(self, client, login) -> None:
        # Use 'all' so the period covers 1970-now and the seeded trades
        # (April 2026) are included regardless of when the test runs.
        r = client.get("/api/journal/aggregate?period=all", cookies=login)
        assert r.status_code == 200
        data = r.json()
        assert data["period"] == "all"
        assert data["n_trades"] == 3
        assert data["n_closed"] == 3
        assert data["n_wins"] == 2
        assert data["n_losses"] == 1
        assert 0.5 < data["winrate"] < 0.8
        assert data["total_pnl"] == pytest.approx(250.0, abs=0.01)

    def test_aggregate_unknown_period_400(self, client, login) -> None:
        r = client.get("/api/journal/aggregate?period=lifetime", cookies=login)
        assert r.status_code == 400


# ----------------------------------------------------------------- /api/backtest


class TestBacktestEndpoints:
    def test_backtest_list_empty(self, client, login) -> None:
        r = client.get("/api/backtest/list", cookies=login)
        assert r.status_code == 200
        assert r.json() == []

    def test_backtest_status_unknown_404(self, client, login) -> None:
        r = client.get("/api/backtest/status?task_id=does-not-exist", cookies=login)
        assert r.status_code == 404

    def test_backtest_run_viewer_403(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "viewer", "password": "viewer-pw"},
        )
        assert r.status_code == 200
        sid = r.cookies.get(SESSION_COOKIE)
        r2 = client.post(
            "/api/backtest/run",
            json={
                "start_date": "2026-04-01T00:00:00+00:00",
                "end_date": "2026-04-02T00:00:00+00:00",
            },
            cookies={SESSION_COOKIE: sid},
        )
        assert r2.status_code == 403


# ----------------------------------------------------------------- /api/review


class TestReviewEndpoints:
    def test_daily_returns_review_run(self, client, login) -> None:
        r = client.get("/api/review/daily?day=2026-04-15", cookies=login)
        assert r.status_code == 200
        data = r.json()
        assert data["period_kind"] == "daily"
        assert data["insufficient_data"] is False
        assert data["output"] is not None
        assert data["output"]["data_sufficiency"] == "sufficient"

    def test_weekly_returns_review_run(self, client, login) -> None:
        r = client.get("/api/review/weekly?week_start=2026-04-13", cookies=login)
        assert r.status_code == 200
        assert r.json()["period_kind"] == "weekly"


# ----------------------------------------------------------------- /api/fitting-proposal


class TestFittingProposalEndpoints:
    def _seed_proposal(self, engine: _StubFittingProposalEngine) -> FittingProposal:
        p = FittingProposal(
            period_start=datetime(2026, 4, 15, tzinfo=UTC),
            period_end=datetime(2026, 4, 16, tzinfo=UTC),
            proposal_number=1,
            category="score_threshold",
            observation="test observation",
            hypothesis="test hypothesis",
            validation_test="score_threshold=70",
            overfitting_risk="low",
            overfitting_rationale="test",
            status="proposed",
        )
        engine.add(p)
        return p

    def test_list_empty(self, client, login) -> None:
        r = client.post("/api/fitting-proposal/list", json={}, cookies=login)
        assert r.status_code == 200
        assert r.json() == []

    def test_list_with_one(self, client, login, proposal_engine) -> None:
        self._seed_proposal(proposal_engine)
        r = client.post("/api/fitting-proposal/list", json={}, cookies=login)
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["status"] == "proposed"

    def test_approve_viewer_403(self, client, proposal_engine) -> None:
        p = self._seed_proposal(proposal_engine)
        # Login as viewer.
        r = client.post(
            "/api/auth/login",
            data={"username": "viewer", "password": "viewer-pw"},
        )
        sid = r.cookies.get(SESSION_COOKIE)
        r2 = client.post(
            "/api/fitting-proposal/approve",
            json={"proposal_id": str(p.id)},
            cookies={SESSION_COOKIE: sid},
        )
        assert r2.status_code == 403

    def test_approve_operator_200(self, client, login, proposal_engine) -> None:
        p = self._seed_proposal(proposal_engine)
        r = client.post(
            "/api/fitting-proposal/approve",
            json={"proposal_id": str(p.id), "note": "looks good"},
            cookies=login,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "approved"
        assert data["decided_by"] == "operator"
        assert data["decision_note"] == "looks good"

    def test_reject_operator_200(self, client, login, proposal_engine) -> None:
        p = self._seed_proposal(proposal_engine)
        r = client.post(
            "/api/fitting-proposal/reject",
            json={"proposal_id": str(p.id), "note": "no"},
            cookies=login,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_validate_unknown_404(self, client, login) -> None:
        r = client.post(
            "/api/fitting-proposal/validate",
            json={"proposal_id": str(uuid4())},
            cookies=login,
        )
        assert r.status_code == 404

    def test_approve_does_not_modify_settings(
        self, client, login, proposal_engine, app_and_state
    ) -> None:
        """I-4: approve() calls engine.approve which writes ONLY to the
        journal; never to settings."""

        from xauusd_bot.common.config import ConnectorMode

        p = self._seed_proposal(proposal_engine)
        app, settings = app_and_state
        before_mode = settings.connector_mode
        before_users = dict(settings.dashboard_users)
        r = client.post(
            "/api/fitting-proposal/approve",
            json={"proposal_id": str(p.id)},
            cookies=login,
        )
        assert r.status_code == 200
        # Settings unchanged.
        assert settings.connector_mode == before_mode
        assert settings.dashboard_users == before_users


# ----------------------------------------------------------------- /api/mode/toggle


class TestModeToggle:
    def test_viewer_403(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "viewer", "password": "viewer-pw"},
        )
        sid = r.cookies.get(SESSION_COOKIE)
        r2 = client.post(
            "/api/mode/toggle",
            json={"target_mode": "replay", "confirm": True},
            cookies={SESSION_COOKIE: sid},
        )
        assert r2.status_code == 403

    def test_operator_403(self, client, login) -> None:
        r = client.post(
            "/api/mode/toggle",
            json={"target_mode": "replay", "confirm": True},
            cookies=login,
        )
        assert r.status_code == 403

    def test_admin_replay_no_confirm_400(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "admin-pw"},
        )
        sid = r.cookies.get(SESSION_COOKIE)
        r2 = client.post(
            "/api/mode/toggle",
            json={"target_mode": "replay"},
            cookies={SESSION_COOKIE: sid},
        )
        assert r2.status_code == 400

    def test_admin_replay_ok(self, client) -> None:
        r = client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "admin-pw"},
        )
        sid = r.cookies.get(SESSION_COOKIE)
        r2 = client.post(
            "/api/mode/toggle",
            json={"target_mode": "replay", "confirm": True},
            cookies={SESSION_COOKIE: sid},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["new_mode"] == "replay"
        assert data["operator"] == "admin"
        assert data["redis_key"] == "dashboard:connector_mode"

    def test_admin_live_disabled_403(
        self, fake_redis, journal_with_trades
    ) -> None:
        """Live-mode is double-gated: admin role AND dashboard_live_mode_enabled."""

        from xauusd_bot.dashboard import app as app_module

        settings = _make_settings(dashboard_live_mode_enabled=False)
        app_module.make_dashboard_redis = lambda s: _async_return(fake_redis)
        app = app_module.create_app(
            settings=settings,
            journal_store=journal_with_trades,
        )
        with TestClient(app) as c:
            lr = c.post(
                "/api/auth/login",
                data={"username": "admin", "password": "admin-pw"},
            )
            sid = lr.cookies.get(SESSION_COOKIE)
            r = c.post(
                "/api/mode/toggle",
                json={"target_mode": "live", "confirm": True},
                cookies={SESSION_COOKIE: sid},
            )
            assert r.status_code == 403
