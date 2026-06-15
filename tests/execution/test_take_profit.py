"""Tests for TakeProfitManager — Block 4 Phase 3 (multi-tier TP)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.common.schemas.execution import StopsAndTPs
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    FVGOutput,
    FVGStatus,
    FVGType,
    FVGZone,
    LiquidityEngineOutput,
    LiquidityZone,
    MarketStructureOutput,
    StructureEvent,
    StructureEventType,
    VolumeProfileName,
    VolumeProfileOutput,
    VolumeProfileState,
    VolumeRangeOutput,
    ValueAreaStatus,
)
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.execution.take_profit import TakeProfitManager

from tests._execution_factories import make_symbol_spec


# ----------------------------------------------------------------- bundle helpers


def _bundle_with_long_targets(
    above_center: float, atr: float = 0.5
) -> FeatureSnapshotBundle:
    zone = LiquidityZone(
        kind="high",
        price_low=above_center - 0.5,
        price_high=above_center + 0.5,
        center=above_center,
        pool_count=1,
    )
    return FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        atr=atr,
        liquidity=LiquidityEngineOutput(
            tp_targets_above=[zone],
            tp_targets_below=[],
            sl_protection_zones=[],
        ),
    )


def _bundle_with_short_targets(
    below_center: float, atr: float = 0.5
) -> FeatureSnapshotBundle:
    zone = LiquidityZone(
        kind="low",
        price_low=below_center - 0.5,
        price_high=below_center + 0.5,
        center=below_center,
        pool_count=1,
    )
    return FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        atr=atr,
        liquidity=LiquidityEngineOutput(
            tp_targets_above=[],
            tp_targets_below=[zone],
            sl_protection_zones=[],
        ),
    )


def _bundle_with_htf_vah(
    vah: float,
) -> FeatureSnapshotBundle:
    weekly = VolumeProfileOutput(
        name=VolumeProfileName.WEEKLY,
        state=VolumeProfileState.DEVELOPING,
        period_start=datetime(2026, 4, 13, tzinfo=UTC),
        period_end=datetime(2026, 4, 20, tzinfo=UTC),
        bin_size=1.0,
        vah=vah,
        vpoc=2375.0,
        val=2370.0,
        value_area_pct=0.70,
        value_status=ValueAreaStatus.WITHIN_VALUE,
        n_bars=1440,
    )
    monthly = VolumeProfileOutput(
        name=VolumeProfileName.MONTHLY,
        state=VolumeProfileState.DEVELOPING,
        period_start=datetime(2026, 4, 1, tzinfo=UTC),
        period_end=datetime(2026, 4, 30, tzinfo=UTC),
        bin_size=1.0,
        vah=2400.0, vpoc=2380.0, val=2350.0,
        value_area_pct=0.70,
        value_status=ValueAreaStatus.WITHIN_VALUE,
        n_bars=40000,
    )
    yearly = VolumeProfileOutput(
        name=VolumeProfileName.YEARLY,
        state=VolumeProfileState.DEVELOPING,
        period_start=datetime(2026, 1, 1, tzinfo=UTC),
        period_end=datetime(2026, 12, 31, tzinfo=UTC),
        bin_size=1.0,
        vah=2500.0, vpoc=2400.0, val=2200.0,
        value_area_pct=0.70,
        value_status=ValueAreaStatus.WITHIN_VALUE,
        n_bars=200000,
    )
    return FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        atr=0.5,
        volume_range=VolumeRangeOutput(
            weekly=weekly,
            monthly=monthly,
            yearly=yearly,
            cluster_within_atr=0.5,
        ),
    )


def _bundle_with_bullish_fvg(top: float) -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        atr=0.5,
        fvg=FVGOutput(
            zones=[
                FVGZone(
                    tf="M5", type=FVGType.BULLISH,
                    top=top, bottom=top - 0.5,
                    size_points=0.5,
                    created_at=datetime(2026, 4, 15, 13, 0, tzinfo=UTC),
                    age_seconds=1800,
                    displacement_atr=1.5,
                    status=FVGStatus.OPEN,
                    mitigation_pct=0.0,
                    rank_score=2.0,
                )
            ],
            top_zones=[],
        ),
    )


# ----------------------------------------------------------------- 1. long TP plan


def test_long_tp_plan_uses_liquidity_for_tp1() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = _bundle_with_long_targets(above_center=2378.0)
    bundle = bundle.model_copy(update={"fvg": _bundle_with_bullish_fvg(top=2380.0).fvg})
    bundle = bundle.model_copy(update={"volume_range": _bundle_with_htf_vah(vah=2385.0).volume_range})

    result = mgr.compute(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        sl_price=Decimal("2370.00"),
        bundle=bundle,
    )
    assert result.tp1_price == Decimal("2378.00")
    # TP2 = M5 bullish FVG top = 2380.00
    assert result.tp2_price == Decimal("2380.00")
    # TP3 = weekly VAH (closest HTF level) = 2385.00
    assert result.tp3_price == Decimal("2385.00")
    # Plan sums to 100 % (30+30+40).
    total_pct = sum(float(p["pct"]) for p in result.partial_close_plan)
    assert abs(total_pct - 1.0) < 1e-6


# ----------------------------------------------------------------- 2. short TP plan


def test_short_tp_plan_uses_liquidity_for_tp1() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = _bundle_with_short_targets(below_center=2370.0)
    result = mgr.compute(
        side=OrderSide.SELL,
        entry_price=Decimal("2375.00"),
        sl_price=Decimal("2380.00"),
        bundle=bundle,
    )
    assert result.tp1_price == Decimal("2370.00")
    # No bullish FVG → 2R fallback.
    assert result.tp2_price is not None
    # 2R from entry = 2375 - 2*5 = 2365
    assert result.tp2_price == Decimal("2365.00")


# ----------------------------------------------------------------- 3. 1R fallback when no liquidity data


def test_long_tp1_falls_back_to_1r_when_no_liquidity() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = FeatureSnapshotBundle(ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC), atr=0.5)
    result = mgr.compute(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        sl_price=Decimal("2370.00"),  # 5 USD SL → 1R = 2380
        bundle=bundle,
    )
    assert result.tp1_price == Decimal("2380.00")
    assert any("1R" in r for r in result.reasoning)


# ----------------------------------------------------------------- 4. HTF-level picker prefers weekly


def test_long_tp3_uses_weekly_vah_over_yearly() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = _bundle_with_htf_vah(vah=2385.0)
    bundle.liquidity = LiquidityEngineOutput(
        tp_targets_above=[
            LiquidityZone(
                kind="high", price_low=2377.0, price_high=2378.0, center=2377.5, pool_count=1,
            )
        ],
    )
    bundle.fvg = FVGOutput(zones=[], top_zones=[])
    result = mgr.compute(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        sl_price=Decimal("2370.00"),
        bundle=bundle,
    )
    assert result.tp3_price == Decimal("2385.00")
    assert any("weekly_vah" in r for r in result.reasoning)


# ----------------------------------------------------------------- 5. runner decision: rejection


def test_runner_should_close_on_structure_rejection_long() -> None:
    """A long runner at VAH with a fresh BOS_down closes."""

    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        structure=MarketStructureOutput(
            swings=[],
            last_bos=StructureEvent(
                type=StructureEventType.BOS_DOWN,
                level=2385.0, time=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
                bar_index=100, close=2383.0, distance_atr=0.5,
            ),
            last_choch=None,
            liquidity_pools=[],
            trend="up", fractal_n=3,
        ),
    )
    close, reason = mgr.should_close_runner(
        side=OrderSide.BUY,
        tp3_price=Decimal("2385.00"),
        current_close=Decimal("2383.00"),
        bundle=bundle,
    )
    assert close is True
    assert "rejection" in reason


def test_runner_continues_with_no_rejection() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = FeatureSnapshotBundle(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        structure=MarketStructureOutput(
            swings=[],
            last_bos=StructureEvent(
                type=StructureEventType.BOS_UP,  # continuation
                level=2380.0, time=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
                bar_index=100, close=2386.0, distance_atr=0.5,
            ),
            last_choch=None,
            liquidity_pools=[],
            trend="up", fractal_n=3,
        ),
    )
    close, _ = mgr.should_close_runner(
        side=OrderSide.BUY,
        tp3_price=Decimal("2385.00"),
        current_close=Decimal("2386.00"),
        bundle=bundle,
    )
    assert close is False


def test_runner_no_structure_data_returns_no_close() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = FeatureSnapshotBundle(ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC))
    close, reason = mgr.should_close_runner(
        side=OrderSide.BUY,
        tp3_price=Decimal("2385.00"),
        current_close=Decimal("2386.00"),
        bundle=bundle,
    )
    assert close is False
    assert reason == "no_structure_data"


# ----------------------------------------------------------------- 6. partial-close percentages


def test_default_partial_close_pct_sums_to_100() -> None:
    from xauusd_bot.execution.take_profit import (
        DEFAULT_TP1_PCT,
        DEFAULT_TP2_PCT,
        DEFAULT_TP3_PCT,
    )

    assert abs(DEFAULT_TP1_PCT + DEFAULT_TP2_PCT + DEFAULT_TP3_PCT - 100.0) < 1e-6


def test_partial_close_plan_uses_configured_fractions() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec, tp1_pct=20, tp2_pct=30, tp3_pct=50)
    bundle = _bundle_with_long_targets(above_center=2378.0)
    result = mgr.compute(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        sl_price=Decimal("2370.00"),
        bundle=bundle,
    )
    pcts = {p["level"]: float(p["pct"]) for p in result.partial_close_plan}
    assert abs(pcts["tp1"] - 0.20) < 1e-6
    assert abs(pcts["tp2"] - 0.30) < 1e-6
    assert abs(pcts["tp3"] - 0.50) < 1e-6


def test_partial_close_fractions_must_sum_to_100() -> None:
    spec = make_symbol_spec()
    with pytest.raises(AssertionError):
        TakeProfitManager(spec=spec, tp1_pct=20, tp2_pct=30, tp3_pct=40)


# ----------------------------------------------------------------- 7. reasoning present


def test_result_contains_reasoning_lines() -> None:
    spec = make_symbol_spec()
    mgr = TakeProfitManager(spec=spec)
    bundle = _bundle_with_long_targets(above_center=2378.0)
    result = mgr.compute(
        side=OrderSide.BUY,
        entry_price=Decimal("2375.00"),
        sl_price=Decimal("2370.00"),
        bundle=bundle,
    )
    assert len(result.reasoning) >= 3
    for line in result.reasoning:
        assert "TP" in line
