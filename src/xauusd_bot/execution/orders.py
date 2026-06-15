"""OrderManager — the deterministic "place orders" engine (Block 4 Phase 2).

The :class:`OrderManager` is the **only** place in Block 4 that calls
``connector.order_send``. Every order must:

1. Pass the :class:`PreTradeSafetyChecker` (Block 1) — the executor
   treats a ``BLOCK`` verdict as a hard veto.
2. Carry the setup_id (UUID from :class:`TradeQualification`) +
   strategy_version + engine_source (rule / ai).
3. Use a stable ``client_order_id`` (idempotency key) so a duplicate
   call does not place the order twice.
4. Be recorded in the in-memory ledger (``self._envelopes``) so the
   journal can replay it later.
5. Log requested-vs-fill prices for slippage analysis (Block 5).

Fill tracking
-------------
The manager subscribes to ``connector.order_send`` results (a
:class:`OrderResult`) and records:

* ``order_id`` (broker-assigned),
* ``filled_volume`` and ``avg_fill_price``,
* ``slippage_points = (fill_price - requested_price) / spec.point``.

A future Block-4 enhancement can poll ``connector.positions_get`` to
detect out-of-band fills; the current implementation trusts the
connector's synchronous ``OrderResult`` payload.

I-1
---
Imports only :class:`IMarketConnector`. No ``MetaTrader5``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.schemas.execution import (
    OrderEnvelope,
    OrderTag,
)
from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.safety import (
    PreTradeSafetyChecker,
    SafetyAction,
    SafetyVerdict,
)
from xauusd_bot.connectors.schemas import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
)

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------- protocol


class _FlattenFn(Protocol):
    """Callable protocol used by the OrderManager to close a position."""

    def __call__(self, position_id: str) -> OrderResult: ...


# ----------------------------------------------------------------- manager


class OrderManager:
    """Submit orders via :class:`IMarketConnector` after safety checks.

    Parameters
    ----------
    connector:
        The market connector (Replay or Live).
    safety:
        The :class:`PreTradeSafetyChecker` (Block 1). The manager
        blocks on every ``BLOCK`` verdict.
    strategy_version:
        Tag for the executor build (recorded in every envelope).
    """

    def __init__(
        self,
        connector: IMarketConnector,
        safety: PreTradeSafetyChecker,
        strategy_version: str = "block4-v1",
    ) -> None:
        self._connector = connector
        self._safety = safety
        self._strategy_version = strategy_version
        self._envelopes: dict[str, OrderEnvelope] = {}

    # --------------------------------------------------------------- public

    @property
    def envelopes(self) -> list[OrderEnvelope]:
        """Snapshot of all envelopes created in this process."""

        return list(self._envelopes.values())

    def get_envelope(self, client_order_id: str) -> OrderEnvelope | None:
        return self._envelopes.get(client_order_id)

    def send(
        self,
        request: OrderRequest,
        *,
        setup_id: UUID,
        engine_source: OrderTag = OrderTag.RULE_BASED,
        now: datetime | None = None,
    ) -> OrderEnvelope:
        """Submit an order after running :meth:`PreTradeSafetyChecker.check`.

        The request's ``client_order_id`` is used as the idempotency
        key. If the manager has already sent an envelope with the
        same key, it returns the existing envelope (no double order).

        Returns
        -------
        OrderEnvelope
            The envelope describing what was sent + the connector's
            :class:`OrderResult`. The envelope's ``state`` is
            ``submitted``, ``filled`` or ``rejected``.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        # Idempotency: if we already saw this client_order_id, return it.
        cid = request.client_order_id or f"om-{uuid4().hex}"
        existing = self._envelopes.get(cid)
        if existing is not None:
            log.info("order_idempotent_return", client_order_id=cid, state=existing.state)
            return existing

        envelope = OrderEnvelope(
            client_order_id=cid,
            setup_id=setup_id,
            strategy_version=self._strategy_version,
            engine_source=engine_source,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            requested_volume=Decimal(request.volume),
            requested_price=(Decimal(request.price) if request.price is not None else None),
            sl=request.sl,
            tp=request.tp,
            state="pending",
            created_at=ts,
            updated_at=ts,
        )
        self._envelopes[cid] = envelope

        # --- Pre-trade safety check. ---
        verdict: SafetyVerdict = self._safety.check(ts)
        if verdict.action == SafetyAction.BLOCK:
            envelope.state = "rejected"
            envelope.error_code = "SAFETY_BLOCK"
            envelope.error_message = ",".join(r.value for r in verdict.reasons) or "blocked"
            envelope.updated_at = ts
            log.warning(
                "order_blocked_by_safety",
                client_order_id=cid,
                reasons=[r.value for r in verdict.reasons],
            )
            return envelope

        # --- Forward to the connector. ---
        try:
            result: OrderResult = self._connector.order_send(request.model_copy(update={"client_order_id": cid}))
        except Exception as exc:  # noqa: BLE001
            envelope.state = "rejected"
            envelope.error_code = "CONNECTOR_EXCEPTION"
            envelope.error_message = str(exc)
            envelope.updated_at = ts
            log.error("order_send_exception", client_order_id=cid, error=str(exc))
            return envelope

        # --- Apply the connector's result. ---
        envelope.order_id = result.order_id
        envelope.filled_volume = result.filled_volume
        envelope.avg_fill_price = result.avg_fill_price
        envelope.error_code = result.error_code
        envelope.error_message = result.error_message
        envelope.state = "filled" if result.accepted and result.filled_volume > 0 else (
            "rejected" if not result.accepted else "submitted"
        )
        envelope.updated_at = ts
        if envelope.requested_price is not None and envelope.avg_fill_price is not None:
            spec = self._connector.get_symbol_spec(request.symbol)
            slip = (envelope.avg_fill_price - envelope.requested_price) / spec.point
            envelope.slippage_points = slip
        log.info(
            "order_submitted",
            client_order_id=cid,
            state=envelope.state,
            filled_volume=str(envelope.filled_volume),
            avg_fill_price=str(envelope.avg_fill_price),
        )
        return envelope

    # --------------------------------------------------------------- shortcuts

    def market_buy(
        self,
        symbol: str,
        volume: Decimal,
        *,
        sl: Decimal | None = None,
        tp: Decimal | None = None,
        setup_id: UUID,
        now: datetime | None = None,
    ) -> OrderEnvelope:
        return self.send(
            OrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                volume=volume,
                sl=sl,
                tp=tp,
            ),
            setup_id=setup_id,
            now=now,
        )

    def market_sell(
        self,
        symbol: str,
        volume: Decimal,
        *,
        sl: Decimal | None = None,
        tp: Decimal | None = None,
        setup_id: UUID,
        now: datetime | None = None,
    ) -> OrderEnvelope:
        return self.send(
            OrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                volume=volume,
                sl=sl,
                tp=tp,
            ),
            setup_id=setup_id,
            now=now,
        )


# ----------------------------------------------------------------- re-exports

__all__ = ["OrderManager"]
