"""Tests for the Telegram notifier (no network — disabled paths + config)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xauusd_bot.common.notify import TelegramNotifier


def test_disabled_when_token_or_chat_missing():
    assert TelegramNotifier(None, "123").enabled is False
    assert TelegramNotifier("tok", None).enabled is False
    assert TelegramNotifier(None, None).enabled is False


def test_enabled_when_both_set():
    assert TelegramNotifier("tok", "chat").enabled is True


def test_enabled_false_when_master_switch_off():
    assert TelegramNotifier("tok", "chat", enabled=False).enabled is False


@pytest.mark.asyncio
async def test_send_is_noop_when_disabled():
    assert await TelegramNotifier(None, None).send("hi") is False


def test_from_settings_reads_secret_and_flags():
    s = SimpleNamespace(
        telegram_bot_token=SimpleNamespace(get_secret_value=lambda: "tok"),
        telegram_chat_id="chat",
        telegram_alerts_enabled=True,
    )
    assert TelegramNotifier.from_settings(s).enabled is True
    s2 = SimpleNamespace(telegram_bot_token=None, telegram_chat_id="chat", telegram_alerts_enabled=True)
    assert TelegramNotifier.from_settings(s2).enabled is False
