"""Unit tests for the PositionManager per-bar management planner.

The planner is pure: we inject fake Stop/TakeProfit managers so we control the
trail / runner outputs and assert the PositionManager's own logic (TP partials,
break-even, ratcheting, runner close, idempotency).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from xauusd_bot.connectors.schemas import OrderSide, SymbolSpec
from xauusd_bot.execution.position_manager import (
    ManagedPosition,
    PositionManager,
)


def _spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD+",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )


class _FakeStop:
    def __init__(self, trail_sl: Decimal | None = None) -> None:
        self._trail_sl = trail_sl

    def trail(self, side, current_sl, entry_price, bundle, *, now=None):
        return SimpleNamespace(sl_price=self._trail_sl)


class _FakeTP:
    def __init__(self, close: bool = False, reason: str = "rej") -> None:
        self._close = close
        self._reason = reason

    def should_close_runner(self, side, tp3_price, current_close, bundle):
        return (self._close, self._reason)


def _mgr(trail_sl=None, runner_close=False):
    return PositionManager(_FakeStop(trail_sl), _FakeTP(runner_close), _spec())


def _pos(**kw):
    base = dict(
        ticket="111",
        side=OrderSide.BUY,
        entry_price=Decimal("4250.00"),
        initial_volume=Decimal("0.10"),
        sl_price=Decimal("4240.00"),
        tp1_price=Decimal("4255.00"),
        tp2_price=Decimal("4260.00"),
        tp3_price=Decimal("4275.00"),
    )
    base.update(kw)
    return ManagedPosition(**base)


_BUNDLE = object()  # the fake managers ignore the bundle


def test_tp1_hit_partial_close_and_breakeven():
    actions, mp = _mgr().plan(_pos(), _BUNDLE, current_price=Decimal("4255.50"))
    kinds = [(a.kind, a.reason) for a in actions]
    assert ("partial_close", "tp1_hit") in kinds
    assert ("modify_sl", "breakeven_after_tp1") in kinds
    pc = next(a for a in actions if a.kind == "partial_close")
    assert pc.volume == Decimal("0.03")  # 30% of 0.10
    be = next(a for a in actions if a.kind == "modify_sl")
    assert be.price == Decimal("4250.00")  # entry
    assert mp.tp1_taken and mp.breakeven_done and mp.sl_price == Decimal("4250.00")


def test_no_tp_hit_no_actions():
    actions, mp = _mgr().plan(_pos(), _BUNDLE, current_price=Decimal("4251.00"))
    assert actions == []
    assert not mp.tp1_taken


def test_tp1_idempotent_when_already_taken():
    actions, _ = _mgr().plan(
        _pos(tp1_taken=True, breakeven_done=True, sl_price=Decimal("4250.00")),
        _BUNDLE,
        current_price=Decimal("4256.00"),
    )
    assert all(a.reason != "tp1_hit" for a in actions)


def test_tp2_hit_partial_close():
    actions, mp = _mgr().plan(
        _pos(tp1_taken=True, breakeven_done=True, sl_price=Decimal("4250.00")),
        _BUNDLE,
        current_price=Decimal("4260.50"),
    )
    pc = [a for a in actions if a.kind == "partial_close" and a.reason == "tp2_hit"]
    assert pc and pc[0].volume == Decimal("0.03")
    assert mp.tp2_taken


def test_trailing_ratchets_only_in_favour():
    # Long: trail SL up to 4253 (tighter than 4250 breakeven) → applied.
    actions, mp = _mgr(trail_sl=Decimal("4253.00")).plan(
        _pos(tp1_taken=True, breakeven_done=True, sl_price=Decimal("4250.00")),
        _BUNDLE,
        current_price=Decimal("4258.00"),
    )
    trail = [a for a in actions if a.reason == "structure_trail"]
    assert trail and trail[0].price == Decimal("4253.00") and mp.sl_price == Decimal("4253.00")

    # Trail candidate BELOW current SL → no move (ratchet).
    actions2, _ = _mgr(trail_sl=Decimal("4248.00")).plan(
        _pos(tp1_taken=True, breakeven_done=True, sl_price=Decimal("4253.00")),
        _BUNDLE,
        current_price=Decimal("4258.00"),
    )
    assert all(a.reason != "structure_trail" for a in actions2)


def test_runner_close_on_rejection():
    actions, _ = _mgr(runner_close=True).plan(
        _pos(tp1_taken=True, tp2_taken=True, breakeven_done=True, sl_price=Decimal("4250")),
        _BUNDLE,
        current_price=Decimal("4274.00"),
    )
    assert any(a.kind == "close_all" for a in actions)


def test_short_side_tp1_mirror():
    pos = _pos(
        side=OrderSide.SELL,
        entry_price=Decimal("4250.00"),
        sl_price=Decimal("4260.00"),
        tp1_price=Decimal("4245.00"),
        tp2_price=Decimal("4240.00"),
        tp3_price=Decimal("4225.00"),
    )
    actions, mp = _mgr().plan(pos, _BUNDLE, current_price=Decimal("4244.50"))
    assert any(a.reason == "tp1_hit" for a in actions)
    assert mp.sl_price == Decimal("4250.00")  # breakeven = entry


def test_partial_below_volume_min_skips_close_but_marks_taken():
    # 30% of 0.02 = 0.006 → floors to 0.00 < volume_min → no partial, still taken.
    actions, mp = _mgr().plan(
        _pos(initial_volume=Decimal("0.02")),
        _BUNDLE,
        current_price=Decimal("4256.00"),
    )
    assert all(a.kind != "partial_close" for a in actions)
    assert mp.tp1_taken and mp.breakeven_done


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
