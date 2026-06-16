"""Pydantic-Settings — typed configuration loaded from .env / env.

Fail-fast: missing required keys raise at startup, not in the middle of
a trade. Each setting is documented; the model dump is what the
:func:`xauusd_bot.common.logging.setup.setup_logging` function uses
to enrich every log line.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConnectorMode(str, Enum):
    """Connector selection — see ``00_FINAL_PLAN.md`` §10."""

    REPLAY = "replay"
    LIVE = "live"


class NewsProvider(str, Enum):
    """News / macro calendar provider."""

    TRADING_ECONOMICS = "tradingeconomics"
    FXSTREET = "fxstreet"
    STUB = "stub"


class ServiceRole(str, Enum):
    """Which service a container is running as — picked by docker/service entrypoint."""

    DATA_COLLECTOR = "data-collector"
    FEATURE_ENGINE = "feature-engine"
    DECISION_ENGINE = "decision-engine"
    EXECUTION_ENGINE = "execution-engine"
    REVIEW = "review"


class Settings(BaseSettings):
    """Top-level typed configuration.

    Reads from:
    * process environment
    * ``.env`` in the current working directory
    * ``.env.example`` is shipped as a *template* and is NOT auto-loaded.

    Required keys (validated at startup, fail-fast):
        ``REDIS_URL``, ``TIMESCALEDB_URL``, ``SYMBOL``
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- AI / Decision layer
    openrouter_api_key: SecretStr | None = Field(default=None, description="OpenRouter BYOK key.")
    openrouter_model: str = Field(default="minimax/minimax-m2", description="Model string on OpenRouter.")
    # Block 6 — AIDecisionLayer settings. The AI layer is enabled by
    # default but the orchestrator will short-circuit to RuleBasedFallback
    # when ``ai_layer_enabled`` is False OR when ``openrouter_api_key`` is
    # unset OR when ``score.total < ai_layer_score_threshold``.
    ai_layer_enabled: bool = Field(
        default=True,
        description="Master switch for the AI decision layer. When False, the orchestrator always uses RuleBasedFallback.",
    )
    ai_layer_score_threshold: int = Field(
        default=65,
        ge=0,
        le=100,
        description="Only call the LLM when score.total >= this threshold. Default 65 = 'prepare' band and above.",
    )
    ai_layer_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Hard timeout per OpenRouter HTTP call (seconds).",
    )
    ai_layer_zdr: bool = Field(
        default=True,
        description=(
            "Zero-Data-Retention routing on OpenRouter. Per the official OpenRouter docs, ZDR is a body-level "
            "flag (provider.zdr=True) and we also set provider.data_collection='deny'. There is no "
            "X-Privacy-Mode header in the OpenRouter API as of 2026-01."
        ),
    )

    # --- Messaging / storage
    redis_url: str = Field(..., description="Redis connection URL.")
    timescaledb_url: str = Field(..., description="TimescaleDB async SQLAlchemy URL.")

    # --- Connector
    connector_mode: ConnectorMode = Field(default=ConnectorMode.REPLAY)
    symbol: str = Field(default="XAUUSD")
    vantage_data_dir: str = Field(default="/var/lib/xauusd/vantage")

    # --- News
    news_api_provider: NewsProvider = Field(default=NewsProvider.STUB)
    news_api_key: SecretStr | None = Field(default=None)

    # --- MT5 (prod only)
    mt5_login: str | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None

    # --- Risk (fractions, e.g. 0.04 = 4%)
    risk_max_daily: float = Field(default=0.04, ge=0, le=1)
    risk_max_weekly: float = Field(default=0.08, ge=0, le=1)
    risk_max_open_positions: int = Field(default=3, ge=1, description="Max simultaneously open positions (Block 4 limit).")
    risk_max_trades_per_session: int = Field(default=5, ge=1, description="Max new trades per UTC day (Block 4 limit).")
    # --- Spread block threshold (pips). RuleBasedFallback blocks entries
    # when AccountInfo.current_spread > spread_max_pips * 10 (XAUUSD pip = 10 points).
    spread_max_pips: float = Field(default=3.0, ge=0, description="Max spread in pips before blocking new entries.")

    # --- Service selection
    service_role: ServiceRole = Field(default=ServiceRole.DATA_COLLECTOR)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    environment: Literal["development", "test", "production"] = "development"

    @field_validator("risk_max_weekly")
    @classmethod
    def _weekly_geq_daily(cls, v: float, info) -> float:
        daily = info.data.get("risk_max_daily", 0.0)
        if v < daily:
            raise ValueError(f"risk_max_weekly ({v}) must be >= risk_max_daily ({daily})")
        return v

    def is_prod(self) -> bool:
        return self.environment == "production"

    def is_live_connector(self) -> bool:
        return self.connector_mode == ConnectorMode.LIVE

    def require_openrouter(self) -> SecretStr:
        if self.openrouter_api_key is None:
            raise RuntimeError("OPENROUTER_API_KEY is required for the AI decision layer")
        return self.openrouter_api_key


def load_settings() -> Settings:
    """Construct :class:`Settings` from current environment, with validation."""

    return Settings()  # type: ignore[call-arg]
