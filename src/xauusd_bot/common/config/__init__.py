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

    FOREX_FACTORY = "forexfactory"  # free weekly JSON calendar, no API key
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
    ai_layer_max_fvg_zones: int = Field(
        default=25,
        ge=3,
        le=500,
        description=(
            "Cap the number of FVG zones sent to the LLM (top-N by rank_score, the same "
            "metric behind top_zones). The bundle can carry 100+ mostly-stale zones — sending "
            "all of them is ~85% of the prompt tokens and pure noise. Zone *validation* still "
            "runs against the full bundle, so a smaller payload never invalidates the LLM's pick."
        ),
    )
    ai_layer_reasoning_enabled: bool = Field(
        default=False,
        description=(
            "Static default for the LLM reasoning toggle. When False the OpenRouter client "
            "sends reasoning:{enabled:false} (no chain-of-thought) — ~halves m3 latency at the "
            "cost of analytical depth. Operators flip this at runtime from the dashboard "
            "(runtime:llm_reasoning_enabled on the trading Redis); this is only the boot default. "
            "Default is False: minimax-m3 reasoning-ON runs ~55s (TTFT ~8s + ~3.6k CoT tokens) "
            "and blows past ai_layer_timeout_seconds. Any client built WITHOUT usage_redis (tools, "
            "tests, backtest runners) reads this default, so True would silently send reasoning=True."
        ),
    )
    ai_layer_max_attempts: int = Field(
        default=1,
        ge=1,
        le=6,
        description=(
            "Total LLM attempts per decision before falling back to the rule. Retries cover "
            "transient validation/empty-body/timeout errors (same provider — no ZDR change). "
            "1 = no retry. Kept at 1 so a 30s timeout never compounds to >60s and backs up the "
            "1-bar/min decision loop."
        ),
    )
    ai_layer_retry_backoff_seconds: float = Field(
        default=0.4,
        ge=0.0,
        le=5.0,
        description="Base delay between LLM retries (grows linearly: 0.4s, 0.8s, ...).",
    )
    ai_layer_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Hard total timeout per OpenRouter HTTP call (seconds, enforced via asyncio.wait_for).",
    )
    ai_layer_zdr: bool = Field(
        default=False,
        description=(
            "Zero-Data-Retention routing on OpenRouter (body flag provider.zdr=true). "
            "OFF by default because it is INCOMPATIBLE with the default MiniMax provider "
            "pin: MiniMax's OpenRouter endpoint (minimax/fp8) is not ZDR-certified, so "
            "zdr=true + the pin returns 404 'no endpoints'. provider.data_collection='deny' "
            "is sent regardless (privacy-preserving and MiniMax-compatible). Enable ZDR only "
            "with a ZDR-listed model and openrouter_allow_fallbacks=true (which may route away "
            "from MiniMax / your BYOK)."
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
    news_currencies: list[str] = Field(
        default_factory=lambda: ["USD"],
        description="Currencies whose calendar events drive the news blackout. USD is the dominant XAUUSD driver (NFP/FOMC/CPI).",
    )

    # --- Alerting (Telegram) — live push notifications for orders/management/emergency
    telegram_bot_token: SecretStr | None = Field(
        default=None, description="Telegram bot token (@BotFather). Alerts disabled when unset."
    )
    telegram_chat_id: str | None = Field(
        default=None, description="Telegram chat id to send alerts to."
    )
    telegram_alerts_enabled: bool = Field(
        default=True, description="Master switch for Telegram alerts (effective only if token+chat set)."
    )

    # --- Web Push (mobile PWA notifications)
    vapid_public_key: str | None = Field(
        default=None,
        description="VAPID public key (base64url) the PWA uses to subscribe. Push disabled when unset.",
    )
    vapid_private_key: SecretStr | None = Field(
        default=None, description="VAPID private key (base64url PEM). Server-side only; never commit."
    )
    vapid_subject: str = Field(
        default="mailto:admin@goldmanager.local",
        description="VAPID 'sub' claim — a mailto: or https: contact for the push service.",
    )
    webpush_enabled: bool = Field(
        default=True, description="Master switch for Web Push (effective only if VAPID keys set)."
    )

    # --- MT5 (prod only)
    mt5_login: str | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None
    mt5_bridge_kind: Literal["mt5linux", "rpyc"] = Field(
        default="mt5linux",
        description=(
            "Which live bridge to talk to. 'mt5linux' = the gmag11/metatrader5_vnc "
            "container's mt5linux RPyC server (port 8001, attach-mode — operator logs in "
            "via KasmVNC, no MT5_* creds required). 'rpyc' = our own mt5_bridge_server "
            "(port 18812, programmatic login with MT5_LOGIN/PASSWORD/SERVER)."
        ),
    )
    mt5_bridge_host: str = Field(
        default="mt5-terminal",
        description="Hostname of the MT5 bridge (the mt5-terminal container in prod).",
    )
    mt5_bridge_port: int = Field(
        default=8001,
        ge=1,
        le=65535,
        description="Bridge port. 8001 for mt5linux (default), 18812 for the rpyc bridge.",
    )
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
        default=1500,
        ge=0,
        description=(
            "Bars the feature-engine fetches from the connector at startup to seed its buffer "
            "(live mode only; replay fills from the stream). Must span the current day's 00:00 "
            "anchor so the three anchored VWAPs (00:00/07:00/12:00) are distinct right after a "
            "restart — with too few bars all anchors collapse to the buffer start and read "
            "identical. ~1500 M1 ≈ 25h covers it regardless of restart time."
        ),
    )
    chart_history_bars: int = Field(
        default=1500,
        ge=0,
        description=(
            "On live start, the data-collector backfills this many historical M1 bars into "
            "the CHART_HISTORY stream (dashboard chart context only — NOT market_ticks, so "
            "the trading pipeline never sees history). Once, gated on stream length. "
            "0 disables. ~1500 ≈ 25 H1 candles."
        ),
    )
    max_history_bars: int = Field(
        default=200_000,
        ge=1,
        description="Upper bound on the feature-engine's MAIN in-memory bar buffer (FVG/structure/vwap/…). Keep modest: FVG is ~O(n²).",
    )
    volume_profile_history_bars: int = Field(
        default=0,
        ge=0,
        description=(
            "Separate DEEP bar history fed only to the Volume Profile so its locked "
            "Daily/Weekly/Monthly ranges span whole completed periods. 0 = off (volume_range "
            "uses the main buffer). Live: ~90000 covers >2 months. volume_range is cheap even "
            "over deep history (~0.7s/80k); the main buffer stays small so FVG doesn't blow up."
        ),
    )
    stream_block_ms: int = Field(
        default=1000, ge=1, description="XREADGROUP block timeout (ms) for service consumers."
    )
    stream_batch_size: int = Field(
        default=64, ge=1, description="Max messages a service consumer fetches per iteration."
    )
    stream_maxlen: int = Field(
        default=50_000,
        ge=1,
        description=(
            "Approximate MAXLEN cap (XADD ~) for small-payload streams "
            "(market_ticks ~350 bytes each). 50k ≈ 17 MB."
        ),
    )
    stream_maxlen_large: int = Field(
        default=1_500,
        ge=1,
        description=(
            "Approximate MAXLEN cap (XADD ~) for bundle-carrying streams "
            "(features/decisions). The published bundle is compacted before XADD "
            "(see compact_bundle) so each event is now tens of KB rather than "
            "~800 KB; this cap stays as a safety net so a regression in payload "
            "size cannot OOM Redis."
        ),
    )
    bundle_compact_max_swings: int = Field(
        default=50,
        ge=1,
        description=(
            "Transport compaction: how many of the most-recent structure swings to "
            "keep in the published FeatureSnapshotBundle. Execution only reads the "
            "latest swing high/low (see execution.stops._last_swing), so the long "
            "history tail is dropped. Set high enough to always include the most "
            "recent high and low."
        ),
    )
    bundle_compact_max_mitigated_zones_per_tf: int = Field(
        default=10,
        ge=0,
        description=(
            "Transport compaction: how many fully-mitigated FVG zones to keep per "
            "timeframe in the published bundle. Open/partially-mitigated zones are "
            "always kept; the (mostly M1) mitigated tail is the bulk of the payload. "
            "Kept above the aggregator's mitigated-count thresholds (>5) so decision "
            "scoring is preserved."
        ),
    )

    # --- Risk (fractions, e.g. 0.04 = 4%)
    risk_max_daily: float = Field(default=0.04, ge=0, le=1)
    risk_max_weekly: float = Field(default=0.08, ge=0, le=1)
    risk_max_open_positions: int = Field(default=3, ge=1, description="Max simultaneously open positions (Block 4 limit).")
    risk_max_trades_per_session: int = Field(default=5, ge=1, description="Max new trades per UTC day (Block 4 limit).")
    zone_lock_enabled: bool = Field(
        default=True,
        description="One entry per zone/setup: block a second entry into the same price band "
        "while a position is open / used; a zone dies on an H1 close beyond it. Kills stacked entries.",
    )
    zone_lock_atr_mult: float = Field(
        default=0.5,
        ge=0,
        description="Half-width of the zone band as a multiple of ATR (when no explicit entry zone).",
    )
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
    # ---- Cloudflare Access SSO (pass-through) --------------------------------
    cf_access_enabled: bool = Field(
        default=False,
        description="Accept a verified Cloudflare Access JWT as login (Google/Azure SSO), bypassing the local password. Off = unchanged.",
    )
    cf_access_team_domain: str | None = Field(
        default=None,
        description="Cloudflare Access team domain, e.g. 'name.cloudflareaccess.com'. Used for the JWKS URL + issuer.",
    )
    cf_access_aud: str | None = Field(
        default=None,
        description="Cloudflare Access Application Audience (AUD) tag the JWT must match. Identifier, not a secret.",
    )
    cf_access_default_role: Literal["viewer", "operator", "admin"] = Field(
        default="admin",
        description="Dashboard role granted to any user who authenticates via Cloudflare Access.",
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
