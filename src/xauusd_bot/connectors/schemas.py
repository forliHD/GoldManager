"""Canonical Pydantic schemas for the connector layer.

All numeric fields use ``Decimal`` for price/size to avoid float drift; ``float``
is used for derived quantities (profit, margin) where rounding error is acceptable.

Datetime fields are timezone-aware UTC ``datetime`` objects. The connector layer
guarantees this by converting any naive timestamps to UTC at parse time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderSide(str, Enum):
    """Order side."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Order type — kept tight to the subset we actually trade."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderTimeInForce(str, Enum):
    """Time-in-force policy."""

    GTC = "GTC"  # good-till-cancel
    IOC = "IOC"  # immediate-or-cancel
    FOK = "FOK"  # fill-or-kill
    DAY = "DAY"  # session-day


class FillPolicy(str, Enum):
    """Fill policy (paper-broker / live)."""

    PAPER = "paper"
    LIVE = "live"


class Bar(BaseModel):
    """OHLCV bar (any timeframe)."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    symbol: str
    timeframe: str = Field(description="e.g. 'M1', 'M5', 'H1', 'D1'")
    time: datetime = Field(description="Bar open time, UTC.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = Field(ge=0, description="Tick count inside the bar (relative only).")
    real_volume: int | None = Field(default=None, ge=0, description="Real volume if broker provides it.")
    spread: Decimal | None = Field(default=None, description="Average spread in points during the bar.")

    @field_validator("time")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Bar.time must be timezone-aware (UTC).")
        return v.astimezone(tz=__import__("datetime").timezone.utc)


class Tick(BaseModel):
    """Single tick (quote update)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    time: datetime
    bid: Decimal
    ask: Decimal
    last: Decimal | None = None
    volume: int = Field(default=0, ge=0)
    flags: int = Field(default=0, description="MT5 tick flags (raw int, opaque to consumers).")

    @field_validator("time")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Tick.time must be timezone-aware (UTC).")
        return v.astimezone(tz=__import__("datetime").timezone.utc)

    @property
    def spread(self) -> Decimal:
        """Bid/ask spread in price units."""

        return self.ask - self.bid


class SymbolSpec(BaseModel):
    """Static symbol specification (point size, contract size, limits)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    description: str = ""
    point: Decimal = Field(description="Smallest price increment (e.g. 0.01 for XAUUSD CFDs, 0.001 for FX).")
    digits: int = Field(ge=0, description="Number of decimal places.")
    trade_contract_size: Decimal = Field(description="Units per lot (e.g. 100 oz for XAUUSD).")
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    price_limit_max: Decimal | None = None
    price_limit_min: Decimal | None = None
    margin_rate: Decimal = Field(default=Decimal("0.01"), description="Margin requirement rate.")
    currency_base: str = "XAU"
    currency_profit: str = "USD"
    currency_margin: str = "USD"
    # Suggested safety thresholds (connector may override at runtime):
    spread_max_warn_points: int = Field(default=50, description="Warn if spread > this many points.")
    spread_max_block_points: int = Field(default=120, description="Block new entries if spread > this.")


class AccountInfo(BaseModel):
    """Account snapshot (balance, equity, margin, free margin)."""

    model_config = ConfigDict(extra="forbid")

    login: int | str
    broker: str
    currency: str = "USD"
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    leverage: int = 100
    server_time: datetime
    trade_allowed: bool = True
    raw: dict[str, Any] = Field(default_factory=dict, description="Raw broker payload for diagnostics.")


class OrderRequest(BaseModel):
    """Order intent — the connector turns this into an actual fill (or rejection)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: OrderSide
    type: OrderType
    volume: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, description="Required for LIMIT/STOP, ignored for MARKET.")
    sl: Decimal | None = None
    tp: Decimal | None = None
    deviation_points: int | None = Field(default=None, ge=0, description="Max slippage in points.")
    magic: int = 0
    comment: str = ""
    time_in_force: OrderTimeInForce = OrderTimeInForce.GTC
    fill_policy: FillPolicy = FillPolicy.LIVE
    client_order_id: str | None = Field(default=None, description="Idempotency key (ReplayConnector).")


class OrderResult(BaseModel):
    """Result of an order attempt — filled, rejected, or pending."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    order_id: str | None = None
    client_order_id: str | None = None
    filled_volume: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    slippage_points: Decimal | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Position(BaseModel):
    """Open position snapshot."""

    model_config = ConfigDict(extra="forbid")

    position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    open_price: Decimal
    sl: Decimal | None = None
    tp: Decimal | None = None
    open_time: datetime
    profit: Decimal = Decimal("0")
    swap: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    comment: str = ""
    magic: int = 0
