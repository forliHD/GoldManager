"""Dashboard authentication (Block 9).

Cookie-session-based authentication for the FastAPI dashboard. The
:class:`DashboardAuth` wraps bcrypt password verification + Redis
session storage. Sessions are keyed under ``dashboard:session:<uuid>``
in the dashboard Redis DB (default :setting:`Settings.dashboard_redis_url`,
DB 1) so they cannot collide with trading Redis Streams on DB 0.

Architecture
------------
* :class:`UserSession` — immutable view-model dataclass returned by
  the FastAPI dependency :func:`current_session`.
* :class:`DashboardAuth` — owns user loading, bcrypt verify, session
  create/validate/revoke. Pure async; takes :class:`Settings` and a
  redis-py async client.
* :func:`current_session` / :func:`require_role` — FastAPI Depends
  factories. They look up the session cookie and 401 / 403 as needed.

Hard rules (see AGENTS.md §4j)
-----------------------------
* Plaintext passwords are ONLY accepted on the login endpoint and
  NEVER logged (structlog does not see the password field).
* Bcrypt hashes only — no SHA / plaintext.
* Cookie attributes: ``httponly=True``, ``samesite="lax"``, ``secure``
  in production environments.
* 401 (not 403) for invalid sessions; 403 for authenticated-but-wrong-
  role.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, AsyncIterator, Literal

import bcrypt
import structlog
from fastapi import Cookie, Depends, HTTPException, Request, status

from xauusd_bot.common.config import Settings

log = structlog.get_logger(__name__)

# A no-op logger so accidental `log.info("password=%s", pw)` still has
# to be deliberate. structlog does not log this by default, but we
# keep the ``logging`` module import for symmetry with the rest of the
# codebase.
_ = logging

# ----------------------------------------------------------------- types


Role = Literal["viewer", "operator", "admin"]
ROLES: tuple[Role, ...] = ("viewer", "operator", "admin")
_RANK: dict[Role, int] = {"viewer": 1, "operator": 2, "admin": 3}

# Cookie name. Frontend reads ``document.cookie`` filtered by this.
SESSION_COOKIE = "dashboard_sid"

# Redis key namespace.
_SESSION_KEY_PREFIX = "dashboard:session:"


@dataclass(frozen=True)
class UserSession:
    """An authenticated session — returned by :func:`current_session`."""

    session_id: str
    username: str
    role: Role
    created_at: datetime
    last_seen: datetime


# ----------------------------------------------------------------- exceptions


class AuthError(RuntimeError):
    """Base auth error."""


class InvalidCredentialsError(AuthError):
    """Raised when username or password is wrong."""


class SessionExpiredError(AuthError):
    """Raised when a session id is in Redis but its TTL has elapsed."""


# ----------------------------------------------------------------- helpers


def _hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (12 rounds).

    Returns the bcrypt-encoded hash as a UTF-8 string for storage.
    """

    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison. Returns False on malformed hash."""

    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash → not a match (do NOT raise — login flow is
        # expected to never raise; it just returns False).
        return False


def _session_redis_key(session_id: str) -> str:
    return f"{_SESSION_KEY_PREFIX}{session_id}"


# ----------------------------------------------------------------- DashboardAuth


class DashboardAuth:
    """Cookie-session auth, bcrypt passwords, Redis-backed sessions.

    Parameters
    ----------
    settings:
        The application :class:`Settings`. We read
        ``dashboard_users``, ``dashboard_session_ttl_seconds`` and
        ``dashboard_redis_url``.
    redis_client:
        An ``redis.asyncio.Redis`` instance. The dashboard factory
        constructs this in :mod:`xauusd_bot.dashboard.app`.
    """

    def __init__(self, settings: Settings, redis_client) -> None:
        self._settings = settings
        self._redis = redis_client
        self._users = dict(settings.dashboard_users)
        self._ttl = int(settings.dashboard_session_ttl_seconds)

    # ============================================================ users

    def list_users(self) -> list[str]:
        """Return the configured usernames (sorted)."""

        return sorted(self._users.keys())

    def has_user(self, username: str) -> bool:
        return username in self._users

    def verify_password(self, username: str, password: str) -> bool:
        """Return True if ``username`` exists and ``password`` matches
        the stored bcrypt hash.

        Never raises. Unknown user → False.
        """

        user = self._users.get(username)
        if user is None:
            # Run a dummy hash compare to keep timing constant for
            # unknown-user vs wrong-password paths.
            _verify_password(password, "$2b$12$" + "0" * 53)
            return False
        return _verify_password(password, user.get("password_hash", ""))

    # ============================================================ sessions

    async def create_session(self, username: str) -> UserSession:
        """Create a new session for ``username`` and persist it in Redis.

        The caller (login endpoint) is responsible for setting the
        cookie on the response. We do NOT touch the response here
        because this class is auth-only — the endpoint couples the
        session to the cookie.
        """

        if username not in self._users:
            raise InvalidCredentialsError(f"unknown user: {username!r}")
        user = self._users[username]
        role = user.get("role", "viewer")
        if role not in ROLES:
            raise ValueError(f"invalid role {role!r} for user {username!r}")

        session_id = uuid.uuid4().hex
        now = datetime.now(tz=UTC)
        payload = {
            "session_id": session_id,
            "username": username,
            "role": role,
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
        }
        key = _session_redis_key(session_id)
        await self._redis.set(key, json.dumps(payload), ex=self._ttl)
        log.info(
            "dashboard_session_created",
            session_id=session_id,
            username=username,
            role=role,
        )
        return UserSession(
            session_id=session_id,
            username=username,
            role=role,  # type: ignore[arg-type]
            created_at=now,
            last_seen=now,
        )

    async def validate_session(self, session_id: str) -> UserSession | None:
        """Look up the session in Redis and refresh ``last_seen``.

        Returns None if the session is missing or expired. NEVER raises
        — caller is expected to convert None into HTTP 401.
        """

        if not session_id:
            return None
        key = _session_redis_key(session_id)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("dashboard_session_corrupt_payload", session_id=session_id)
            await self._redis.delete(key)
            return None
        # Refresh last_seen and bump TTL — we deliberately extend the
        # session on every valid request (sliding expiration).
        now = datetime.now(tz=UTC)
        data["last_seen"] = now.isoformat()
        try:
            await self._redis.set(key, json.dumps(data), ex=self._ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dashboard_session_refresh_failed",
                session_id=session_id,
                error=str(exc),
            )
        try:
            return UserSession(
                session_id=data["session_id"],
                username=data["username"],
                role=data["role"],
                created_at=datetime.fromisoformat(data["created_at"]),
                last_seen=datetime.fromisoformat(data["last_seen"]),
            )
        except (KeyError, ValueError) as exc:
            log.warning(
                "dashboard_session_parse_failed",
                session_id=session_id,
                error=str(exc),
            )
            await self._redis.delete(key)
            return None

    async def revoke_session(self, session_id: str) -> bool:
        """Delete the session from Redis. Returns True if a session was removed."""

        if not session_id:
            return False
        deleted = await self._redis.delete(_session_redis_key(session_id))
        if deleted:
            log.info("dashboard_session_revoked", session_id=session_id)
        return bool(deleted)


# ----------------------------------------------------------------- dependencies


async def current_session(
    request: Request,
    dashboard_sid: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> UserSession:
    """FastAPI dependency that returns the :class:`UserSession` for the
    request's session cookie, or raises 401 if invalid.

    Wires up to :class:`DashboardAuth` via :attr:`app.state.dashboard_auth`.
    """

    auth: DashboardAuth | None = getattr(request.app.state, "dashboard_auth", None)
    if auth is None:
        # Auth subsystem not wired up → every authenticated endpoint
        # is unsafe. Fail closed with 503 (service unavailable) so the
        # caller knows the dashboard is misconfigured.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard auth not initialized",
        )
    if not dashboard_sid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no session cookie",
        )
    session = await auth.validate_session(dashboard_sid)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired session",
        )
    return session


def require_role(min_role: Role):
    """Build a FastAPI dependency that 403s if the session role is below ``min_role``.

    Usage::

        @router.post(...)
        async def approve(current: UserSession = Depends(require_role("operator"))):
            ...

    Hierarchy: viewer < operator < admin.
    """

    async def _checker(
        session: UserSession = Depends(current_session),
    ) -> UserSession:
        if _RANK[session.role] < _RANK[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {session.role!r} cannot perform this action; need {min_role!r}",
            )
        return session

    return _checker


# ----------------------------------------------------------------- redis client factory


async def make_dashboard_redis(settings: Settings):
    """Construct an ``redis.asyncio.Redis`` client for dashboard sessions.

    Imported lazily by :mod:`xauusd_bot.dashboard.app` so unit tests can
    swap in ``fakeredis``.
    """

    import redis.asyncio as redis_async

    return redis_async.from_url(
        settings.dashboard_redis_url, encoding="utf-8", decode_responses=True
    )


async def get_dashboard_redis_factory() -> AsyncIterator[None]:
    """Async context helper — used by the app to ensure the redis client is closed on shutdown."""

    yield None


__all__ = [
    "AuthError",
    "DashboardAuth",
    "InvalidCredentialsError",
    "ROLES",
    "Role",
    "SESSION_COOKIE",
    "SessionExpiredError",
    "UserSession",
    "current_session",
    "make_dashboard_redis",
    "require_role",
]
