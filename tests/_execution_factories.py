"""Shared test factories for xauusd_bot execution-layer tests (Block 4).

Plain module — not a conftest — so tests can ``from tests._execution_factories
import make_account`` etc. without conftest import gymnastics. The
pytest fixtures for these are exposed via ``tests/execution/conftest.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
    TradeQualification,
)
from xauusd_bot.common.schemas.execution import (
    OrderEnvelope,
    OrderTag,
)
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        "environment": "test",
    }
    base.update(overrides)
    return Settings(**base)


def make_account(
    *,
    balance: Decimal = Decimal("10000"),
    equity: Decimal = Decimal("10000"),
    margin: Decimal = Decimal("0"),
    free_margin: Decimal | None = None,
    leverage: int = 100,
    trade_allowed: bool = True,
    daily_pnl: Decimal | None = None,
    weekly_pnl: Decimal | None = None,
    current_spread: Decimal | None = None,
    server_time: datetime | None = None,
) -> AccountInfo:
    return AccountInfo(
        login="test",
        broker="test",
        balance=balance,
        equity=equity,
        margin=margin,
        free_margin=(free_margin if free_margin is not None else equity - margin),
        leverage=leverage,
        server_time=(server_time or datetime.now(tz=UTC)),
        trade_allowed=trade_allowed,
        daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl,
        current_spread=current_spread,
    )


def make_symbol_spec(
    *,
    point: Decimal = Decimal("0.01"),
    digits: int = 2,
    contract_size: Decimal = Decimal("100"),
    volume_min: Decimal = Decimal("0.01"),
    volume_max: Decimal = Decimal("100"),
    volume_step: Decimal = Decimal("0.01"),
) -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD",
        description="test XAUUSD",
        point=point,
        digits=digits,
        trade_contract_size=contract_size,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        margin_rate=Decimal("0.01"),
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
    )


def make_position(
    *,
    side: OrderSide = OrderSide.BUY,
    volume: Decimal = Decimal("0.10"),
    open_price: Decimal = Decimal("2375.00"),
    sl: Decimal | None = None,
    tp: Decimal | None = None,
    profit: Decimal = Decimal("0"),
    position_id: str = "pos-1",
    open_time: datetime | None = None,
) -> Position:
    return Position(
        position_id=position_id,
        symbol="XAUUSD",
        side=side,
        volume=volume,
        open_price=open_price,
        sl=sl,
        tp=tp,
        open_time=(open_time or datetime(2026, 4, 15, 13, 0, tzinfo=UTC)),
        profit=profit,
    )


def make_qualification(
    *,
    qualified: bool = True,
    action: DecisionAction = DecisionAction.ENTER_LONG,
    entry_type: EntryType = EntryType.FULL,
    block_reasons: list[str] | None = None,
    band: ScoreBand = ScoreBand.FULL_85_PLUS,
    score: float = 88.0,
    direction: str = "long",
    ts: datetime | None = None,
) -> TradeQualification:
    return TradeQualification(
        qualified=qualified,
        final_action=action,
        final_entry_type=entry_type,
        block_reasons=block_reasons or [],
        final_direction=direction,  # type: ignore[arg-type]
        source_score=score,
        source_band=band,
        timestamp=(ts or datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
    )


def make_order_request(
    *,
    side: OrderSide = OrderSide.BUY,
    type: OrderType = OrderType.MARKET,
    volume: Decimal = Decimal("0.10"),
    price: Decimal | None = None,
    sl: Decimal | None = None,
    tp: Decimal | None = None,
    client_order_id: str | None = None,
    symbol: str = "XAUUSD",
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        type=type,
        volume=volume,
        price=price,
        sl=sl,
        tp=tp,
        client_order_id=client_order_id,
    )


def make_order_envelope(
    *,
    client_order_id: str = "env-1",
    state: str = "filled",
    filled_volume: Decimal = Decimal("0.10"),
    avg_fill_price: Decimal | None = None,
) -> OrderEnvelope:
    return OrderEnvelope(
        client_order_id=client_order_id,
        setup_id=uuid4(),
        strategy_version="block4-test",
        engine_source=OrderTag.RULE_BASED,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        requested_volume=filled_volume,
        requested_price=Decimal("2375.00"),
        sl=Decimal("2370.00"),
        tp=Decimal("2380.00"),
        state=state,  # type: ignore[arg-type]
        order_id=("oid-1" if state == "filled" else None),
        filled_volume=filled_volume,
        avg_fill_price=avg_fill_price,
        created_at=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        updated_at=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )


def make_bar(
    *,
    time: datetime | None = None,
    close: Decimal = Decimal("2375.00"),
    high: Decimal = Decimal("2376.00"),
    low: Decimal = Decimal("2374.00"),
    open: Decimal | None = None,
) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=(time or datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        open=(open if open is not None else close),
        high=high,
        low=low,
        close=close,
        tick_volume=100,
    )


__all__ = [
    "make_account",
    "make_bar",
    "make_order_envelope",
    "make_order_request",
    "make_position",
    "make_qualification",
    "make_settings",
    "make_symbol_spec",
]
