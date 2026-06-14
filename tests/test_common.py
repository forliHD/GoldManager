"""Common-layer tests: Settings, schemas, logging."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from xauusd_bot.common.config import (
    ConnectorMode,
    ServiceRole,
    Settings,
)
from xauusd_bot.common.logging import bind_correlation, get_logger, setup_logging
from xauusd_bot.common.schemas import (
    SCHEMA_VERSION,
    BarEvent,
    Decision,
    DecisionAction,
    FeatureSnapshot,
    JournalEntry,
    MarketData,
    OrderEvent,
    Side,
)

# --------------------------------------------------------------- Settings


def test_settings_loads_with_required_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    s = Settings()
    assert s.connector_mode == ConnectorMode.REPLAY
    assert s.symbol == "XAUUSD"
    assert s.service_role == ServiceRole.DATA_COLLECTOR


def test_settings_rejects_weekly_less_than_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("RISK_MAX_DAILY", "0.10")
    monkeypatch.setenv("RISK_MAX_WEEKLY", "0.05")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_parses_connector_mode_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("CONNECTOR_MODE", "live")
    s = Settings()
    assert s.connector_mode == ConnectorMode.LIVE
    assert s.is_live_connector() is True


def test_settings_openrouter_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    s = Settings()
    assert s.openrouter_api_key is None
    with pytest.raises(RuntimeError):
        s.require_openrouter()


# --------------------------------------------------------------- schemas


def test_schema_version_default() -> None:
    d = Decision(
        source="decision-engine",
        ts=datetime.now(tz=UTC),
        symbol="XAUUSD",
        action=DecisionAction.NO_TRADE,
        side=Side.NO_TRADE,
    )
    assert d.schema_version == SCHEMA_VERSION
    assert d.kind == "decision"


def test_bar_event_round_trip() -> None:
    e = BarEvent(
        source="data-collector",
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000.5"),
        tick_volume=10,
    )
    j = e.model_dump(mode="json")
    parsed = BarEvent.model_validate(j)
    assert parsed.symbol == e.symbol
    assert parsed.tick_volume == 10


def test_feature_snapshot_score_bounds() -> None:
    with pytest.raises(ValidationError):
        FeatureSnapshot(
            source="feature-engine",
            ts=datetime.now(tz=UTC),
            symbol="XAUUSD",
            bar_time=datetime.now(tz=UTC),
            score=150.0,  # out of [0, 100]
        )


def test_market_data_minimal() -> None:
    md = MarketData(
        source="data-collector",
        ts=datetime.now(tz=UTC),
        symbol="XAUUSD",
        last_bid=Decimal("2000"),
        last_ask=Decimal("2000.5"),
    )
    assert md.kind == "market_data"


def test_journal_entry_persists_payload() -> None:
    je = JournalEntry(
        source="execution-engine",
        ts=datetime.now(tz=UTC),
        symbol="XAUUSD",
        event_type="order_filled",
        payload={"order_id": "ord-1", "price": "2000.5"},
    )
    assert je.payload["order_id"] == "ord-1"


def test_order_event_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        OrderEvent(
            source="execution-engine",
            ts=datetime.now(tz=UTC),
            symbol="XAUUSD",
            status="exploded",  # not in the Literal
        )


# --------------------------------------------------------------- logging


def test_setup_logging_does_not_raise() -> None:
    setup_logging(level="DEBUG", json_output=False)
    log = get_logger("test")
    log.info("hello", foo="bar")
    # If we got here without an exception, the setup is fine.
    assert True


def test_bind_correlation_emits_context(capsys: pytest.CaptureFixture) -> None:
    setup_logging(level="INFO", json_output=True)
    log = get_logger("test")
    with bind_correlation(setup_id="setup-x", trade_id=None):
        log.info("inside_block")
    log.info("outside_block")
    captured = capsys.readouterr().out
    # At least one line should contain setup_id="setup-x"
    assert "setup-x" in captured
