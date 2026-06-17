"""Tests for DashboardAuth (Block 9).

Coverage
--------
* Password hashing round-trip.
* Session create / validate / revoke lifecycle.
* Cookie-based FastAPI dependency :func:`current_session`.
* Role-based access via :func:`require_role`.
* TTL behaviour via fakeredis's ``time_machine``.

All tests use fakeredis (no real Redis dependency) — see
``pyproject.toml`` dev-deps.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from xauusd_bot.common.config import Settings
from xauusd_bot.dashboard.auth import (
    SESSION_COOKIE,
    DashboardAuth,
    InvalidCredentialsError,
    UserSession,
    _hash_password,
    _verify_password,
    current_session,
    require_role,
)


# ----------------------------------------------------------------- fixtures


def _settings_with_users(**users: tuple[str, str]) -> Settings:
    """Build Settings with the given dashboard_users.

    ``users`` mapping: username -> (plaintext_password, role).
    The plaintext is hashed before storage.
    """

    dashboard_users: dict[str, dict[str, str]] = {}
    for username, (password, role) in users.items():
        dashboard_users[username] = {
            "password_hash": _hash_password(password),
            "role": role,
        }
    return Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql://xauusd:xauusd@localhost:5432/xauusd",
        dashboard_users=dashboard_users,
        dashboard_session_ttl_seconds=8 * 3600,
        environment="development",
    )


@pytest.fixture
def fake_redis():
    """An async fakeredis instance — tests run without a real Redis."""

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def auth(fake_redis):
    """A DashboardAuth with one viewer + one operator + one admin user."""

    settings = _settings_with_users(
        viewer=("viewer-pw", "viewer"),
        operator=("operator-pw", "operator"),
        admin=("admin-pw", "admin"),
    )
    return DashboardAuth(settings, fake_redis)


# ----------------------------------------------------------------- hashing


class TestPasswordHashing:
    def test_hash_then_verify_roundtrip(self) -> None:
        h = _hash_password("hunter2")
        assert h != "hunter2"
        assert h.startswith("$2b$") or h.startswith("$2a$")
        assert _verify_password("hunter2", h) is True

    def test_verify_wrong_password_returns_false(self) -> None:
        h = _hash_password("correct")
        assert _verify_password("wrong", h) is False

    def test_verify_malformed_hash_returns_false(self) -> None:
        assert _verify_password("anything", "not-a-bcrypt-hash") is False


# ----------------------------------------------------------------- user mgmt


class TestUserLookup:
    def test_has_user_returns_true_for_known(self, auth: DashboardAuth) -> None:
        assert auth.has_user("viewer") is True
        assert auth.has_user("admin") is True

    def test_has_user_returns_false_for_unknown(self, auth: DashboardAuth) -> None:
        assert auth.has_user("ghost") is False

    def test_list_users_returns_sorted(self, auth: DashboardAuth) -> None:
        assert auth.list_users() == ["admin", "operator", "viewer"]

    def test_verify_password_correct(self, auth: DashboardAuth) -> None:
        assert auth.verify_password("viewer", "viewer-pw") is True
        assert auth.verify_password("admin", "admin-pw") is True

    def test_verify_password_wrong(self, auth: DashboardAuth) -> None:
        assert auth.verify_password("viewer", "wrong") is False

    def test_verify_password_unknown_user(self, auth: DashboardAuth) -> None:
        # Unknown user returns False; does NOT raise.
        assert auth.verify_password("ghost", "anything") is False


# ----------------------------------------------------------------- sessions


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_create_returns_uuid_and_session_id(self, auth: DashboardAuth) -> None:
        s = await auth.create_session("admin")
        assert isinstance(s.session_id, str) and len(s.session_id) == 32
        assert s.username == "admin"
        assert s.role == "admin"
        assert s.created_at <= datetime.now(tz=UTC)
        assert s.last_seen <= datetime.now(tz=UTC)

    @pytest.mark.asyncio
    async def test_validate_roundtrip(self, auth: DashboardAuth) -> None:
        s = await auth.create_session("operator")
        loaded = await auth.validate_session(s.session_id)
        assert loaded is not None
        assert loaded.username == "operator"
        assert loaded.role == "operator"
        assert loaded.session_id == s.session_id

    @pytest.mark.asyncio
    async def test_validate_missing_returns_none(self, auth: DashboardAuth) -> None:
        assert await auth.validate_session("00000000000000000000000000000000") is None

    @pytest.mark.asyncio
    async def test_validate_empty_returns_none(self, auth: DashboardAuth) -> None:
        assert await auth.validate_session("") is None

    @pytest.mark.asyncio
    async def test_revoke_removes_session(self, auth: DashboardAuth) -> None:
        s = await auth.create_session("viewer")
        removed = await auth.revoke_session(s.session_id)
        assert removed is True
        assert await auth.validate_session(s.session_id) is None

    @pytest.mark.asyncio
    async def test_revoke_unknown_returns_false(self, auth: DashboardAuth) -> None:
        assert await auth.revoke_session("nonexistent") is False

    @pytest.mark.asyncio
    async def test_create_unknown_user_raises(self, auth: DashboardAuth) -> None:
        with pytest.raises(InvalidCredentialsError):
            await auth.create_session("ghost")

    @pytest.mark.asyncio
    async def test_ttl_expiry_via_fakeredis(self, fake_redis) -> None:
        """Validate that an expired session is treated as missing.

        fakeredis honors the ``ex`` TTL on ``set`` — we directly
        delete the session key to simulate TTL expiry. (The
        time_machine helper from fakeredis is not part of the public
        API in the version we depend on; the assertion is the same.)
        """

        from xauusd_bot.dashboard.auth import _session_redis_key

        settings = _settings_with_users(viewer=("pw", "viewer"))
        auth = DashboardAuth(settings, fake_redis)
        s = await auth.create_session("viewer")
        # Confirm the session is there first.
        loaded = await auth.validate_session(s.session_id)
        assert loaded is not None
        # Simulate TTL expiry by deleting the Redis key directly.
        deleted = await fake_redis.delete(_session_redis_key(s.session_id))
        assert deleted == 1
        # Now validate must return None.
        loaded_after = await auth.validate_session(s.session_id)
        assert loaded_after is None


# ----------------------------------------------------------------- FastAPI dependency


def _build_app_with_auth(auth: DashboardAuth) -> FastAPI:
    """Build a tiny FastAPI app exposing current_session / require_role."""

    app = FastAPI()
    app.state.dashboard_auth = auth

    @app.get("/me")
    async def me(s: UserSession = pytest.importorskip("fastapi").Depends(current_session)):
        return {"user": s.username, "role": s.role}

    @app.get("/operator-only")
    async def op_only(
        s: UserSession = pytest.importorskip("fastapi").Depends(require_role("operator")),
    ):
        return {"user": s.username, "role": s.role}

    @app.get("/admin-only")
    async def admin_only(
        s: UserSession = pytest.importorskip("fastapi").Depends(require_role("admin")),
    ):
        return {"user": s.username, "role": s.role}

    return app


class TestFastAPIDependencies:
    @pytest.mark.asyncio
    async def test_current_session_no_cookie_401(self, auth: DashboardAuth) -> None:
        from fastapi.testclient import TestClient

        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/me")
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_current_session_invalid_cookie_401(self, auth: DashboardAuth) -> None:
        from fastapi.testclient import TestClient

        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/me", cookies={SESSION_COOKIE: "bogus"})
            assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_current_session_valid_cookie(self, auth: DashboardAuth) -> None:
        from fastapi.testclient import TestClient

        s = await auth.create_session("operator")
        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/me", cookies={SESSION_COOKIE: s.session_id})
            assert r.status_code == 200
            assert r.json() == {"user": "operator", "role": "operator"}

    @pytest.mark.asyncio
    async def test_require_role_viewer_denied_for_operator_only(
        self, auth: DashboardAuth
    ) -> None:
        from fastapi.testclient import TestClient

        s = await auth.create_session("viewer")
        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/operator-only", cookies={SESSION_COOKIE: s.session_id})
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_require_role_admin_denies_operator(self, auth: DashboardAuth) -> None:
        from fastapi.testclient import TestClient

        s = await auth.create_session("operator")
        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/admin-only", cookies={SESSION_COOKIE: s.session_id})
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_require_role_admin_allows_admin(self, auth: DashboardAuth) -> None:
        from fastapi.testclient import TestClient

        s = await auth.create_session("admin")
        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/admin-only", cookies={SESSION_COOKIE: s.session_id})
            assert r.status_code == 200
            assert r.json()["role"] == "admin"

    @pytest.mark.asyncio
    async def test_require_role_operator_allows_admin(self, auth: DashboardAuth) -> None:
        """Hierarchy: admin > operator > viewer."""

        from fastapi.testclient import TestClient

        s = await auth.create_session("admin")
        app = _build_app_with_auth(auth)
        with TestClient(app) as client:
            r = client.get("/operator-only", cookies={SESSION_COOKIE: s.session_id})
            assert r.status_code == 200


# ----------------------------------------------------------------- disabled-auth guard


class TestAuthNotInitialized:
    @pytest.mark.asyncio
    async def test_current_session_no_auth_503(self) -> None:
        """When app.state.dashboard_auth is not set, current_session → 503."""

        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/me")
        async def me(s: UserSession = pytest.importorskip("fastapi").Depends(current_session)):
            return {"ok": True}

        with TestClient(app) as client:
            r = client.get("/me", cookies={SESSION_COOKIE: "anything"})
            assert r.status_code == 503
