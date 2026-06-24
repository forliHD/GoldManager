"""Tests for StopManager — Block 4 Phase 3 (SL construction)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.common.schemas.execution import StopsAndTPs, TrailingMode
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    MarketStructureOutput,
    StructureEvent,
    StructureEventType,
    SwingPoint,
)
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.execution.stops import StopManager

from tests._execution_factories import make_symbol_spec


# ----------------------------------------------------------------- helpers


def _empty_bundle(ts: datetime) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(ts=ts)


def _bundle_with_low_swing(ts: datetime, swing_low: float) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=ts,
        atr=0.5,
        structure=MarketStructureOutput(
            swings=[SwingPoint(kind="low", price=swing_low, time=ts, bar_index=5, is_external=True)],
            last_bos=None,
            last_choch=None,
            liquidity_pools=[],
            trend="up",
            fractal_n=3,
        ),
    )


def _bundle_with_high_swing(ts: datetime, swing_high: float) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=ts,
        atr=0.5,
        structure=MarketStructureOutput(
            swings=[SwingPoint(kind="high", price=swing_high, time=ts, bar_index=5, is_external=True)],
            last_bos=None,
            last_choch=None,
            liquidity_pools=[],
            trend="down",
            fractal_n=3,
        ),
    )


# ----------------------------------------------------------------- 1. long initial SL


def test_long_initial_sl_behind_swing_low_with_atr_buffer() -> None:
    """Long SL = swing_low - 1.0×ATR (rounded to digits)."""

    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, initial_sl_atr=1.0)
    bundle = _bundle_with_low_swing(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), swing_low=2370.0
    )
    result = mgr.compute_initial(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        bundle=bundle,
        now=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )
    assert result.sl_price == Decimal("2369.50")  # 2370.00 - 0.5
    assert result.trail_active is False
    assert result.trailing_mode == TrailingMode.FIXED
    assert any("swing low" in r for r in result.reasoning)


# ----------------------------------------------------------------- 2. short initial SL


def test_short_initial_sl_behind_swing_high_with_atr_buffer() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, initial_sl_atr=1.0)
    bundle = _bundle_with_high_swing(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), swing_high=2380.0
    )
    result = mgr.compute_initial(
        side=OrderSide.SELL,
        entry_price=Decimal("2375.00"),
        bundle=bundle,
        now=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )
    assert result.sl_price == Decimal("2380.50")  # 2380.00 + 0.5
    assert any("swing high" in r for r in result.reasoning)


# ----------------------------------------------------------------- 3. fallback when no swing


def test_long_initial_sl_fallback_when_no_structure() -> None:
    spec = make_symbol_spec()
    # ATR=5 so the 1×ATR fallback (5 pts) exceeds the SL floor (3 pts) and the
    # floor doesn't interfere — this test exercises the fallback distance.
    mgr = StopManager(spec=spec, initial_sl_atr=1.0)
    bundle = _empty_bundle(datetime(2026, 4, 15, 13, 30, tzinfo=UTC))
    bundle.atr = 5.0
    result = mgr.compute_initial(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        bundle=bundle,
    )
    # Fallback uses entry - 1×ATR = 2375 - 5 = 2370
    assert result.sl_price == Decimal("2370.00")
    assert any("fallback" in r for r in result.reasoning)


def test_sl_floor_pushes_out_a_too_tight_structure_stop() -> None:
    # Regression for the −6.7× sizing blowup (trade #7): when price enters just
    # above the swing low, the structure stop is microscopic. The floor pushes
    # it to at least max(min_sl_atr×ATR, min_sl_points) from entry.
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, initial_sl_atr=0.5, min_sl_atr=0.6, min_sl_points=3.0)
    # Swing low ≈ entry → structure SL lands a fraction of a point below entry
    # (the #7 case: entry 4191.36 just above a 4191.10 swing).
    bundle = _bundle_with_low_swing(datetime(2026, 4, 15, 13, 30, tzinfo=UTC), 4191.10)
    result = mgr.compute_initial(
        side=OrderSide.BUY, entry_price=Decimal("4191.36"), bundle=bundle
    )
    sl_distance = Decimal("4191.36") - result.sl_price
    # Floor = max(0.6×1.78, 3.0) = 3.0 → SL at least 3 pts away (not 0.26).
    assert sl_distance >= Decimal("3.0")
    assert any("floor" in r for r in result.reasoning)


# ----------------------------------------------------------------- 4. break-even


def test_break_even_long_moves_sl_above_entry() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, be_bonus_points=5.0)
    result = mgr.move_to_break_even(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        current_spread_points=30.0,
        now=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )
    # 30 points = 0.30, 5 points = 0.05. SL = 2375 + 0.30 + 0.05 = 2375.35
    assert result.sl_price == Decimal("2375.35")
    assert result.trail_active is True
    assert result.trailing_mode == TrailingMode.BREAK_EVEN


def test_break_even_short_moves_sl_below_entry() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, be_bonus_points=5.0)
    result = mgr.move_to_break_even(
        side=OrderSide.SELL,
        entry_price=Decimal("2375.00"),
        current_spread_points=30.0,
    )
    # SL = 2375 - 0.30 - 0.05 = 2374.65
    assert result.sl_price == Decimal("2374.65")


# ----------------------------------------------------------------- 5. trail ratchet (long)


def test_long_trail_ratchets_up_only() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, trail_buffer_atr=0.5)
    # Phase D: SL sits BEHIND (below) the swing low. Swing 2380, ATR 0.5, buffer
    # 0.5 → candidate = 2380 − 0.25 = 2379.75. Current SL 2370 → ratchets up.
    bundle = _bundle_with_low_swing(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), swing_low=2380.0
    )
    result = mgr.trail(
        side=OrderSide.BUY,
        current_sl=Decimal("2370.00"),
        entry_price=Decimal("2375.00"),
        bundle=bundle,
    )
    assert result.sl_price == Decimal("2379.75")
    assert result.sl_price < Decimal("2380.0")  # behind the swing, not above it
    assert result.trail_active is True
    assert result.trailing_mode == TrailingMode.STRUCTURE_TRAIL


def test_long_trail_does_not_lower_sl() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, trail_min_atr=1.0)
    # Swing low 2372 → candidate 2372.5. Current SL 2375 → 2372.5 is lower → keep.
    bundle = _bundle_with_low_swing(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), swing_low=2372.0
    )
    result = mgr.trail(
        side=OrderSide.BUY,
        current_sl=Decimal("2375.00"),
        entry_price=Decimal("2376.00"),
        bundle=bundle,
    )
    assert result.sl_price == Decimal("2375.00")  # unchanged


# ----------------------------------------------------------------- 6. trail ratchet (short)


def test_short_trail_ratchets_down_only() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec, trail_buffer_atr=0.5)
    # Phase D: SL sits BEHIND (above) the swing high. Swing 2370, ATR 0.5, buffer
    # 0.5 → candidate = 2370 + 0.25 = 2370.25. Current SL 2380 → ratchets down.
    bundle = _bundle_with_high_swing(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), swing_high=2370.0
    )
    result = mgr.trail(
        side=OrderSide.SELL,
        current_sl=Decimal("2380.00"),
        entry_price=Decimal("2375.00"),
        bundle=bundle,
    )
    assert result.sl_price == Decimal("2370.25")
    assert result.sl_price > Decimal("2370.0")  # behind the swing, not below it


# ----------------------------------------------------------------- 7. trail without data


def test_trail_without_swing_leaves_sl_unchanged() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec)
    result = mgr.trail(
        side=OrderSide.BUY,
        current_sl=Decimal("2370.00"),
        entry_price=Decimal("2375.00"),
        bundle=_empty_bundle(datetime(2026, 4, 15, 13, 30, tzinfo=UTC)),
    )
    assert result.sl_price == Decimal("2370.00")
    assert any("unchanged" in r for r in result.reasoning)


# ----------------------------------------------------------------- 8. result type


def test_compute_initial_returns_stopsandtps() -> None:
    spec = make_symbol_spec()
    mgr = StopManager(spec=spec)
    bundle = _bundle_with_low_swing(
        datetime(2026, 4, 15, 13, 30, tzinfo=UTC), swing_low=2370.0
    )
    result = mgr.compute_initial(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        bundle=bundle,
    )
    assert isinstance(result, StopsAndTPs)
    assert result.timestamp.tzinfo is not None


# ----------------------------------------------------------------- Phase D: BE floor + chandelier


_TS_PD = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


def test_trail_break_even_floor_locks_no_loss() -> None:
    # be_armed → the SL may not sit worse than entry + cost buffer, even though
    # the structure swing (2369.75) is far below entry. Chandelier off.
    mgr = StopManager(spec=make_symbol_spec(), trail_buffer_atr=0.5, chandelier_atr=0.0)
    bundle = _bundle_with_low_swing(_TS_PD, swing_low=2370.0)  # atr 0.5
    res = mgr.trail(OrderSide.BUY, Decimal("2369.00"), Decimal("2375.00"), bundle,
                    now=_TS_PD, peak=None, be_armed=True)
    assert res.sl_price == Decimal("2375.05")  # entry + 5×point — break-even floor


def test_trail_chandelier_ratchets_from_peak() -> None:
    # The chandelier (peak − 3×ATR) ratchets the SL up continuously, dominating
    # the far structure swing.
    mgr = StopManager(spec=make_symbol_spec(), trail_buffer_atr=0.5, chandelier_atr=3.0)
    bundle = _bundle_with_low_swing(_TS_PD, swing_low=2370.0)  # atr 0.5
    res = mgr.trail(OrderSide.BUY, Decimal("2369.00"), Decimal("2375.00"), bundle,
                    now=_TS_PD, peak=Decimal("2400.00"), be_armed=False)
    assert res.sl_price == Decimal("2398.50")  # 2400 − 3×0.5


def test_trail_short_be_floor_and_chandelier() -> None:
    mgr = StopManager(spec=make_symbol_spec(), trail_buffer_atr=0.5, chandelier_atr=3.0)
    bundle = _bundle_with_high_swing(_TS_PD, swing_high=2380.0)  # atr 0.5
    # Short entry 2375, peak (low) 2350 → chandelier = 2350 + 1.5 = 2351.5; BE = 2374.95.
    res = mgr.trail(OrderSide.SELL, Decimal("2381.00"), Decimal("2375.00"), bundle,
                    now=_TS_PD, peak=Decimal("2350.00"), be_armed=True)
    assert res.sl_price == Decimal("2351.50")  # tightest protective (min) for a short
