"""WebSocket broker + endpoint for the dashboard (Block 9).

Authentication
--------------
The ``/ws`` endpoint upgrades only if the request carries a valid
``dashboard_sid`` cookie (same :func:`current_session` dependency as
the REST API). Unauthenticated upgrades are closed with code 4401
(a custom code in the 4xxx range — outside the standard close codes
but easy to spot in browser DevTools).

Subscription protocol
---------------------
Client → Server (JSON)::
    {"action": "subscribe",   "topic": "ticks"}
    {"action": "unsubscribe", "topic": "features"}

Server → Client (JSON, broadcast per topic)::
    {"topic": "ticks", "data": {...}, "ts": "..."}

Topics are :data:`DEFAULT_TOPICS` (see below). Subscribing to an
unknown topic is a no-op (logged at DEBUG) — clients are forward-
compatible with topic additions.

Concurrency model
-----------------
The :class:`WebSocketBroker` keeps a single in-process registry of
``(websocket, subscribed_topics)``. The RedisSubscriber calls
:meth:`WebSocketBroker.broadcast` from the consumer loop. Both run on
the same event loop (asyncio) so no lock is needed for state, but
each send is wrapped in a try/except so a single dead client does
not stop the broadcast.

Hard rules (see AGENTS.md §4j)
------------------------------
* No per-topic permission check — any authenticated user can subscribe
  to any topic (granular permissions are a Block-9+ follow-up).
* Server pushes are read-only — clients cannot inject trades via
  this channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Iterable

import structlog
from fastapi import Cookie, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status

from xauusd_bot.dashboard.auth import (
    SESSION_COOKIE,
    DashboardAuth,
    UserSession,
    current_session,
)

log = structlog.get_logger(__name__)
_ = logging

DEFAULT_TOPICS: tuple[str, ...] = (
    "ticks",
    "features",
    "decisions",
    "orders",
    "journal",
)

# Custom close code for unauthenticated WS upgrades (4xxx = application).
_WS_CLOSE_UNAUTHENTICATED = 4401


class WebSocketBroker:
    """In-memory broker of WebSocket connections and topic subscriptions.

    Use :meth:`connect` to register a connection (called from the WS
    endpoint), :meth:`broadcast` to push a payload to all subscribers
    of a given topic, and :meth:`disconnect` to remove a connection
    after close.
    """

    def __init__(self, *, heartbeat_seconds: float = 30.0) -> None:
        # Map of WebSocket -> set[str] of subscribed topics.
        self._clients: dict[WebSocket, set[str]] = {}
        # Per-topic index for fast fan-out.
        self._by_topic: dict[str, set[WebSocket]] = {t: set() for t in DEFAULT_TOPICS}
        self._heartbeat = float(heartbeat_seconds)
        self._lock = asyncio.Lock()

    # ============================================================ connection mgmt

    async def connect(self, ws: WebSocket, topics: Iterable[str] | None = None) -> None:
        """Register a connected websocket. Subscribes to ``topics`` (default all)."""

        sub = set(topics) if topics is not None else set(DEFAULT_TOPICS)
        # Filter to known topics.
        sub = {t for t in sub if t in DEFAULT_TOPICS}
        async with self._lock:
            self._clients[ws] = sub
            for t in sub:
                self._by_topic[t].add(ws)
        log.info("ws_connected", topics=sorted(sub))

    async def disconnect(self, ws: WebSocket) -> None:
        """Unregister a websocket."""

        async with self._lock:
            sub = self._clients.pop(ws, set())
            for t in sub:
                self._by_topic.get(t, set()).discard(ws)
        log.info("ws_disconnected", topics=sorted(sub))

    async def subscribe(self, ws: WebSocket, topic: str) -> bool:
        """Subscribe a single ws to a topic. Returns True if changed."""

        if topic not in DEFAULT_TOPICS:
            return False
        async with self._lock:
            sub = self._clients.get(ws)
            if sub is None:
                return False
            if topic in sub:
                return False
            sub.add(topic)
            self._by_topic[topic].add(ws)
        return True

    async def unsubscribe(self, ws: WebSocket, topic: str) -> bool:
        """Unsubscribe a single ws from a topic. Returns True if changed."""

        if topic not in DEFAULT_TOPICS:
            return False
        async with self._lock:
            sub = self._clients.get(ws)
            if sub is None or topic not in sub:
                return False
            sub.discard(topic)
            self._by_topic[topic].discard(ws)
        return True

    # ============================================================ broadcast

    async def broadcast(self, topic: str, payload: dict[str, Any]) -> int:
        """Push a payload to all subscribers of ``topic``. Returns count sent."""

        if topic not in DEFAULT_TOPICS:
            log.debug("ws_broadcast_unknown_topic", topic=topic)
            return 0
        async with self._lock:
            targets = list(self._by_topic.get(topic, set()))
        if not targets:
            return 0
        msg = json.dumps(payload, default=_json_default)
        dead: list[WebSocket] = []
        sent = 0
        for ws in targets:
            try:
                await ws.send_text(msg)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "ws_send_failed",
                    topic=topic,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                dead.append(ws)
        if dead:
            await self._cleanup_dead(dead)
        return sent

    async def _cleanup_dead(self, dead: list[WebSocket]) -> None:
        async with self._lock:
            for ws in dead:
                sub = self._clients.pop(ws, set())
                for t in sub:
                    self._by_topic.get(t, set()).discard(ws)

    # ============================================================ introspection

    @property
    def n_clients(self) -> int:
        return len(self._clients)

    def subscribers(self, topic: str) -> int:
        return len(self._by_topic.get(topic, set()))


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for UUID, datetime, Decimal."""

    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "hex") and hasattr(obj, "version"):
        # UUID-like.
        return str(obj)
    if hasattr(obj, "__float__"):
        try:
            return float(obj)
        except Exception:  # noqa: BLE001
            return str(obj)
    return str(obj)


# ----------------------------------------------------------------- endpoint


async def websocket_endpoint(
    websocket: WebSocket,
    dashboard_sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> None:
    """FastAPI WS endpoint at ``/ws``.

    Closes with 4401 if no valid session. Otherwise loops, reading
    subscribe/unsubscribe commands and forwarding to the broker.
    """

    app: FastAPI = websocket.app
    auth: DashboardAuth | None = getattr(app.state, "dashboard_auth", None)
    broker: WebSocketBroker | None = getattr(app.state, "ws_broker", None)
    if auth is None or broker is None:
        # Auth not initialized — accept then close with our custom code.
        await websocket.accept()
        await websocket.close(code=_WS_CLOSE_UNAUTHENTICATED)
        return
    if not dashboard_sid:
        await websocket.accept()
        await websocket.close(code=_WS_CLOSE_UNAUTHENTICATED)
        return
    session: UserSession | None = await auth.validate_session(dashboard_sid)
    if session is None:
        await websocket.accept()
        await websocket.close(code=_WS_CLOSE_UNAUTHENTICATED)
        return
    await websocket.accept()
    await broker.connect(websocket)
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                cmd = json.loads(raw)
            except (TypeError, ValueError):
                # Garbled message — ignore.
                continue
            action = cmd.get("action")
            topic = cmd.get("topic")
            if not isinstance(topic, str):
                continue
            if action == "subscribe":
                await broker.subscribe(websocket, topic)
            elif action == "unsubscribe":
                await broker.unsubscribe(websocket, topic)
            else:
                # Unknown action — silently ignore.
                continue
    finally:
        await broker.disconnect(websocket)


__all__ = ["DEFAULT_TOPICS", "WebSocketBroker", "websocket_endpoint"]
