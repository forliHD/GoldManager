"""Tests for OrderManager — Block 4 Phase 2 (the only `order_send` caller)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from xauusd_bot.common.schemas.execution import OrderEnvelope, OrderTag
from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.safety import (
    PreTradeSafetyChecker,
    SafetyAction,
    SafetyReason,
    SafetyThresholds,
    SafetyVerdict,
)
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
    Tick,
)
from xauusd_bot.execution.orders import OrderManager

from tests._execution_factories import make_account, make_order_request, make_symbol_spec


# ----------------------------------------------------------------- test stubs


class _StubConnector:
    """Minimal in-memory :class:`IMarketConnector` for unit tests."""

    def __init__(self, spec: SymbolSpec | None = None) -> None:
        self.symbol = "XAUUSD"
        self._spec = spec or make_symbol_spec()
        self._account = make_account()
        self.sent: list[OrderRequest] = []
        self.fail_next: bool = False

    def get_rates(self, symbol: str, timeframe: str, count: int, *, end_time: Any = None) -> list:  # noqa: ARG002
        return []

    def get_ticks(self, symbol: str, from_ts: Any, to_ts: Any) -> list[Tick]:  # noqa: ARG002
        return []

    def get_account(self) -> AccountInfo:
        return self._account

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._spec

    def order_send(self, request: OrderRequest) -> OrderResult:
        if self.fail_next:
            self.fail_next = False
            return OrderResult(
                accepted=False,
                order_id=None,
                client_order_id=request.client_order_id,
                error_code="BROKER_REJECT",
                error_message="simulated rejection",
            )
        self.sent.append(request)
        return OrderResult(
            accepted=True,
            order_id=f"oid-{len(self.sent)}",
            client_order_id=request.client_order_id,
            filled_volume=Decimal(request.volume),
            avg_fill_price=Decimal("2375.00"),
        )

    def positions_get(self, symbol: str | None = None) -> list[Position]:  # noqa: ARG002
        return []

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:  # noqa: ARG002
        return []

    def order_modify(  # noqa: ARG002
        self, order_id: str, *, price: float | None = None, sl: float | None = None, tp: float | None = None
    ) -> OrderResult:
        return OrderResult(accepted=True, order_id=order_id)

    def order_cancel(self, order_id: str) -> OrderResult:
        return OrderResult(accepted=True, order_id=order_id)

    def is_connected(self) -> bool:
        return True

    def shutdown(self) -> None:
        return None


def _safety_allowing() -> PreTradeSafetyChecker:
    return PreTradeSafetyChecker(
        get_account=lambda: make_account(),
        get_spread_points=lambda: 25.0,
        thresholds=SafetyThresholds(),
    )


def _safety_blocking() -> PreTradeSafetyChecker:
    """A safety checker that always returns BLOCK."""

    def _block(*args: Any, **kwargs: Any) -> SafetyVerdict:
        return SafetyVerdict(
            action=SafetyAction.BLOCK,
            reasons=[SafetyReason.FEED_OFFLINE],
            details={"feed": "stub"},
            checked_at=datetime.now(tz=UTC),
        )

    # We override .check via monkey-patching: easiest is to subclass.
    class _Blocking(PreTradeSafetyChecker):
        def check(self, now: datetime) -> SafetyVerdict:  # type: ignore[override]
            return SafetyVerdict(
                action=SafetyAction.BLOCK,
                reasons=[SafetyReason.FEED_OFFLINE],
                details={"feed": "stub"},
                checked_at=now,
            )

    return _Blocking(
        get_account=lambda: make_account(),
        get_spread_points=lambda: 25.0,
        thresholds=SafetyThresholds(),
    )


# ----------------------------------------------------------------- 1. happy path


def test_send_market_order_succeeds() -> None:
    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    env = om.send(
        make_order_request(client_order_id="c-1"),
        setup_id=uuid4(),
        now=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )
    assert env.state == "filled"
    assert env.filled_volume == Decimal("0.10")
    assert env.avg_fill_price == Decimal("2375.00")
    assert env.error_code is None
    assert connector.sent[0].client_order_id == "c-1"
    assert env.setup_id is not None


# ----------------------------------------------------------------- 2. idempotency


def test_idempotent_send_returns_existing() -> None:
    """A second send with the same client_order_id returns the original envelope."""

    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    setup_id = uuid4()
    a = om.send(
        make_order_request(client_order_id="dup"),
        setup_id=setup_id,
    )
    b = om.send(
        make_order_request(client_order_id="dup"),
        setup_id=setup_id,
    )
    assert a is b  # same envelope object
    # The connector saw only ONE order.
    assert len(connector.sent) == 1
    assert om.get_envelope("dup") is a


# ----------------------------------------------------------------- 3. safety block


def test_safety_block_rejects_order() -> None:
    """A BLOCK verdict from PreTradeSafetyChecker vetoes the order before connector call."""

    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_blocking())
    env = om.send(make_order_request(), setup_id=uuid4())
    assert env.state == "rejected"
    assert env.error_code == "SAFETY_BLOCK"
    assert "feed_offline" in (env.error_message or "")
    # The connector was never called.
    assert connector.sent == []


# ----------------------------------------------------------------- 4. broker reject


def test_broker_rejection_captured() -> None:
    connector = _StubConnector()
    connector.fail_next = True
    om = OrderManager(connector=connector, safety=_safety_allowing())
    env = om.send(make_order_request(client_order_id="rej"), setup_id=uuid4())
    assert env.state == "rejected"
    assert env.error_code == "BROKER_REJECT"
    assert "simulated rejection" in (env.error_message or "")


# ----------------------------------------------------------------- 5. connector exception


def test_connector_exception_marks_envelope_rejected() -> None:
    class _BoomConnector(_StubConnector):
        def order_send(self, request: OrderRequest) -> OrderResult:  # type: ignore[override]
            raise ConnectionError("bridge down")

    om = OrderManager(connector=_BoomConnector(), safety=_safety_allowing())
    env = om.send(make_order_request(client_order_id="boom"), setup_id=uuid4())
    assert env.state == "rejected"
    assert env.error_code == "CONNECTOR_EXCEPTION"
    assert "bridge down" in (env.error_message or "")


# ----------------------------------------------------------------- 6. slippage recording


def test_slippage_recorded_for_market_with_requested_price() -> None:
    """When a requested price is provided, slippage = (fill - request) / point."""

    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    req = make_order_request(client_order_id="slip", price=Decimal("2374.50"))
    env = om.send(req, setup_id=uuid4())
    # fill at 2375.00, requested 2374.50 → diff 0.50, point 0.01 → 50 points.
    assert env.slippage_points == Decimal("50.00")


# ----------------------------------------------------------------- 7. tag recording


def test_engine_source_tag_recorded() -> None:
    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    env = om.send(
        make_order_request(),
        setup_id=uuid4(),
        engine_source=OrderTag.AI_ASSISTED,
    )
    assert env.engine_source == OrderTag.AI_ASSISTED


# ----------------------------------------------------------------- 8. helpers


def test_market_buy_shorthand() -> None:
    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    env = om.market_buy(
        symbol="XAUUSD",
        volume=Decimal("0.05"),
        sl=Decimal("2370.00"),
        tp=Decimal("2380.00"),
        setup_id=uuid4(),
    )
    assert env.state == "filled"
    assert env.side == OrderSide.BUY
    assert env.sl == Decimal("2370.00")
    assert env.tp == Decimal("2380.00")


def test_market_sell_shorthand() -> None:
    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    env = om.market_sell(
        symbol="XAUUSD",
        volume=Decimal("0.05"),
        setup_id=uuid4(),
    )
    assert env.side == OrderSide.SELL


# ----------------------------------------------------------------- 9. envelope list snapshot


def test_envelopes_property_returns_snapshot() -> None:
    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    om.send(make_order_request(client_order_id="a"), setup_id=uuid4())
    om.send(make_order_request(client_order_id="b"), setup_id=uuid4())
    snap = om.envelopes
    assert len(snap) == 2
    assert {e.client_order_id for e in snap} == {"a", "b"}


# ----------------------------------------------------------------- 10. connector is IMarketConnector


def test_order_manager_uses_protocol_type() -> None:
    """The constructor signature requires an IMarketConnector (duck-typed)."""

    connector = _StubConnector()
    om = OrderManager(connector=connector, safety=_safety_allowing())
    assert isinstance(connector, IMarketConnector)
    # The internal reference is the same object.
    assert om._connector is connector  # noqa: SLF001
