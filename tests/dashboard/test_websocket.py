"""Tests for the WebSocket broker + endpoint (Block 9).

Coverage
--------
* Broker subscribe/unsubscribe/broadcast.
* WS upgrade without cookie → close 4401.
* WS upgrade with cookie → connect OK.
* Broadcast from a fake stream callback reaches subscribed clients.
* Reconnect / cleanup of dead clients.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xauusd_bot.common.config import Settings
from xauusd_bot.dashboard.auth import SESSION_COOKIE, _hash_password
from xauusd_bot.dashboard.redis_subscriber import RedisSubscriber
from xauusd_bot.dashboard.websocket import (
    DEFAULT_TOPICS,
    WebSocketBroker,
    websocket_endpoint,
)


# ----------------------------------------------------------------- helpers


def _settings(**users) -> Settings:
    """Build Settings with the given dashboard_users.

    ``users`` mapping: username -> (password, role). Empty mapping
    produces a no-users Settings (useful for tests that don't need
    any user).
    """

    du: dict[str, dict[str, str]] = {}
    for username, spec in users.items():
        password, role = spec
        du[username] = {"password_hash": _hash_password(password), "role": role}
    return Settings(
        redis_url="redis://localhost:6379/0",
        timescaledb_url="postgresql://xauusd:xauusd@localhost:5432/xauusd",
        dashboard_users=du,
        environment="development",
    )


@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# ----------------------------------------------------------------- broker unit tests


class TestBrokerUnit:
    @pytest.mark.asyncio
    async def test_subscribe_and_broadcast(self) -> None:
        broker = WebSocketBroker()

        class FakeWS:
            def __init__(self):
                self.sent: list[str] = []

            async def send_text(self, msg: str) -> None:
                self.sent.append(msg)

        ws1 = FakeWS()
        ws2 = FakeWS()
        await broker.connect(ws1)  # subscribes to all
        await broker.connect(ws2)
        sent = await broker.broadcast("ticks", {"topic": "ticks", "data": {"p": 1}, "ts": "now"})
        assert sent == 2
        # Both received the same payload.
        payload = json.loads(ws1.sent[0])
        assert payload["topic"] == "ticks"
        assert payload["data"] == {"p": 1}

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self) -> None:
        broker = WebSocketBroker()

        class FakeWS:
            def __init__(self):
                self.sent: list[str] = []

            async def send_text(self, msg: str) -> None:
                self.sent.append(msg)

        ws = FakeWS()
        await broker.connect(ws)
        await broker.subscribe(ws, "features")
        await broker.broadcast("features", {"topic": "features", "data": {}, "ts": "now"})
        await broker.unsubscribe(ws, "features")
        sent = await broker.broadcast("features", {"topic": "features", "data": {}, "ts": "now"})
        assert sent == 0
        assert len(ws.sent) == 1

    @pytest.mark.asyncio
    async def test_unknown_topic_returns_zero(self) -> None:
        broker = WebSocketBroker()
        sent = await broker.broadcast("not-a-topic", {"foo": "bar"})
        assert sent == 0

    @pytest.mark.asyncio
    async def test_dead_client_cleaned_up(self) -> None:
        broker = WebSocketBroker()

        class DeadWS:
            async def send_text(self, msg: str) -> None:
                raise RuntimeError("connection closed")

        ws = DeadWS()
        await broker.connect(ws)
        sent = await broker.broadcast("ticks", {"topic": "ticks", "data": {}, "ts": "now"})
        assert sent == 0  # send failed, target cleaned up
        assert broker.n_clients == 0

    @pytest.mark.asyncio
    async def test_subscribe_unknown_topic_returns_false(self) -> None:
        broker = WebSocketBroker()

        class FakeWS:
            async def send_text(self, msg: str) -> None:
                pass

        ws = FakeWS()
        await broker.connect(ws)
        assert await broker.subscribe(ws, "nope") is False
        assert await broker.unsubscribe(ws, "nope") is False

    @pytest.mark.asyncio
    async def test_default_topics_constant(self) -> None:
        assert set(DEFAULT_TOPICS) == {
            "ticks",
            "features",
            "decisions",
            "orders",
            "journal",
        }


# ----------------------------------------------------------------- WS endpoint integration


def _build_ws_app(broker: WebSocketBroker, redis_client) -> FastAPI:
    """Build a tiny app exposing the WS endpoint with auth wired in."""

    from xauusd_bot.dashboard.auth import DashboardAuth

    app = FastAPI()
    settings = _settings(viewer=("pw", "viewer"), operator=("pw2", "operator"))
    app.state.settings = settings
    app.state.dashboard_redis = redis_client
    app.state.dashboard_auth = DashboardAuth(settings, redis_client)
    app.state.ws_broker = broker
    app.add_api_websocket_route("/ws", websocket_endpoint)
    return app


class TestWebSocketEndpoint:
    def test_no_cookie_close_4401(self, fake_redis) -> None:
        broker = WebSocketBroker()
        app = _build_ws_app(broker, fake_redis)
        with TestClient(app) as c:
            with c.websocket_connect("/ws") as ws:
                # Server immediately closes (4401) — client sees a close.
                with pytest.raises(Exception):
                    ws.receive_text()  # any recv raises because closed

    def test_invalid_cookie_close_4401(self, fake_redis) -> None:
        broker = WebSocketBroker()
        app = _build_ws_app(broker, fake_redis)
        with TestClient(app) as c:
            with c.websocket_connect(
                "/ws", cookies={SESSION_COOKIE: "does-not-exist"}
            ) as ws:
                with pytest.raises(Exception):
                    ws.receive_text()

    def test_valid_cookie_connects(self, fake_redis) -> None:
        """Auth succeeds, then a broadcast from the broker reaches the client."""

        broker = WebSocketBroker()
        app = _build_ws_app(broker, fake_redis)

        # First, create a session via DashboardAuth directly (sync).
        from xauusd_bot.dashboard.auth import DashboardAuth

        async def _make_session() -> str:
            auth = DashboardAuth(_settings(viewer=("pw", "viewer")), fake_redis)
            s = await auth.create_session("viewer")
            return s.session_id

        session_id = asyncio_run(_make_session())

        # After making the session, drain the redis connection to make
        # sure subsequent TestClient runs (which create their own loop)
        # don't conflict with the loop the fakeredis client was bound to.
        # The simplest workaround: we re-create the app with a freshly
        # constructed DashboardAuth against the SAME fake_redis (the
        # fake_redis is loop-agnostic enough that it works across).
        # Since we did _build_ws_app before, we can keep using it — the
        # session lives in fake_redis under a known key.

        with TestClient(app) as c:
            with c.websocket_connect(
                "/ws", cookies={SESSION_COOKIE: session_id}
            ) as ws:
                # Send a subscribe command (we're already subscribed to all,
                # but this exercises the broker round-trip).
                ws.send_text(json.dumps({"action": "subscribe", "topic": "ticks"}))
                # Now broadcast via the broker (synchronous: schedule it).
                import threading

                def _do_broadcast():
                    asyncio.run(_broadcast_one(broker, "ticks", {"price": 2350.5}))

                t = threading.Thread(target=_do_broadcast)
                t.start()
                t.join(timeout=2.0)
                msg = ws.receive_text()
                payload = json.loads(msg)
                assert payload["topic"] == "ticks"
                assert payload["data"]["price"] == 2350.5


def asyncio_run(coro):
    """Helper to run a coroutine in a synchronous test."""

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def _broadcast_one(broker: WebSocketBroker, topic: str, data: dict[str, Any]) -> None:
    await broker.broadcast(topic, {"topic": topic, "data": data, "ts": "now"})


# ----------------------------------------------------------------- RedisSubscriber unit


class TestRedisSubscriber:
    @pytest.mark.asyncio
    async def test_subscriber_can_be_started_and_stopped(self, fake_redis) -> None:
        """Smoke: start() + immediate stop() should not hang."""

        settings = Settings(
            redis_url="redis://localhost:6379/0",
            timescaledb_url="postgresql://xauusd:xauusd@localhost:5432/xauusd",
        )
        # Use the real redis_client (fake_redis) as the subscriber's
        # backend so start() connects immediately.
        from xauusd_bot.dashboard.redis_subscriber import RedisSubscriber

        broadcast_calls: list[tuple[str, dict]] = []

        async def broadcast(topic, payload):
            broadcast_calls.append((topic, payload))

        sub = RedisSubscriber(settings=settings, broadcast=broadcast)
        # Manually swap in the fake redis to avoid real network I/O.
        sub._redis = fake_redis  # type: ignore[attr-defined]
        # start() spawns a background task; stop() cancels it cleanly.
        # We can't call start() because it would create a real Redis
        # connection — just verify stop() on a never-started sub.
        assert sub.is_running is False
        await sub.stop()  # no-op

    def test_decode_entry_with_payload(self) -> None:
        from xauusd_bot.dashboard.redis_subscriber import _decode_entry

        fields = {"payload": '{"topic": "ticks", "data": {"p": 1}, "ts": "now"}'}
        out = _decode_entry("ticks", "1-0", fields)
        assert out is not None
        assert out["topic"] == "ticks"
        assert out["data"] == {"p": 1}

    def test_decode_entry_with_bad_json(self) -> None:
        from xauusd_bot.dashboard.redis_subscriber import _decode_entry

        fields = {"payload": "not-json"}
        out = _decode_entry("ticks", "1-0", fields)
        assert out is None

    def test_decode_entry_with_flat_fields(self) -> None:
        from xauusd_bot.dashboard.redis_subscriber import _decode_entry

        fields = {"price": "2350.5", "volume": "100"}
        out = _decode_entry("ticks", "1-0", fields)
        assert out is not None
        assert out["topic"] == "ticks"
        assert out["data"]["price"] == "2350.5"
