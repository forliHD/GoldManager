"""Tests for the Web Push notifier (fakeredis + a stubbed pywebpush module)."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import fakeredis.aioredis
import pytest

from xauusd_bot.common.webpush import PUSH_SUBSCRIPTIONS_KEY, WebPushNotifier


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _sub(endpoint: str) -> dict:
    return {"endpoint": endpoint, "keys": {"p256dh": "x", "auth": "y"}}


@pytest.fixture
def stub_pywebpush(monkeypatch):
    """Install a fake ``pywebpush`` module; returns a list recording calls.

    Configure per-endpoint failures via ``calls.fail_status`` (endpoint -> code).
    """

    calls: list[dict] = []
    fail_status: dict[str, int] = {}

    class WebPushException(Exception):
        def __init__(self, msg, response=None):
            super().__init__(msg)
            self.response = response

    def webpush(*, subscription_info, data, vapid_private_key, vapid_claims, timeout=8):
        ep = subscription_info["endpoint"]
        calls.append({"endpoint": ep, "data": data})
        code = fail_status.get(ep)
        if code:
            raise WebPushException("fail", response=SimpleNamespace(status_code=code))

    mod = ModuleType("pywebpush")
    mod.webpush = webpush
    mod.WebPushException = WebPushException
    monkeypatch.setitem(sys.modules, "pywebpush", mod)
    return SimpleNamespace(calls=calls, fail_status=fail_status)


def test_disabled_when_keys_unset(redis):
    assert WebPushNotifier(redis, vapid_public_key=None, vapid_private_key=None).enabled is False
    assert WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key=None).enabled is False
    assert WebPushNotifier(None, vapid_public_key="pub", vapid_private_key="priv").enabled is False


def test_enabled_when_configured(redis):
    assert WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key="priv").enabled is True


@pytest.mark.asyncio
async def test_add_remove_count(redis):
    wp = WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key="priv")
    assert await wp.add_subscription(_sub("https://push/1")) is True
    assert await wp.add_subscription(_sub("https://push/2")) is True
    assert await wp.subscription_count() == 2
    await wp.remove_subscription("https://push/1")
    assert await wp.subscription_count() == 1
    # missing endpoint -> not stored
    assert await wp.add_subscription({"keys": {}}) is False


@pytest.mark.asyncio
async def test_send_noop_when_disabled_or_empty(redis, stub_pywebpush):
    disabled = WebPushNotifier(redis, vapid_public_key=None, vapid_private_key=None)
    assert await disabled.send("hi") is False
    enabled_empty = WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key="priv")
    assert await enabled_empty.send("hi") is False  # no subscriptions
    assert stub_pywebpush.calls == []


@pytest.mark.asyncio
async def test_send_delivers_and_strips_html(redis, stub_pywebpush):
    wp = WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key="priv")
    await wp.add_subscription(_sub("https://push/1"))
    await wp.add_subscription(_sub("https://push/2"))
    ok = await wp.send("🔴 <b>ORDER REJECTED</b> · XAUUSD")
    assert ok is True
    assert len(stub_pywebpush.calls) == 2
    import json

    body = json.loads(stub_pywebpush.calls[0]["data"])
    assert body["body"] == "🔴 ORDER REJECTED · XAUUSD"  # HTML stripped
    assert body["title"] == "GoldManager"


@pytest.mark.asyncio
async def test_send_prunes_gone_subscriptions(redis, stub_pywebpush):
    wp = WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key="priv")
    await wp.add_subscription(_sub("https://push/live"))
    await wp.add_subscription(_sub("https://push/dead"))
    stub_pywebpush.fail_status["https://push/dead"] = 410  # expired
    ok = await wp.send("ping")
    assert ok is True  # the live one delivered
    # dead subscription pruned, live kept
    remaining = await redis.hkeys(PUSH_SUBSCRIPTIONS_KEY)
    assert remaining == ["https://push/live"]


@pytest.mark.asyncio
async def test_send_keeps_subscription_on_transient_error(redis, stub_pywebpush):
    wp = WebPushNotifier(redis, vapid_public_key="pub", vapid_private_key="priv")
    await wp.add_subscription(_sub("https://push/1"))
    stub_pywebpush.fail_status["https://push/1"] = 500  # transient, not gone
    ok = await wp.send("ping")
    assert ok is False
    assert await wp.subscription_count() == 1  # NOT pruned on 5xx
