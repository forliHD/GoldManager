"""Unit tests for the service-runtime scaffolding and stream envelopes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from xauusd_bot.common.config import ServiceRole
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    BarClosedEvent,
    envelope_for_topic,
)
from xauusd_bot.common.messaging.streams import StreamTopic, _from_json, _to_json
from xauusd_bot.common.service import heartbeat_path, service_runtime
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.docker_entrypoint import _DISPATCH


def _bar() -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        open=Decimal("2360.0"),
        high=Decimal("2361.0"),
        low=Decimal("2359.0"),
        close=Decimal("2360.5"),
        tick_volume=42,
    )


def test_bar_closed_event_round_trips_through_stream_helpers():
    ev = BarClosedEvent(symbol="XAUUSD", bar=_bar())
    wire = _to_json(ev)
    assert set(wire) == {"payload"}  # flat {key: str} shape Redis wants
    back = _from_json(wire, BarClosedEvent)
    assert back.kind == "bar_closed"
    assert back.schema_version == ENVELOPE_SCHEMA_VERSION
    assert back.bar.close == Decimal("2360.5")
    assert back.bar.time == ev.bar.time


def test_envelope_for_topic_covers_every_stream_topic():
    mapping = envelope_for_topic()
    assert set(mapping) == {t.value for t in StreamTopic}
    assert mapping[StreamTopic.MARKET_TICKS.value] is BarClosedEvent


def test_dispatch_table_wires_the_five_streaming_services():
    roles = set(_DISPATCH)
    assert roles == {
        ServiceRole.DATA_COLLECTOR,
        ServiceRole.FEATURE_ENGINE,
        ServiceRole.DECISION_ENGINE,
        ServiceRole.EXECUTION_ENGINE,
        ServiceRole.JOURNAL_WRITER,
    }
    # REVIEW is intentionally an on-demand CLI, not a streaming daemon.
    assert ServiceRole.REVIEW not in roles


def test_heartbeat_path_uses_role_value():
    assert heartbeat_path(ServiceRole.FEATURE_ENGINE).name == "feature-engine.alive"
    assert heartbeat_path("custom").name == "custom.alive"


def test_service_runtime_writes_heartbeat_and_stops_cleanly(tmp_path):
    async def scenario() -> bool:
        async with service_runtime(
            ServiceRole.DATA_COLLECTOR, heartbeat_interval=0.05, heartbeat_dir=tmp_path
        ) as stop:
            await asyncio.sleep(0.12)
            written = (tmp_path / "data-collector.alive").exists()
            stop.set()
        return written

    assert asyncio.run(scenario()) is True
    # The heartbeat file persists after shutdown (last-seen marker).
    assert (tmp_path / "data-collector.alive").exists()
