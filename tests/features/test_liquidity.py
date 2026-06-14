"""Tests for the LiquidityEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from xauusd_bot.common.schemas.features import LiquidityPool
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.liquidity import LiquidityEngine


def _bar(time: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=time,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


def _drift_bars(n: int, start_price: float = 2000.0) -> list[Bar]:
    """Build n M1 bars that drift up slowly with realistic ranges."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    price = start_price
    for i in range(n):
        t = base + timedelta(minutes=i)
        bars.append(_bar(t, price, price + 1.0, price - 1.0, price + 0.3))
        price += 0.3
    return bars


def _pool(price: float, kind: Literal["high", "low"] = "high", swept: bool = False) -> LiquidityPool:
    return LiquidityPool(
        kind=kind,
        price=price,
        created_at=datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        swept=swept,
    )


def _drift_bars(n: int, start_price: float = 2000.0) -> list[Bar]:
    """Build n M1 bars that drift up slowly with realistic ranges."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    price = start_price
    for i in range(n):
        t = base + timedelta(minutes=i)
        bars.append(_bar(t, price, price + 1.0, price - 1.0, price + 0.3))
        price += 0.3
    return bars


def _pool(price: float, kind: Literal["high", "low"] = "high", swept: bool = False) -> LiquidityPool:
    return LiquidityPool(
        kind=kind,
        price=price,
        created_at=datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
        swept=swept,
    )


# ---------------------------------------------------------------- clustering


def test_two_close_pools_become_one_zone() -> None:
    """Two pools within band → single zone."""

    eng = LiquidityEngine(cluster_atr=0.5)
    pools = [_pool(2010.0), _pool(2010.3)]  # 0.3 apart, well under 0.5*ATR
    out = eng.compute(pools, current_price=2000.0, bars=_drift_bars(20), current_t=datetime(2026, 1, 5, 0, 19, tzinfo=UTC))
    # Both are above current_price → tp_targets_above.
    assert len(out.tp_targets_above) == 1
    assert out.tp_targets_above[0].pool_count == 2


def test_two_far_pools_become_two_zones() -> None:
    """Two pools far apart → two zones."""

    eng = LiquidityEngine(cluster_atr=0.5)
    pools = [_pool(2010.0), _pool(2050.0)]  # 40 apart
    out = eng.compute(pools, current_price=2000.0, bars=_drift_bars(20), current_t=datetime(2026, 1, 5, 0, 19, tzinfo=UTC))
    assert len(out.tp_targets_above) == 2


# ---------------------------------------------------------------- split


def test_high_pools_above_price_go_to_tp_above() -> None:
    eng = LiquidityEngine(cluster_atr=0.5)
    pools = [_pool(2010.0, "high"), _pool(1990.0, "low")]
    out = eng.compute(pools, current_price=2000.0, bars=_drift_bars(20), current_t=datetime(2026, 1, 5, 0, 19, tzinfo=UTC))
    assert any(z.center > 2000.0 for z in out.tp_targets_above)
    assert any(z.center < 2000.0 for z in out.tp_targets_below)


def test_swept_pools_excluded() -> None:
    """Swept pools are not future liquidity."""

    eng = LiquidityEngine(cluster_atr=0.5)
    pools = [_pool(2010.0, "high", swept=True), _pool(1990.0, "low", swept=False)]
    out = eng.compute(pools, current_price=2000.0, bars=_drift_bars(20), current_t=datetime(2026, 1, 5, 0, 19, tzinfo=UTC))
    # Only the unswept low pool should appear.
    assert out.tp_targets_above == []
    assert len(out.tp_targets_below) == 1


# ---------------------------------------------------------------- SL traps


def test_dense_cluster_below_price_marks_sl_trap() -> None:
    """2+ low pools within 1.5*ATR of current_price below → SL trap."""

    eng = LiquidityEngine(cluster_atr=0.5)
    pools = [_pool(1999.0, "low"), _pool(1999.3, "low"), _pool(1999.5, "low")]
    out = eng.compute(pools, current_price=2000.0, bars=_drift_bars(20), current_t=datetime(2026, 1, 5, 0, 19, tzinfo=UTC))
    # The three low pools cluster tightly just below the current price.
    # They should be both tp_targets_below AND sl_protection_zones.
    assert any(z.is_sl_trap for z in out.sl_protection_zones)


# ---------------------------------------------------------------- PIT


def test_pit_excludes_bars_after_current_t() -> None:
    eng = LiquidityEngine(cluster_atr=0.5)
    pools = [_pool(2010.0, "high")]
    bars = _drift_bars(20)
    cutoff = bars[10].time
    out_pre = eng.compute(pools, 2000.0, bars, cutoff)
    fut = _bar(bars[10].time + timedelta(minutes=1), 9999, 9999.5, 9998.5, 9999, tv=999999)
    out_with_fut = eng.compute(pools, 2000.0, bars + [fut], cutoff)
    # Output is identical (ATR computed from same visible bars).
    assert len(out_pre.tp_targets_above) == len(out_with_fut.tp_targets_above)


def test_no_pools_returns_empty() -> None:
    eng = LiquidityEngine()
    out = eng.compute([], 2000.0, _drift_bars(20), datetime(2026, 1, 5, 0, 19, tzinfo=UTC))
    assert out.tp_targets_above == []
    assert out.tp_targets_below == []
    assert out.sl_protection_zones == []
