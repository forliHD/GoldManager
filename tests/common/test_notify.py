"""Tests for the Telegram notifier (no network — disabled paths + config)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xauusd_bot.common.notify import FanoutNotifier, TelegramNotifier


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


# ----------------------------------------------------------------- FanoutNotifier


class _FakeChild:
    def __init__(self, enabled: bool, result=True, raises: bool = False) -> None:
        self.enabled = enabled
        self._result = result
        self._raises = raises
        self.sent: list[str] = []

    async def send(self, text: str):
        self.sent.append(text)
        if self._raises:
            raise RuntimeError("boom")
        return self._result


def test_fanout_enabled_if_any_child_enabled():
    assert FanoutNotifier(_FakeChild(False), _FakeChild(True)).enabled is True
    assert FanoutNotifier(_FakeChild(False), _FakeChild(False)).enabled is False
    assert FanoutNotifier().enabled is False


@pytest.mark.asyncio
async def test_fanout_dispatches_to_enabled_children_only():
    on1, on2, off = _FakeChild(True), _FakeChild(True), _FakeChild(False)
    ok = await FanoutNotifier(on1, on2, off).send("alert")
    assert ok is True
    assert on1.sent == ["alert"] and on2.sent == ["alert"]
    assert off.sent == []  # disabled child never called


@pytest.mark.asyncio
async def test_fanout_one_failure_does_not_block_others():
    boom = _FakeChild(True, raises=True)
    good = _FakeChild(True, result=True)
    ok = await FanoutNotifier(boom, good).send("alert")
    assert ok is True  # good still delivered despite boom raising
    assert good.sent == ["alert"]


@pytest.mark.asyncio
async def test_fanout_none_children_ignored():
    good = _FakeChild(True)
    assert await FanoutNotifier(None, good, None).send("x") is True
