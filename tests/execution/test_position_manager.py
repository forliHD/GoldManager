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

    def trail(self, side, current_sl, entry_price, bundle, *, now=None, peak=None, be_armed=False, spread_points=0.0):
        self.last_spread_points = spread_points  # captured so a test can assert it's forwarded
        return SimpleNamespace(sl_price=self._trail_sl)


class _FakeTP:
    def __init__(self, close: bool = False, reason: str = "rej") -> None:
        self._close = close
        self._reason = reason

    def should_close_runner(self, side, tp3_price, current_close, bundle):
        return (self._close, self._reason)


def _mgr(trail_sl=None, runner_close=False, breakeven_at_tp1=False):
    return PositionManager(
        _FakeStop(trail_sl), _FakeTP(runner_close), _spec(), breakeven_at_tp1=breakeven_at_tp1
    )


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


def test_tp1_partial_close_arms_trailing_no_breakeven():
    # New default (lever #1): TP1 takes the partial and ARMS trailing, but does
    # NOT snap the SL to entry — the runner keeps room to reach TP3.
    actions, mp = _mgr().plan(_pos(), _BUNDLE, current_price=Decimal("4255.50"))
    kinds = [(a.kind, a.reason) for a in actions]
    assert ("partial_close", "tp1_hit") in kinds
    assert all(a.reason != "breakeven_after_tp1" for a in actions)  # no dead break-even
    pc = next(a for a in actions if a.kind == "partial_close")
    assert pc.volume == Decimal("0.03")  # 30% of 0.10
    assert mp.tp1_taken and mp.breakeven_done  # trailing armed
    assert mp.sl_price == Decimal("4240.00")  # initial SL unchanged (not entry)


def test_tp1_breakeven_when_flag_enabled():
    # Opt-in restores the old behaviour: SL snaps to entry at TP1.
    actions, mp = _mgr(breakeven_at_tp1=True).plan(_pos(), _BUNDLE, current_price=Decimal("4255.50"))
    assert ("modify_sl", "breakeven_after_tp1") in [(a.kind, a.reason) for a in actions]
    assert mp.sl_price == Decimal("4250.00")  # entry


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
    trail = [a for a in actions if a.reason == "trail"]
    assert trail and trail[0].price == Decimal("4253.00") and mp.sl_price == Decimal("4253.00")

    # Trail candidate BELOW current SL → no move (ratchet).
    actions2, _ = _mgr(trail_sl=Decimal("4248.00")).plan(
        _pos(tp1_taken=True, breakeven_done=True, sl_price=Decimal("4253.00")),
        _BUNDLE,
        current_price=Decimal("4258.00"),
    )
    assert all(a.reason != "trail" for a in actions2)


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
    assert mp.sl_price == Decimal("4260.00")  # initial SL unchanged (no break-even snap)


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


# ---------------------------------------------------------------- Phase D: trail activation


def _mgr_activate(trail_sl, trail_activate_r):
    return PositionManager(
        _FakeStop(trail_sl), _FakeTP(False), _spec(), trail_activate_r=trail_activate_r
    )


def test_trail_does_not_arm_below_activation_r():
    # TP1 far away (not hit) so only the profit-R gate can arm trailing.
    # entry 4250, initial_risk 10 → 1R at price 4260. At 4255 (0.5R) → no trail.
    mgr = _mgr_activate(trail_sl=Decimal("4248.00"), trail_activate_r=1.0)
    pos = _pos(tp1_price=Decimal("4300.00"), initial_risk=Decimal("10.0"))
    actions, mp = mgr.plan(pos, _BUNDLE, current_price=Decimal("4255.00"))
    assert all(a.kind != "modify_sl" for a in actions)
    assert mp.sl_price == Decimal("4240.00")  # unchanged


def test_trail_arms_at_activation_r():
    # At 4262 (1.2R ≥ 1R) the trail arms and ratchets the SL up to 4248.
    mgr = _mgr_activate(trail_sl=Decimal("4248.00"), trail_activate_r=1.0)
    pos = _pos(tp1_price=Decimal("4300.00"), initial_risk=Decimal("10.0"))
    actions, mp = mgr.plan(pos, _BUNDLE, current_price=Decimal("4262.00"))
    assert ("modify_sl", "trail") in [(a.kind, a.reason) for a in actions]
    assert mp.sl_price == Decimal("4248.00")


# ---------------------------------------------------------------- weekend flat


def _bundle_at(ts_iso: str):
    from datetime import datetime
    return SimpleNamespace(ts=datetime.fromisoformat(ts_iso), broker_offset_minutes=0.0)


def test_weekend_flat_closes_whole_position():
    mgr = PositionManager(_FakeStop(), _FakeTP(), _spec(), weekend_flat=lambda ts, off: True)
    # Even sitting at TP1, the weekend flat pre-empts everything → just close_all.
    actions, _ = mgr.plan(_pos(), _bundle_at("2026-01-02T21:00:00+00:00"), Decimal("4255.50"))
    assert [(a.kind, a.reason) for a in actions] == [("close_all", "weekend_flat")]


def test_no_weekend_flat_runs_normal_management():
    mgr = PositionManager(_FakeStop(), _FakeTP(), _spec(), weekend_flat=lambda ts, off: False)
    actions, _ = mgr.plan(_pos(), _bundle_at("2026-01-01T12:00:00+00:00"), Decimal("4255.50"))
    reasons = [a.reason for a in actions]
    assert "tp1_hit" in reasons and "weekend_flat" not in reasons
