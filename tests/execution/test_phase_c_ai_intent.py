"""Phase C — the AI's SL/TP intent reaches the executor within Phase-A floors."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    MarketStructureOutput,
    SwingPoint,
)
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.execution.stops import StopManager, parse_sl_from_invalidations
from xauusd_bot.execution.take_profit import TakeProfitManager

from tests._execution_factories import make_symbol_spec

_TS = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


def _bundle_low_swing(swing_low: float) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=_TS,
        atr=0.5,
        structure=MarketStructureOutput(
            swings=[SwingPoint(kind="low", price=swing_low, time=_TS, bar_index=5, is_external=True)],
            last_bos=None, last_choch=None, liquidity_pools=[], trend="up", fractal_n=3,
        ),
    )


# ---------------------------------------------------------------- invalidation parser


def test_parse_sl_extracts_price_below_entry_for_long():
    inv = ["H1-Close unter 4179.4", "vwap_loss"]
    assert parse_sl_from_invalidations(inv, OrderSide.BUY, entry_price=4189.0) == 4179.4


def test_parse_sl_extracts_price_above_entry_for_short():
    inv = ["H1-Close über 4205.5"]
    assert parse_sl_from_invalidations(inv, OrderSide.SELL, entry_price=4190.0) == 4205.5


def test_parse_sl_ignores_fib_ratios_and_wrong_side():
    # 0.382 is a fib ratio (< price floor) and 4200 is ABOVE a long entry → ignored.
    inv = ["close below 0.382 retrace", "invalid above 4200.0"]
    assert parse_sl_from_invalidations(inv, OrderSide.BUY, entry_price=4189.0) is None


def test_parse_sl_picks_nearest_level_to_entry():
    inv = ["dead under 4150.0", "or under 4180.0"]  # both below a long entry
    # Nearest-to-entry (highest below) is the operative invalidation.
    assert parse_sl_from_invalidations(inv, OrderSide.BUY, entry_price=4189.0) == 4180.0


# ---------------------------------------------------------------- AI SL hint → compute_initial


def test_sl_hint_overrides_structure_swing():
    mgr = StopManager(spec=make_symbol_spec(), initial_sl_atr=0.5)
    bundle = _bundle_low_swing(swing_low=4185.0)  # structure would give 4184.75
    res = mgr.compute_initial(
        OrderSide.BUY, Decimal("4189.00"), bundle, now=_TS, sl_hint=Decimal("4179.4")
    )
    # AI level 4179.4 − 0.5×0.5 buffer = 4179.15 (well beyond the floor) → used.
    assert res.sl_price == Decimal("4179.15")
    assert any("AI invalidation" in r for r in res.reasoning)


def test_sl_hint_wrong_side_falls_back_to_structure():
    mgr = StopManager(spec=make_symbol_spec(), initial_sl_atr=0.5)
    bundle = _bundle_low_swing(swing_low=4185.0)
    # Hint ABOVE a long entry is nonsensical → ignored, structure swing used.
    res = mgr.compute_initial(
        OrderSide.BUY, Decimal("4189.00"), bundle, now=_TS, sl_hint=Decimal("4200.0")
    )
    assert res.sl_price == Decimal("4184.75")  # 4185.0 − 0.25
    assert any("swing low" in r for r in res.reasoning)


def test_sl_hint_too_tight_is_floored():
    mgr = StopManager(spec=make_symbol_spec(), initial_sl_atr=0.5, min_sl_points=3.0, min_sl_atr=0.6)
    bundle = _bundle_low_swing(swing_low=4185.0)
    # Hint 0.1 below entry → would be a microscopic stop → floor pushes to entry−3.
    res = mgr.compute_initial(
        OrderSide.BUY, Decimal("4189.00"), bundle, now=_TS, sl_hint=Decimal("4188.90")
    )
    assert res.sl_price == Decimal("4186.00")  # entry − max(0.6×0.5, 3.0)


# ---------------------------------------------------------------- AI TP R-targets


def test_tp_rr_targets_place_r_multiples():
    mgr = TakeProfitManager(spec=make_symbol_spec())
    bundle = FeatureSnapshotBundle(ts=_TS, atr=0.5)
    entry = Decimal("4189.00")
    sl = Decimal("4185.00")  # 1R = 4.0
    plan = mgr.compute(OrderSide.BUY, entry, sl, bundle, now=_TS, tp1_rr=1.5, tp2_rr=3.0)
    assert plan.tp1_price == Decimal("4195.00")  # entry + 1.5×4
    assert plan.tp2_price == Decimal("4201.00")  # entry + 3.0×4
    assert any("ai_1.5R" in r for r in plan.reasoning)


def test_tp_rr_out_of_range_ignored():
    mgr = TakeProfitManager(spec=make_symbol_spec())
    bundle = FeatureSnapshotBundle(ts=_TS, atr=0.5)
    entry = Decimal("4189.00")
    sl = Decimal("4185.00")
    # 500R is absurd → ignored, deterministic 1R fallback (no liquidity → 1R).
    plan = mgr.compute(OrderSide.BUY, entry, sl, bundle, now=_TS, tp1_rr=500.0)
    assert plan.tp1_price == Decimal("4193.00")  # 1R fallback, not 500R
