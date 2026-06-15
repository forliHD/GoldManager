"""Tests for PendingOrderManager — Block 4 Phase 2 (cancel obsolete pendings)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    FVGOutput,
    LiquidityEngineOutput,
    MarketStructureOutput,
    NewsContextOutput,
    SessionEngineOutput,
    SessionName,
    StructureEvent,
    StructureEventType,
    SwingPoint,
    TripleVWAPOutput,
    VWAPLevel,
    VWAPLevelOutput,
    VolumeProfileName,
    VolumeProfileOutput,
    VolumeProfileState,
    VolumeRangeOutput,
    ValueAreaStatus,
)
from xauusd_bot.connectors.schemas import (
    OrderRequest,
    OrderSide,
    OrderType,
)
from xauusd_bot.execution.pending import PendingOrderManager

from tests._execution_factories import make_symbol_spec


# ----------------------------------------------------------------- stub connector


class _StubConnector:
    def __init__(self, pending: list[OrderRequest] | None = None) -> None:
        self.symbol = "XAUUSD"
        self.spec = make_symbol_spec()
        self._pending: list[OrderRequest] = list(pending or [])

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:  # noqa: ARG002
        return list(self._pending)

    def order_cancel(self, order_id: str) -> Any:
        # mutate: drop the cancelled order.
        self._pending = [p for p in self._pending if p.client_order_id != order_id]
        from xauusd_bot.connectors.schemas import OrderResult

        return OrderResult(accepted=True, order_id=order_id)

    # Methods not used in these tests — satisfy the Protocol shape.
    def get_rates(self, *args: Any, **kwargs: Any) -> list:  # noqa: ARG002
        return []

    def get_ticks(self, *args: Any, **kwargs: Any) -> list:  # noqa: ARG002
        return []

    def get_account(self) -> Any:  # noqa: ARG002
        from datetime import UTC, datetime
        from decimal import Decimal

        from xauusd_bot.connectors.schemas import AccountInfo

        return AccountInfo(
            login="t",
            broker="t",
            balance=Decimal("10000"),
            equity=Decimal("10000"),
            margin=Decimal("0"),
            free_margin=Decimal("10000"),
            server_time=datetime.now(tz=UTC),
        )

    def get_symbol_spec(self, symbol: str) -> Any:  # noqa: ARG002
        return self.spec

    def order_send(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    def positions_get(self, *args: Any, **kwargs: Any) -> list:  # noqa: ARG002
        return []

    def order_modify(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        return None

    def is_connected(self) -> bool:  # noqa: ARG002
        return True

    def shutdown(self) -> None:  # noqa: ARG002
        return None


# ----------------------------------------------------------------- bundle helpers


def _empty_bundle(ts: datetime) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(ts=ts)


def _bundle_with_news_blackout(ts: datetime) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=ts,
        news=NewsContextOutput(
            minutes_until_next_high_impact=5.0,
            in_blackout_flag=True,
            next_high_impact=None,
            upcoming_events=[],
        ),
    )


def _bundle_with_structure_event(
    ts: datetime, event_type: StructureEventType
) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=ts,
        structure=MarketStructureOutput(
            swings=[],
            last_bos=StructureEvent(
                type=event_type,
                level=2375.0,
                time=ts,
                bar_index=10,
                close=2376.0,
                distance_atr=0.5,
            ),
            last_choch=None,
            liquidity_pools=[],
            trend=("up" if event_type in (StructureEventType.BOS_UP, StructureEventType.CHOCH_UP) else "down"),
            fractal_n=3,
        ),
    )


def _bundle_with_vwap_cluster_far(ts: datetime, current_price: float, atr: float) -> FeatureSnapshotBundle:
    """VWAP cluster is far from the current price."""

    center = current_price + 4 * atr  # 4 ATR away
    return FeatureSnapshotBundle(
        ts=ts,
        atr=atr,
        vwap=TripleVWAPOutput(
            levels={
                "utc00": VWAPLevelOutput(
                    level=VWAPLevel.UTC00, value=center, distance_points=0,
                    distance_atr=0, n_bars_anchored=100,
                ),
            },
            cluster_within_atr=1.5,
            is_cluster=True,
            cluster_center=center,
        ),
    )


# ----------------------------------------------------------------- 1. happy path: no pendings


def test_sweep_with_no_pendings() -> None:
    connector = _StubConnector()
    mgr = PendingOrderManager(connector=connector)
    result = mgr.sweep(
        _empty_bundle(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.examined == 0
    assert result.kept == 0
    assert result.cancelled == 0
    assert result.cancel_reasons == {}


# ----------------------------------------------------------------- 2. news blackout cancels


def test_sweep_cancels_during_news_blackout() -> None:
    pending = [
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2370.00"),
            client_order_id="p-1",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    mgr.register(pending[0])
    result = mgr.sweep(
        _bundle_with_news_blackout(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.examined == 1
    assert result.cancelled == 1
    assert result.cancel_reasons.get("news_blackout") == 1
    # The order is gone from the connector's list.
    assert connector.pending_get() == []
    # The manager's aging ledger has dropped it.
    assert "p-1" not in mgr._registered  # noqa: SLF001


# ----------------------------------------------------------------- 3. structure break cancels


def test_sweep_cancels_when_structure_breaks_against() -> None:
    """A long limit with a fresh BOS_down is a structure break — cancel."""

    pending = [
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2370.00"),
            client_order_id="p-long",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    mgr.register(pending[0])
    result = mgr.sweep(
        _bundle_with_structure_event(
            datetime(2026, 4, 15, 13, 30, tzinfo=UTC), StructureEventType.BOS_DOWN
        ),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.cancelled == 1
    assert result.cancel_reasons.get("structure_against") == 1


def test_sweep_keeps_when_structure_aligns() -> None:
    pending = [
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2370.00"),
            client_order_id="p-long-ok",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    mgr.register(pending[0])
    result = mgr.sweep(
        _bundle_with_structure_event(
            datetime(2026, 4, 15, 13, 30, tzinfo=UTC), StructureEventType.BOS_UP
        ),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.cancelled == 0
    assert result.kept == 1


# ----------------------------------------------------------------- 4. far-from-cluster


def test_sweep_cancels_when_far_from_vwap_cluster() -> None:
    pending = [
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2370.00"),
            client_order_id="p-far",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector, cluster_break_atr=3.0)
    mgr.register(pending[0])
    bundle = _bundle_with_vwap_cluster_far(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), current_price=2375.0, atr=1.0
    )
    result = mgr.sweep(bundle, current_price=2375.0, bar_index=100)
    assert result.cancelled == 1
    assert result.cancel_reasons.get("vwap_cluster_break") == 1


# ----------------------------------------------------------------- 5. age limit


def test_sweep_cancels_stale_pendings() -> None:
    pending = [
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2370.00"),
            client_order_id="p-old",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector, max_age_bars=10)
    # Register a bar_index 5 bars ago; current sweep is at bar 100.
    mgr._registered["p-old"] = 90  # noqa: SLF001
    result = mgr.sweep(
        _empty_bundle(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    # 100 - 90 = 10, NOT > 10, so still kept.
    assert result.cancelled == 0
    # Move further: register even earlier.
    mgr._registered["p-old"] = 50  # noqa: SLF001
    result = mgr.sweep(
        _empty_bundle(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    # 100 - 50 = 50 > 10 → cancelled.
    assert result.cancelled == 1
    assert result.cancel_reasons.get("max_age") == 1


# ----------------------------------------------------------------- 6. multiple cancel reasons aggregated


def test_sweep_aggregates_reasons_across_orders() -> None:
    pending = [
        OrderRequest(
            symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.LIMIT,
            volume=Decimal("0.10"), price=Decimal("2370.00"), client_order_id="p-n1",
        ),
        OrderRequest(
            symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.LIMIT,
            volume=Decimal("0.10"), price=Decimal("2370.00"), client_order_id="p-n2",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    for p in pending:
        mgr.register(p)
    result = mgr.sweep(
        _bundle_with_news_blackout(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.cancelled == 2
    assert result.cancel_reasons.get("news_blackout") == 2


# ----------------------------------------------------------------- 7. cancel failure leaves the order alone


def test_sweep_keeps_when_connector_cancel_fails() -> None:
    """If the connector's order_cancel returns accepted=False, the order stays."""

    class _FailCancel(_StubConnector):
        def order_cancel(self, order_id: str) -> Any:  # type: ignore[override]
            from xauusd_bot.connectors.schemas import OrderResult

            return OrderResult(accepted=False, order_id=order_id, error_code="X")

    pending = [
        OrderRequest(
            symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.LIMIT,
            volume=Decimal("0.10"), price=Decimal("2370.00"), client_order_id="p-fail",
        ),
    ]
    connector = _FailCancel(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    mgr.register(pending[0])
    result = mgr.sweep(
        _bundle_with_news_blackout(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.kept == 1
    assert result.cancelled == 0


# ----------------------------------------------------------------- 8. forget()


def test_forget_removes_from_aging_ledger() -> None:
    connector = _StubConnector()
    mgr = PendingOrderManager(connector=connector)
    mgr.register(
        OrderRequest(
            symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.LIMIT,
            volume=Decimal("0.10"), price=Decimal("2370.00"), client_order_id="x",
        ),
        bar_index=42,
    )
    assert "x" in mgr._registered  # noqa: SLF001
    assert mgr._registered["x"] == 42  # noqa: SLF001
    mgr.forget("x")
    assert "x" not in mgr._registered  # noqa: SLF001


# ----------------------------------------------------------------- 9. market order never cancelled


def test_market_order_in_pending_get_is_kept() -> None:
    """If a MARKET order ever shows up in pending_get, the manager keeps it."""

    pending = [
        OrderRequest(
            symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.MARKET,
            volume=Decimal("0.10"), price=None, client_order_id="mkt",
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    result = mgr.sweep(
        _bundle_with_news_blackout(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    assert result.kept == 1
    assert result.cancelled == 0


# ----------------------------------------------------------------- 10. no client_order_id


def test_pending_without_client_order_id_is_kept() -> None:
    pending = [
        OrderRequest(
            symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.LIMIT,
            volume=Decimal("0.10"), price=Decimal("2370.00"), client_order_id=None,
        ),
    ]
    connector = _StubConnector(pending=pending)
    mgr = PendingOrderManager(connector=connector)
    result = mgr.sweep(
        _bundle_with_news_blackout(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
        current_price=2375.0,
        bar_index=100,
    )
    # No cid → we can't cancel it via the connector; keep.
    assert result.kept == 1
