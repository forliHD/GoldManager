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

from typing import Any

# Key namespace on the trading Redis. Keep the prefix stable — both the
# writer (dashboard) and the reader (decision-engine) hard-code it.
RUNTIME_KEY_AI_ENABLED = "runtime:ai_layer_enabled"

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


__all__ = [
    "RUNTIME_KEY_AI_ENABLED",
    "coerce_bool",
    "get_ai_enabled",
    "set_ai_enabled",
]
