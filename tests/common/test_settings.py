"""Tests for the Pydantic Settings loader — fail-fast, env file, mode validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from xauusd_bot.common.config import (
    ConnectorMode,
    NewsProvider,
    ServiceRole,
    Settings,
    load_settings,
)

# ---------------------------------------------------------------- env loading


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings reads REDIS_URL / TIMESCALEDB_URL from env (the autouse
    fixture sets them). All other fields have defaults."""

    s = load_settings()
    assert s.redis_url.startswith("redis://")
    assert s.timescaledb_url.startswith("postgresql")
    # Defaults
    assert s.symbol == "XAUUSD"
    assert s.connector_mode == ConnectorMode.REPLAY
    assert s.service_role == ServiceRole.DATA_COLLECTOR
    assert s.environment == "test"
    # Warmup must span a full trading day (1440 M1 bars) so the three anchored
    # VWAPs (00:00/07:00/12:00) are distinct right after a feature-engine
    # restart — too few bars collapse every anchor onto the buffer start.
    assert s.warmup_bars >= 1440


def test_settings_loads_from_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A .env file in the current directory is loaded by pydantic-settings."""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "REDIS_URL=redis://from-env-file:6379/0\n"
        "TIMESCALEDB_URL=postgresql+asyncpg://u:p@from-env-file:5432/d\n"
        "SYMBOL=FROM_FILE\n"
    )
    monkeypatch.chdir(tmp_path)
    # Reset any existing env vars so the .env takes effect.
    for k in ("REDIS_URL", "TIMESCALEDB_URL", "SYMBOL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.redis_url == "redis://from-env-file:6379/0"
    assert s.timescaledb_url == "postgresql+asyncpg://u:p@from-env-file:5432/d"
    assert s.symbol == "FROM_FILE"


# ---------------------------------------------------------------- fail-fast


def test_missing_redis_url_raises_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing REDIS_URL is a Pydantic ValidationError, not a silent default."""

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TIMESCALEDB_URL", raising=False)
    with pytest.raises(ValidationError) as ei:
        # _env_file=None neutralizes any repo-root .env so the
        # missing-required-var assertion holds regardless of local files.
        Settings(_env_file=None)  # type: ignore[call-arg]
    # The error must mention the missing field.
    errors = ei.value.errors()
    fields = {tuple(e["loc"]) for e in errors}
    assert any("redis_url" in str(f) for f in fields) or any(
        "TIMESCALEDB_URL" in str(e.get("msg", "")) for e in errors
    )


def test_missing_timescaledb_url_raises_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing TIMESCALEDB_URL is a Pydantic ValidationError."""

    monkeypatch.setenv("REDIS_URL", "redis://x:6379/0")
    monkeypatch.delenv("TIMESCALEDB_URL", raising=False)
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None)  # type: ignore[call-arg]
    errors = ei.value.errors()
    assert any("timescaledb_url" in str(tuple(e["loc"])) for e in errors)


def test_both_required_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both REDIS_URL and TIMESCALEDB_URL missing → ValidationError."""

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("TIMESCALEDB_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------- connector mode validation


def test_connector_mode_must_be_replay_or_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid CONNECTOR_MODE value must fail-fast at construction."""

    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("CONNECTOR_MODE", "invalid-mode")
    with pytest.raises(ValidationError) as ei:
        Settings()
    # The error must mention connector_mode.
    assert any("connector_mode" in str(tuple(e["loc"])) for e in ei.value.errors())


def test_connector_mode_replay_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("CONNECTOR_MODE", "replay")
    s = Settings()
    assert s.connector_mode == ConnectorMode.REPLAY
    assert s.is_live_connector() is False


def test_connector_mode_live_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("CONNECTOR_MODE", "live")
    s = Settings()
    assert s.connector_mode == ConnectorMode.LIVE
    assert s.is_live_connector() is True


# ---------------------------------------------------------------- risk invariants


def test_risk_max_weekly_must_be_geq_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator rejects weekly < daily."""

    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("RISK_MAX_DAILY", "0.10")
    monkeypatch.setenv("RISK_MAX_WEEKLY", "0.05")
    with pytest.raises(ValidationError) as ei:
        Settings()
    assert any("weekly" in str(e.get("msg", "")).lower() for e in ei.value.errors())


def test_risk_bounds_are_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Risk fractions outside [0, 1] are rejected by the field constraints."""

    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("RISK_MAX_DAILY", "1.5")  # > 1
    with pytest.raises(ValidationError):
        Settings()


# ---------------------------------------------------------------- other enums


def test_service_role_enum_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("SERVICE_ROLE", "decision-engine")
    s = Settings()
    assert s.service_role == ServiceRole.DECISION_ENGINE


def test_news_provider_enum_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("NEWS_API_PROVIDER", "tradingeconomics")
    s = Settings()
    assert s.news_api_provider == NewsProvider.TRADING_ECONOMICS


def test_environment_must_be_known_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """environment is a Literal — unknown values fail."""

    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("ENVIRONMENT", "staging")  # not in Literal
    with pytest.raises(ValidationError):
        Settings()


# ---------------------------------------------------------------- helpers


def test_is_prod_true_for_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("ENVIRONMENT", "production")
    s = Settings()
    assert s.is_prod() is True


def test_is_prod_false_for_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    s = Settings()
    assert s.is_prod() is False


def test_require_openrouter_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # _env_file=None so a local .env with OPENROUTER_API_KEY can't satisfy it.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        s.require_openrouter()


def test_require_openrouter_returns_secret_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    s = Settings()
    secret = s.require_openrouter()
    assert secret.get_secret_value() == "test-key-123"


def test_tp_sum_validator_does_not_mask_out_of_range_tp1(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review #9: when tp1 fails its own le=100 validation it is absent from
    # info.data; the sum validator must SKIP (not substitute a 30.0 default and
    # raise a MISLEADING "sum != 100"). Only the real tp1 bound error should show.
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("EXEC_TP1_PCT", "120")  # violates le=100
    monkeypatch.setenv("EXEC_TP2_PCT", "30")
    monkeypatch.setenv("EXEC_TP3_PCT", "50")
    with pytest.raises(ValidationError) as exc:
        Settings()
    msg = str(exc.value)
    assert "exec_tp1_pct" in msg          # the real le=100 error surfaces
    assert "must sum to 100" not in msg   # no misleading fallback-based sum error


def test_tp_sum_validator_still_catches_real_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: with all three in range but not summing to 100, the sum check fires.
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("TIMESCALEDB_URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("EXEC_TP1_PCT", "30")
    monkeypatch.setenv("EXEC_TP2_PCT", "30")
    monkeypatch.setenv("EXEC_TP3_PCT", "50")  # 30+30+50 = 110
    with pytest.raises(ValidationError, match="must sum to 100"):
        Settings()
