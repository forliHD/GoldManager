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
    JOURNAL_WRITER = "journal-writer"
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
    openrouter_model: str = Field(default="minimax/minimax-m3", description="Model string on OpenRouter.")
    # Provider routing — pin OpenRouter to a specific upstream provider so a
    # Bring-Your-Own-Key actually reaches that provider instead of OpenRouter
    # picking a cheaper reseller. Comma-separated provider slugs (see the
    # model's /endpoints list); empty string disables pinning. Default pins
    # MiniMax's own endpoint for the default minimax model + BYOK.
    openrouter_provider_order: str = Field(
        default="minimax/fp8",
        description="Comma-separated OpenRouter provider slugs to route to, in order. Empty = no pin.",
    )
    openrouter_allow_fallbacks: bool = Field(
        default=False,
        description="If False, OpenRouter must use a provider in openrouter_provider_order (no fallback to others).",
    )
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
    mt5_bridge_host: str = Field(
        default="mt5-terminal",
        description="Hostname of the RPyC MT5 bridge (the mt5-terminal container in prod).",
    )
    mt5_bridge_port: int = Field(default=18812, ge=1, le=65535, description="RPyC bridge port.")
    mt5_bridge_auth_key: SecretStr | None = Field(
        default=None, description="Optional shared secret for the RPyC bridge (MT5_BRIDGE_AUTH_KEY)."
    )

    # --- Service runtime (stream-connected services, see common/service.py)
    replay_source: str = Field(
        default="data/sample/xauusd_m1_sample.parquet",
        description="Path to the parquet/CSV the data-collector replays in CONNECTOR_MODE=replay.",
    )
    replay_speed_seconds: float = Field(
        default=0.0,
        ge=0,
        description="Seconds to sleep between replayed bars. 0 = as fast as possible (default).",
    )
    replay_loop: bool = Field(
        default=False,
        description="When True the data-collector restarts the replay from the top after exhausting the source; when False it idles until shutdown.",
    )
    warmup_bars: int = Field(
        default=500,
        ge=0,
        description="Bars the feature-engine fetches from the connector at startup to seed its buffer (live mode only; replay fills from the stream).",
    )
    max_history_bars: int = Field(
        default=200_000,
        ge=1,
        description="Upper bound on the feature-engine's in-memory bar buffer. Note: yearly volume-range needs long history — see AGENTS.md.",
    )
    stream_block_ms: int = Field(
        default=1000, ge=1, description="XREADGROUP block timeout (ms) for service consumers."
    )
    stream_batch_size: int = Field(
        default=64, ge=1, description="Max messages a service consumer fetches per iteration."
    )

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

    # --- Dashboard (Block 9 — Custom Web-Dashboard, FastAPI backend).
    # Default OFF: operators must explicitly enable. See AGENTS.md §4j.
    dashboard_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the FastAPI dashboard. When False, all endpoints "
            "except /api/health return 404. Default off — must be explicitly enabled."
        ),
    )
    dashboard_host: str = Field(
        default="127.0.0.1",
        description="Bind host for the dashboard uvicorn server. Loopback-only by default.",
    )
    dashboard_port: int = Field(
        default=8080, ge=1, le=65535, description="Bind port for the dashboard."
    )
    dashboard_users: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Map of dashboard username -> {password_hash (bcrypt), role}. "
            "Roles: viewer | operator | admin. Default empty (no users). "
            "See AGENTS.md §4j.4 for role semantics."
        ),
    )
    dashboard_session_ttl_seconds: int = Field(
        default=8 * 3600, ge=60, description="Session TTL in seconds (default 8h)."
    )
    dashboard_redis_url: str = Field(
        default="redis://localhost:6379/1",
        description=(
            "Redis URL for dashboard sessions. Uses DB 1 by default so dashboard "
            "session writes do NOT collide with trading Redis Streams on DB 0 "
            "(see AGENTS.md §4j.3)."
        ),
    )
    dashboard_redis_streams_url: str | None = Field(
        default=None,
        description=(
            "Redis URL for dashboard WebSocket streams (subscribed market_ticks, "
            "features, decisions, orders, journal). Default: same as settings.redis_url."
        ),
    )
    dashboard_live_mode_enabled: bool = Field(
        default=False,
        description=(
            "Master switch to ALLOW /api/mode/toggle → live. When False, the toggle "
            "refuses even admin requests. Default off — see AGENTS.md §4j.5."
        ),
    )

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
