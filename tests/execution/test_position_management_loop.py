"""Integration test for the execution-engine position-management loop.

Drives ``_manage_positions`` against a fakeredis + a recording fake connector:
the loop must load the stored plan, apply the TP1 partial-close + break-even,
persist the updated state, and drop the plan once the position is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.schemas import OrderResult, OrderSide, SymbolSpec
from xauusd_bot.execution.position_manager import ManagedPosition, PositionManager
from xauusd_bot.execution.stops import StopManager
from xauusd_bot.execution.take_profit import TakeProfitManager
from xauusd_bot.execution_engine import (
    _load_managed_all,
    _manage_positions,
    _store_managed,
)

pytestmark = pytest.mark.asyncio


def _spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD+", point=Decimal("0.01"), digits=2,
        trade_contract_size=Decimal("100"), volume_min=Decimal("0.01"),
        volume_max=Decimal("100"), volume_step=Decimal("0.01"),
    )


class _FakeConnector:
    def __init__(self, positions):
        self._positions = positions
        self.calls: list[tuple] = []

    def positions_get(self, symbol=None):
        return self._positions

    def order_modify(self, ticket, *, price=None, sl=None, tp=None):
        self.calls.append(("modify_sl", str(ticket), sl))
        return OrderResult(accepted=True, order_id=str(ticket))

    def close_position(self, ticket, volume=None):
        self.calls.append(("close", str(ticket), float(volume) if volume is not None else None))
        return OrderResult(accepted=True, order_id=str(ticket))


def _pos_mgr():
    spec = _spec()
    return PositionManager(StopManager(spec=spec), TakeProfitManager(spec=spec), spec)


def _plan():
    return ManagedPosition(
        ticket="111", side=OrderSide.BUY, entry_price=Decimal("4250.00"),
        initial_volume=Decimal("0.10"), sl_price=Decimal("4240.00"),
        tp1_price=Decimal("4255.00"), tp2_price=Decimal("4260.00"), tp3_price=Decimal("4275.00"),
    )


async def test_tp1_partial_close_and_breakeven_applied_and_persisted():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    conn = _FakeConnector([SimpleNamespace(position_id="111")])
    pipeline = SimpleNamespace(connector=conn)
    settings = SimpleNamespace(symbol="XAUUSD+")
    await _store_managed(r, _plan())

    bundle = FeatureSnapshotBundle(ts=datetime.now(tz=UTC))  # all engines None → trail/runner no-op
    await _manage_positions(pipeline, _pos_mgr(), r, settings, bundle, current_price=4256.0)

    # A 30% partial close (0.03) and a break-even SL move to entry (4250) fired.
    assert ("close", "111", 0.03) in conn.calls
    assert ("modify_sl", "111", 4250.0) in conn.calls
    # Updated plan was persisted.
    stored = await _load_managed_all(r)
    assert stored["111"].tp1_taken is True and stored["111"].breakeven_done is True


async def test_plan_dropped_when_position_closed():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    conn = _FakeConnector([])  # no open positions
    pipeline = SimpleNamespace(connector=conn)
    settings = SimpleNamespace(symbol="XAUUSD+")
    await _store_managed(r, _plan())

    bundle = FeatureSnapshotBundle(ts=datetime.now(tz=UTC))
    await _manage_positions(pipeline, _pos_mgr(), r, settings, bundle, current_price=4256.0)

    assert await _load_managed_all(r) == {}  # plan cleaned up
    assert conn.calls == []  # no actions on a gone position


async def test_no_actions_when_price_below_tp1():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    conn = _FakeConnector([SimpleNamespace(position_id="111")])
    pipeline = SimpleNamespace(connector=conn)
    settings = SimpleNamespace(symbol="XAUUSD+")
    await _store_managed(r, _plan())

    bundle = FeatureSnapshotBundle(ts=datetime.now(tz=UTC))
    await _manage_positions(pipeline, _pos_mgr(), r, settings, bundle, current_price=4251.0)

    assert conn.calls == []
    assert (await _load_managed_all(r))["111"].tp1_taken is False
