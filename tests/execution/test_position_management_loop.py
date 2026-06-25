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


async def test_tp1_partial_close_then_break_even_floor():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    conn = _FakeConnector([SimpleNamespace(position_id="111")])
    pipeline = SimpleNamespace(connector=conn, on_position_closed=lambda t: None, note_bar=lambda p, ts: None)
    settings = SimpleNamespace(symbol="XAUUSD+")
    await _store_managed(r, _plan())

    bundle = FeatureSnapshotBundle(ts=datetime.now(tz=UTC))  # no structure/ATR → only the BE floor applies
    await _manage_positions(pipeline, _pos_mgr(), r, settings, bundle, current_price=4256.0)

    # Phase D fix: a 30% partial (0.03) fires AND, now that the trade is proven
    # (TP1 taken arms the break-even floor), the SL ratchets to entry + cost
    # buffer — a +1R touch can no longer become a loss. Structure/chandelier are
    # no-ops here (empty bundle), so the move is purely the BE floor.
    assert ("close", "111", 0.03) in conn.calls
    be_moves = [c for c in conn.calls if c[0] == "modify_sl"]
    assert be_moves and abs(float(be_moves[0][2]) - 4250.05) < 0.01  # entry 4250 + 5×0.01
    stored = await _load_managed_all(r)
    assert stored["111"].tp1_taken is True and stored["111"].breakeven_done is True
    assert stored["111"].sl_price >= stored["111"].entry_price  # never worse than entry now


async def test_plan_dropped_when_position_closed():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    conn = _FakeConnector([])  # no open positions
    pipeline = SimpleNamespace(connector=conn, on_position_closed=lambda t: None, note_bar=lambda p, ts: None)
    settings = SimpleNamespace(symbol="XAUUSD+")
    await _store_managed(r, _plan())

    bundle = FeatureSnapshotBundle(ts=datetime.now(tz=UTC))
    await _manage_positions(pipeline, _pos_mgr(), r, settings, bundle, current_price=4256.0)

    assert await _load_managed_all(r) == {}  # plan cleaned up
    assert conn.calls == []  # no actions on a gone position


async def test_no_actions_when_price_below_tp1():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    conn = _FakeConnector([SimpleNamespace(position_id="111")])
    pipeline = SimpleNamespace(connector=conn, on_position_closed=lambda t: None, note_bar=lambda p, ts: None)
    settings = SimpleNamespace(symbol="XAUUSD+")
    await _store_managed(r, _plan())

    bundle = FeatureSnapshotBundle(ts=datetime.now(tz=UTC))
    await _manage_positions(pipeline, _pos_mgr(), r, settings, bundle, current_price=4251.0)

    assert conn.calls == []
    assert (await _load_managed_all(r))["111"].tp1_taken is False
