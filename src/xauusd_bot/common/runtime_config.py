"""Runtime-mutable config shared between the dashboard and the services.

Some settings must be flippable at runtime without restarting a
container — e.g. an operator toggling the AI decision layer on/off from
the dashboard. Those live as keys on the **trading** Redis (DB 0, the
same instance the services consume streams from), NOT on the dashboard
session Redis (DB 1). The dashboard writes; the decision-engine reads.

A missing key means "use the static :class:`Settings` default", so a
fresh deployment behaves exactly as its ``.env`` says until someone
flips a toggle.
"""

from __future__ import annotations

import json
from typing import Any

# Key namespace on the trading Redis. Keep the prefix stable — both the
# writer (dashboard) and the reader (decision-engine) hard-code it.
RUNTIME_KEY_AI_ENABLED = "runtime:ai_layer_enabled"
# Operator kill-switch: dashboard sets it, execution-engine reads it and
# flattens + halts new entries.
RUNTIME_KEY_EMERGENCY_STOP = "runtime:emergency_stop"

# Operational state snapshots the execution-engine publishes for the
# dashboard (TTL'd so stale state disappears if the publisher dies).
STATE_KEY_ACCOUNT = "state:account"
STATE_KEY_POSITIONS = "state:positions"
STATE_KEY_RISK = "state:risk"
STATE_TTL_SECONDS = 15

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def coerce_bool(value: Any) -> bool | None:
    """Parse a redis string/bytes value into a bool, or None if unset/unknown."""

    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    s = str(value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


async def get_ai_enabled(redis_client: Any, *, default: bool) -> bool:
    """Return the runtime AI-layer flag, falling back to ``default`` when unset."""

    raw = await redis_client.get(RUNTIME_KEY_AI_ENABLED)
    parsed = coerce_bool(raw)
    return default if parsed is None else parsed


async def set_ai_enabled(redis_client: Any, enabled: bool) -> None:
    """Persist the runtime AI-layer flag on the trading Redis."""

    await redis_client.set(RUNTIME_KEY_AI_ENABLED, "true" if enabled else "false")


async def get_emergency_stop(redis_client: Any) -> bool:
    """Return the operator kill-switch flag (default False when unset)."""

    return bool(coerce_bool(await redis_client.get(RUNTIME_KEY_EMERGENCY_STOP)))


async def set_emergency_stop(redis_client: Any, engaged: bool) -> None:
    """Engage/clear the operator kill-switch on the trading Redis."""

    await redis_client.set(RUNTIME_KEY_EMERGENCY_STOP, "true" if engaged else "false")


# Cumulative OpenRouter usage (a Redis hash: calls / prompt_tokens /
# completion_tokens). The decision-engine increments; the dashboard reads.
USAGE_KEY_OPENROUTER = "usage:openrouter"


async def record_llm_usage(redis_client: Any, usage: dict[str, Any]) -> None:
    """Increment the cumulative OpenRouter usage counters (best-effort)."""

    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    await redis_client.hincrby(USAGE_KEY_OPENROUTER, "calls", 1)
    if pt:
        await redis_client.hincrby(USAGE_KEY_OPENROUTER, "prompt_tokens", pt)
    if ct:
        await redis_client.hincrby(USAGE_KEY_OPENROUTER, "completion_tokens", ct)


async def get_llm_usage(redis_client: Any) -> dict[str, int]:
    """Return cumulative OpenRouter usage counters (zeros when unset)."""

    h = await redis_client.hgetall(USAGE_KEY_OPENROUTER)
    out = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for k, v in (h or {}).items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            pass
    return out


async def set_json(redis_client: Any, key: str, obj: Any, *, ttl: int = STATE_TTL_SECONDS) -> None:
    """Write a JSON snapshot to ``key`` with a TTL (stale state self-expires)."""

    await redis_client.set(key, json.dumps(obj, default=str), ex=ttl)


async def get_json(redis_client: Any, key: str) -> Any | None:
    """Read a JSON snapshot from ``key`` (None if missing/expired)."""

    raw = await redis_client.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "RUNTIME_KEY_AI_ENABLED",
    "RUNTIME_KEY_EMERGENCY_STOP",
    "STATE_KEY_ACCOUNT",
    "STATE_KEY_POSITIONS",
    "STATE_KEY_RISK",
    "STATE_TTL_SECONDS",
    "USAGE_KEY_OPENROUTER",
    "coerce_bool",
    "get_ai_enabled",
    "get_emergency_stop",
    "get_json",
    "get_llm_usage",
    "record_llm_usage",
    "set_ai_enabled",
    "set_emergency_stop",
    "set_json",
]
