"""Web Push (VAPID) notifier — native phone notifications for the mobile PWA.

Mirrors :class:`xauusd_bot.common.notify.TelegramNotifier`: a best-effort
``async send(text)`` that never raises (alerting must never affect a trade).
Where Telegram posts to one chat, this fans a notification out to every browser
push subscription the PWA has registered.

Subscriptions are stored on the trading Redis as a hash ``push:subscriptions``
(field = endpoint URL, value = the browser ``PushSubscription`` JSON). The
dashboard's ``/api/push/*`` endpoints write them; this notifier reads them and
prunes any that the push service reports as gone (HTTP 404/410).

Config (``.env`` / Settings): ``VAPID_PUBLIC_KEY`` + ``VAPID_PRIVATE_KEY`` +
``VAPID_SUBJECT``. Disabled (no-op) when keys are unset, so the bot runs fine
without push — exactly like Telegram.

``pywebpush`` is synchronous (requests under the hood), so each send runs in a
worker thread via :func:`asyncio.to_thread` to stay off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

PUSH_SUBSCRIPTIONS_KEY = "push:subscriptions"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Telegram messages are HTML; push bodies are plain text."""

    return _TAG_RE.sub("", text).strip()


class WebPushNotifier:
    """Async, best-effort Web Push sender backed by Redis-stored subscriptions."""

    def __init__(
        self,
        redis_client: Any,
        *,
        vapid_public_key: str | None,
        vapid_private_key: str | None,
        vapid_subject: str = "mailto:admin@goldmanager.local",
        enabled: bool = True,
        title: str = "GoldManager",
        broadcast_roles: tuple[str, ...] | None = None,
    ) -> None:
        self._redis = redis_client
        self._public_key = vapid_public_key
        self._private_key = vapid_private_key
        self._subject = vapid_subject
        self._title = title
        # When set, ``send()`` (broadcast) only reaches subscriptions whose stored
        # role is in this set — so e.g. trade alerts go to operators/admins, never
        # a viewer who happened to install the PWA. None = no filter (all devices).
        self._broadcast_roles = broadcast_roles
        self._enabled = bool(
            enabled and redis_client is not None and vapid_public_key and vapid_private_key
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def public_key(self) -> str | None:
        return self._public_key

    @classmethod
    def from_settings(
        cls, settings: Any, redis_client: Any, *, broadcast_roles: tuple[str, ...] | None = None
    ) -> "WebPushNotifier":
        priv = getattr(settings, "vapid_private_key", None)
        priv = priv.get_secret_value() if hasattr(priv, "get_secret_value") else priv
        return cls(
            redis_client,
            vapid_public_key=getattr(settings, "vapid_public_key", None),
            vapid_private_key=priv,
            vapid_subject=getattr(settings, "vapid_subject", "mailto:admin@goldmanager.local"),
            enabled=bool(getattr(settings, "webpush_enabled", True)),
            broadcast_roles=broadcast_roles,
        )

    # ----------------------------------------------------------- subscriptions

    async def add_subscription(
        self, subscription: dict[str, Any], *, username: str | None = None, role: str | None = None
    ) -> bool:
        """Persist a browser PushSubscription, tagged with its owner (keyed by endpoint).

        ``username``/``role`` let ``send`` target a single user (so a test push
        never spams other users' devices) and restrict broadcasts by role.
        """

        endpoint = subscription.get("endpoint")
        if not endpoint or self._redis is None:
            return False
        record = {**subscription, "username": username, "role": role}
        await self._redis.hset(PUSH_SUBSCRIPTIONS_KEY, endpoint, json.dumps(record))
        return True

    async def remove_subscription(self, endpoint: str) -> None:
        if self._redis is None or not endpoint:
            return
        await self._redis.hdel(PUSH_SUBSCRIPTIONS_KEY, endpoint)

    async def subscription_count(self) -> int:
        if self._redis is None:
            return 0
        return int(await self._redis.hlen(PUSH_SUBSCRIPTIONS_KEY) or 0)

    # ----------------------------------------------------------- send

    async def send(self, text: str) -> bool:
        """Broadcast ``text`` to registered subscriptions (role-filtered if configured)."""

        roles = self._broadcast_roles
        return await self._fan_out(text, lambda rec: roles is None or rec.get("role") in roles)

    async def send_to_user(self, text: str, username: str) -> bool:
        """Push ``text`` only to ``username``'s own devices (e.g. a test push)."""

        return await self._fan_out(text, lambda rec: rec.get("username") == username)

    async def _fan_out(self, text: str, match) -> bool:
        """Push to every stored subscription matching ``match(record)``, in parallel.

        Sends run concurrently so one slow/dead endpoint (8s timeout each) never
        delays the others; dead subscriptions (404/410) are pruned afterwards.
        """

        if not self._enabled or self._redis is None:
            return False
        try:
            subs = await self._redis.hgetall(PUSH_SUBSCRIPTIONS_KEY)
        except Exception as exc:  # noqa: BLE001 - alerting must never break a trade
            log.warning("webpush_subs_read_failed", error=str(exc))
            return False
        if not subs:
            return False

        payload = json.dumps({"title": self._title, "body": _strip_html(text)})
        targets: list[tuple[str, dict[str, Any]]] = []
        dead: list[str] = []
        for endpoint, raw in subs.items():
            try:
                rec = json.loads(raw)
            except (TypeError, ValueError):
                dead.append(endpoint)
                continue
            if match(rec):
                targets.append((endpoint, rec))

        results = await asyncio.gather(
            *(asyncio.to_thread(self._send_one, rec, payload) for _, rec in targets),
            return_exceptions=True,
        )
        sent = 0
        for (endpoint, _rec), res in zip(targets, results):
            if isinstance(res, BaseException):
                continue
            ok, gone = res
            if ok:
                sent += 1
            if gone:
                dead.append(endpoint)
        for endpoint in dead:
            await self.remove_subscription(endpoint)
        return sent > 0

    def _send_one(self, subscription_info: dict[str, Any], payload: str) -> tuple[bool, bool]:
        """Blocking single push. Returns (ok, is_gone). Runs in a worker thread."""

        try:
            from pywebpush import WebPushException, webpush

            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
                timeout=8,
            )
            return True, False
        except Exception as exc:  # noqa: BLE001 - never raise to the caller
            status = getattr(getattr(exc, "response", None), "status_code", None)
            gone = status in (404, 410)
            # 404/410 = subscription expired → prune quietly; anything else is logged.
            if not gone:
                log.warning("webpush_send_failed", status=status, error=str(exc)[:160])
            return False, gone


__all__ = ["WebPushNotifier", "PUSH_SUBSCRIPTIONS_KEY"]
