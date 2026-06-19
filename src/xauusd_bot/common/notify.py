"""Telegram alerting — best-effort push notifications for live trading events.

The execution-engine uses this to alert on orders (entry filled / rejected),
position-management actions (TP hits, trailing, runner), the emergency
kill-switch, and risk-cap breaches. It replaces the manual log-watcher with a
real channel.

Config (``.env`` / Settings): ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``
(create a bot via @BotFather, get the chat id by messaging the bot then reading
``https://api.telegram.org/bot<token>/getUpdates``). Alerts are disabled when
either is unset, so the bot runs fine without Telegram.

Sends are best-effort: a network/API failure is logged and swallowed — alerting
must never affect a trade. A small min-interval guards against spamming.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Async, best-effort Telegram sender."""

    def __init__(
        self,
        token: str | None,
        chat_id: str | None,
        *,
        enabled: bool = True,
        min_interval_seconds: float = 0.5,
    ) -> None:
        self._token = token
        self._chat = chat_id
        self._enabled = bool(enabled and token and chat_id)
        self._min_interval = float(min_interval_seconds)
        self._last_sent = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @classmethod
    def from_settings(cls, settings: Any) -> "TelegramNotifier":
        tok = getattr(settings, "telegram_bot_token", None)
        token = tok.get_secret_value() if hasattr(tok, "get_secret_value") else tok
        return cls(
            token=token,
            chat_id=getattr(settings, "telegram_chat_id", None),
            enabled=bool(getattr(settings, "telegram_alerts_enabled", True)),
        )

    async def send(self, text: str) -> bool:
        """Send ``text`` to the configured chat. Returns True on a 2xx."""
        if not self._enabled:
            return False
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent = time.monotonic()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    _API.format(token=self._token),
                    json={
                        "chat_id": self._chat,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            ok = 200 <= resp.status_code < 300
            if not ok:
                log.warning("telegram_send_non_2xx", status=resp.status_code, body=resp.text[:200])
            return ok
        except Exception as exc:  # noqa: BLE001 - alerting must never break a trade
            log.warning("telegram_send_failed", error=str(exc))
            return False


class FanoutNotifier:
    """Fan one alert out to several notifiers (Telegram + Web Push + …).

    Exposes the same ``.enabled`` / ``async send(text)`` contract as
    :class:`TelegramNotifier`, so it is a drop-in replacement at the single
    execution-engine call site. ``enabled`` is True if *any* child is enabled;
    ``send`` dispatches to every enabled child best-effort (one failure never
    blocks the others) and returns True if at least one delivered.
    """

    def __init__(self, *notifiers: Any) -> None:
        self._children = [n for n in notifiers if n is not None]

    @property
    def enabled(self) -> bool:
        return any(getattr(n, "enabled", False) for n in self._children)

    async def send(self, text: str) -> bool:
        results = await asyncio.gather(
            *(n.send(text) for n in self._children if getattr(n, "enabled", False)),
            return_exceptions=True,
        )
        return any(r is True for r in results)


__all__ = ["TelegramNotifier", "FanoutNotifier"]
