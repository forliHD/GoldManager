"""FastAPI app factory for the dashboard (Block 9).

Builds the :class:`fastapi.FastAPI` instance, mounts the API router,
WebSocket endpoint and static files, and wires the long-lived
dependencies (JournalStore, ReviewEngine, FittingProposalEngine,
RedisSubscriber) onto ``app.state``.

When :attr:`Settings.dashboard_enabled` is False the app still boots
(it must, so ``/api/health`` returns 200 from monitoring) but every
non-health endpoint returns 404. This is enforced by a single
middleware that runs BEFORE routing — no per-route conditionals.

CORS is intentionally tight (no wildcard, only loopback / localhost).
Production deployments are expected to put a Cloudflare Tunnel or
reverse proxy in front — see AGENTS.md §4j.2.

Run locally
-----------
::

    python -m xauusd_bot.dashboard.app \\
        --dashboard-enabled --dashboard-users 'lucas:...' --host 127.0.0.1 --port 8080

For dev, set ``DASHBOARD_ENABLED=true`` and ``DASHBOARD_USERS`` in
``.env`` and use::

    uvicorn xauusd_bot.dashboard.app:create_app --factory --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from xauusd_bot.common.config import Settings, load_settings
from xauusd_bot.dashboard import api as api_module
from xauusd_bot.dashboard.auth import DashboardAuth, make_dashboard_redis
from xauusd_bot.dashboard.redis_subscriber import RedisSubscriber
from xauusd_bot.dashboard.websocket import WebSocketBroker, websocket_endpoint
from xauusd_bot.journal.store import (
    InMemoryJournalStore,
    JournalStore,
    get_journal_store_with_fallback,
)

log = structlog.get_logger(__name__)
_ = logging

STATIC_DIR = Path(__file__).parent / "static"


async def make_streams_redis(settings):
    """Build the trading-Redis (DB 0) client used for runtime toggles.

    Separate from ``make_dashboard_redis`` (session Redis, DB 1). Kept a
    module-level function so tests can patch it with a fake, mirroring
    the ``make_dashboard_redis`` seam.
    """

    import redis.asyncio as aioredis

    return aioredis.from_url(
        settings.dashboard_redis_streams_url or settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


# ----------------------------------------------------------------- middleware


class DashboardGateMiddleware(BaseHTTPMiddleware):
    """When dashboard_enabled=False, every non-``/api/health`` request → 404.

    We exempt ``/api/health`` and the WebSocket endpoint ``/ws`` (a
    disabled dashboard simply rejects the WS upgrade too — but we let
    the WS handler emit its own 1008 close code so clients see a
    meaningful rejection).
    """

    def __init__(self, app, *, enabled: bool) -> None:
        super().__init__(app)
        self._enabled = bool(enabled)

    async def dispatch(self, request: Request, call_next) -> Response:
        if self._enabled:
            return await call_next(request)
        path = request.url.path
        if path == "/api/health" or path == "/health":
            return await call_next(request)
        # Disabled dashboard — return 404 for everything else.
        return JSONResponse(
            status_code=404,
            content={"detail": "dashboard is disabled (set DASHBOARD_ENABLED=true)"},
        )


# ----------------------------------------------------------------- factory


def create_app(
    settings: Settings | None = None,
    *,
    journal_store: JournalStore | None = None,
    review_engine: Any | None = None,
    fitting_proposal_engine: Any | None = None,
    replay_connector_factory: Any | None = None,
) -> FastAPI:
    """Construct the FastAPI dashboard app.

    All dependencies are optional. When omitted the app boots with
    sensible defaults (in-memory journal store, no review/fitting
    engines — those endpoints return 503 — and no replay-connector
    factory — backtests will fail with a clear RuntimeError).

    Production wiring
    -----------------
    In the production container the bot process passes real
    ``review_engine`` and ``fitting_proposal_engine`` instances so
    the dashboard sees the same state the trading process is using.
    """

    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Wire app.state dependencies.
        app.state.settings = settings
        # Same store the journal-writer uses: Timescale when reachable,
        # else InMemory. So the dashboard reads the durable journal.
        app.state.journal_store = journal_store or await get_journal_store_with_fallback(settings)
        app.state.review_engine = review_engine
        app.state.fitting_proposal_engine = fitting_proposal_engine
        app.state.replay_connector_factory = replay_connector_factory

        # Auth + Redis.
        app.state.dashboard_redis = await make_dashboard_redis(settings)
        app.state.dashboard_auth = DashboardAuth(settings, app.state.dashboard_redis)

        # Cloudflare Access SSO pass-through (opt-in). When enabled + configured,
        # a verified Access JWT logs the user in; the local password stays as a
        # fallback. Off / unconfigured → no behaviour change.
        app.state.cf_access_verifier = None
        if settings.cf_access_enabled and settings.cf_access_team_domain and settings.cf_access_aud:
            try:
                from xauusd_bot.dashboard.cf_access import CloudflareAccessVerifier

                app.state.cf_access_verifier = CloudflareAccessVerifier(
                    settings.cf_access_team_domain, settings.cf_access_aud
                )
                log.info("dashboard_cf_access_enabled", team=settings.cf_access_team_domain)
            except Exception as exc:  # noqa: BLE001 - never block boot on SSO setup
                log.warning("dashboard_cf_access_init_failed", error=str(exc))

        # Trading Redis (DB 0) — where runtime toggles the services read
        # live (e.g. the AI-layer flag). Distinct from the session Redis.
        app.state.streams_redis = await make_streams_redis(settings)

        # Default review / fitting engines when not injected, so the Reviews and
        # Proposals tabs work in the deployed service (uvicorn calls create_app
        # with no args). Fitting needs only the journal; Reviews additionally
        # needs an OpenRouter reviewer, so it stays disabled (503) without a key.
        if app.state.fitting_proposal_engine is None:
            try:
                from xauusd_bot.review.fitting_proposal import FittingProposalEngine

                app.state.fitting_proposal_engine = FittingProposalEngine(journal=app.state.journal_store)
            except Exception as exc:  # noqa: BLE001 - optional dashboard feature
                log.warning("dashboard_fitting_engine_init_failed", error=str(exc))
        if app.state.review_engine is None and settings.openrouter_api_key is not None:
            try:
                from xauusd_bot.decision.openrouter_client import OpenRouterClient
                from xauusd_bot.review.engine import ReviewEngine
                from xauusd_bot.review.reviewer_client import ReviewerOpenRouterClient

                base = OpenRouterClient(
                    settings=settings,
                    prompt_path=Path("decision_agent.md"),
                    usage_redis=app.state.streams_redis,
                )
                reviewer = ReviewerOpenRouterClient(base_client=base, prompt_path=Path("review_agent.md"))
                app.state.review_engine = ReviewEngine(
                    journal=app.state.journal_store, backtest=None, reviewer=reviewer, settings=settings
                )
            except Exception as exc:  # noqa: BLE001 - optional dashboard feature
                log.warning("dashboard_review_engine_init_failed", error=str(exc))

        # WebSocket broker.
        app.state.ws_broker = WebSocketBroker()

        # Redis subscriber (only when dashboard enabled).
        app.state.redis_subscriber = None
        if settings.dashboard_enabled:
            subscriber = RedisSubscriber(
                settings=settings,
                broadcast=lambda topic, payload: app.state.ws_broker.broadcast(topic, payload),
            )
            await subscriber.start()
            app.state.redis_subscriber = subscriber

        log.info(
            "dashboard_app_started",
            dashboard_enabled=settings.dashboard_enabled,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            user_count=len(app.state.dashboard_auth.list_users()),
        )
        try:
            yield
        finally:
            sub = getattr(app.state, "redis_subscriber", None)
            if sub is not None:
                await sub.stop()
            for attr in ("dashboard_redis", "streams_redis"):
                redis_client = getattr(app.state, attr, None)
                if redis_client is not None:
                    try:
                        await redis_client.aclose()
                    except Exception:  # noqa: BLE001
                        pass
            log.info("dashboard_app_stopped")

    app = FastAPI(
        title="XAUUSD Dashboard",
        version="0.1.0",
        description="Block 9 — Custom Web Dashboard (FastAPI backend).",
        lifespan=lifespan,
    )

    # Tight CORS — only loopback / localhost. Cloudflare Tunnel
    # terminated remote traffic keeps this safe.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{settings.dashboard_port}",
            f"http://localhost:{settings.dashboard_port}",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    # Dashboard gate runs OUTSIDE CORS so disabled-dashboard 404s
    # don't leak CORS headers.
    app.add_middleware(
        DashboardGateMiddleware, enabled=settings.dashboard_enabled
    )

    # API router.
    app.include_router(api_module.router)
    api_module.install_helpers(app)

    # WebSocket.
    app.add_api_websocket_route("/ws", websocket_endpoint)

    # Never let the browser hard-cache the SPA shell or its script — a stale
    # index.html (e.g. an old CDN URL) otherwise survives reloads. Hashed
    # assets could opt back into caching later; for now keep it simple.
    @app.middleware("http")
    async def _no_cache_frontend(request, call_next):
        response = await call_next(request)
        path = request.url.path
        # A directory path (e.g. "/" or "/m/") is served by StaticFiles(html=True)
        # as that dir's index.html — but the path ends in "/", not ".html", so it
        # would otherwise miss the no-cache rule and the iOS PWA heuristically
        # caches a STALE shell (CSS/markup changes never reach the phone). Cover
        # any directory-index request too.
        if path.endswith("/") or path.endswith((".html", ".js")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    # Static files (frontend placeholder for now).
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


# ----------------------------------------------------------------- CLI


def _generate_demo_password_hash() -> str:
    """Generate a bcrypt hash for a randomly-chosen demo password.

    The demo password is printed at startup so the operator can log in
    immediately. Production should always use a pre-set
    ``DASHBOARD_USERS`` env-var.
    """

    import bcrypt

    password = secrets.token_urlsafe(16)
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")
    return hashed, password


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xauusd_bot.dashboard.app")
    p.add_argument("--host", default=None, help="Override dashboard_host.")
    p.add_argument("--port", type=int, default=None, help="Override dashboard_port.")
    p.add_argument(
        "--dashboard-enabled",
        action="store_true",
        help="Enable the dashboard (otherwise all non-health endpoints 404).",
    )
    p.add_argument(
        "--create-demo-user",
        action="store_true",
        help=(
            "Generate a random demo user with role 'admin' and print the "
            "username / password + bcrypt-hashed env-var to set in .env."
        ),
    )
    p.add_argument(
        "--demo-username",
        default="demo",
        help="Username for --create-demo-user (default: demo).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """``__main__`` entry point — wires env from args and runs uvicorn."""

    args = _build_argparser().parse_args(argv)

    # Mutate env so Settings() picks up our overrides.
    if args.host:
        os.environ["DASHBOARD_HOST"] = args.host
    if args.port:
        os.environ["DASHBOARD_PORT"] = str(args.port)
    if args.dashboard_enabled:
        os.environ["DASHBOARD_ENABLED"] = "true"
    if args.create_demo_user:
        import bcrypt as _bcrypt

        password = secrets.token_urlsafe(16)
        hashed = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode(
            "utf-8"
        )
        users_env = f"{args.demo_username}:{hashed}:admin"
        os.environ["DASHBOARD_USERS"] = users_env
        # Make sure other required settings are present (CI / dev).
        os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
        os.environ.setdefault("TIMESCALEDB_URL", "postgresql://xauusd:xauusd@localhost:5432/xauusd")
        print(f"\n=== Dashboard demo user created ===")
        print(f"  username : {args.demo_username}")
        print(f"  password : {password}")
        print(f"  role     : admin")
        print(
            f"  env-var  : DASHBOARD_USERS={args.demo_username}:<hash>:admin "
            f"(use the hash below for .env)"
        )
        print(f"  bcrypt   : {hashed}")
        print(f"===================================\n")
    # Settings() needs REDIS_URL + TIMESCALEDB_URL. When running CLI
    # without those, fail fast with a clear message.
    if not os.environ.get("REDIS_URL"):
        print("error: REDIS_URL not set; cannot boot dashboard.", file=__import__("sys").stderr)
        return 2
    if not os.environ.get("TIMESCALEDB_URL"):
        print(
            "error: TIMESCALEDB_URL not set; cannot boot dashboard.",
            file=__import__("sys").stderr,
        )
        return 2

    settings = load_settings()
    if not settings.dashboard_enabled:
        print(
            "warning: dashboard_enabled is False; only /api/health will respond 200.",
            file=__import__("sys").stderr,
        )

    import uvicorn

    uvicorn.run(
        "xauusd_bot.dashboard.app:create_app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        factory=True,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
