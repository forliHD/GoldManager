"""Liquidity Engine — clusters of liquidity pools, TP targets, SL traps.

This engine takes the *raw* liquidity pools (swing points) detected by
:mod:`xauusd_bot.features.structure` and the *untested* session levels
from :mod:`xauusd_bot.features.session`, clusters them into zones, and
classifies the zones by purpose:

* **TP targets** — liquidity that, if reached, would offer a
  reasonable place to take profit. Two sub-types:
    * ``tp_targets_above`` — pools with price > current_price.
    * ``tp_targets_below`` — pools with price < current_price.
* **SL protection zones** — clusters of pools *below* the current
  price (for long trades) or *above* (for short trades) that represent
  a high density of stop-loss orders. The bot should not place its
  protective stop *inside* such a cluster.

Clustering
----------
Two pools are in the same zone if their prices differ by at most
``cluster_atr * ATR``. Default 0.5 ATR. The zone's price range is
``[min(pool prices), max(pool prices)]`` and the center is the mean.

The current ATR is the simplest "scale" reference for the cluster size.
In a backtest with constant ATR this gives stable, reproducible zones.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import structlog

from xauusd_bot.common.schemas.features import (
    LiquidityEngineOutput,
    LiquidityPool,
    LiquidityZone,
)
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df

log = structlog.get_logger(__name__)


def _cluster(
    pools: list[LiquidityPool],
    band: float,
) -> list[LiquidityZone]:
    """Greedy 1D clustering of pools by price.

    Sort pools by price, walk through, group consecutive pools that
    are within ``band`` of each other.
    """

    if not pools:
        return []
    sorted_pools = sorted(pools, key=lambda p: p.price)
    clusters: list[list[LiquidityPool]] = []
    current: list[LiquidityPool] = [sorted_pools[0]]
    for pool in sorted_pools[1:]:
        if pool.price - current[-1].price <= band:
            current.append(pool)
        else:
            clusters.append(current)
            current = [pool]
    clusters.append(current)
    return [
        LiquidityZone(
            kind=current_cluster[0].kind,
            price_low=min(p.price for p in current_cluster),
            price_high=max(p.price for p in current_cluster),
            center=sum(p.price for p in current_cluster) / len(current_cluster),
            pool_count=len(current_cluster),
        )
        for current_cluster in clusters
    ]


class LiquidityEngine:
    """Cluster liquidity pools into zones, classify as TP targets or SL traps.

    Parameters
    ----------
    cluster_atr:
        Maximum price distance (in ATRs) between two pools to be in the
        same zone. Default 0.5.
    """

    def __init__(self, cluster_atr: float = 0.5) -> None:
        self._cluster_atr = cluster_atr

    def compute(
        self,
        pools: Iterable[LiquidityPool],
        current_price: float,
        bars: Iterable[Bar],
        current_t: datetime,
    ) -> LiquidityEngineOutput:
        """Cluster pools and split by direction.

        ``current_price`` is the latest close; the function splits pools
        into "above" and "below" relative to it.
        """

        # PIT filter is the caller's responsibility for the bars, but
        # we don't depend on bars except for ATR.
        bars_pit = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)
        df = bars_to_df(bars_pit)
        atr_value = compute_atr(df, period=14) or 0.0
        band = self._cluster_atr * atr_value if atr_value > 0 else 1.0

        # Untested pools only — swept pools are not future liquidity
        # because they've already been taken.
        unswept = [p for p in pools if not p.swept]
        if not unswept:
            return LiquidityEngineOutput(tp_targets_above=[], tp_targets_below=[], sl_protection_zones=[])

        # Split by kind.
        high_pools = [p for p in unswept if p.kind == "high"]
        low_pools = [p for p in unswept if p.kind == "low"]

        # Cluster each kind.
        high_zones = _cluster(high_pools, band)
        low_zones = _cluster(low_pools, band)

        # Above / below split: high pools with center > current_price
        # are tp_targets_above; otherwise they're sl_protection (if
        # they're below current price and represent stop clusters).
        tp_above = [z for z in high_zones if z.center > current_price]
        tp_below = [z for z in low_zones if z.center < current_price]
        # SL traps: clusters of multiple pools that sit just under (for
        # longs) or just over (for shorts) the current price. We define
        # "SL trap" as a cluster of 2+ pools with center within 1.5*ATR
        # of the current price, on the side that would be a stop zone.
        sl_below: list[LiquidityZone] = []
        sl_above: list[LiquidityZone] = []
        if atr_value > 0:
            proximity = 1.5 * atr_value
            sl_below = [
                z
                for z in low_zones
                if z.pool_count >= 2 and 0 < (current_price - z.center) <= proximity
            ]
            sl_above = [
                z
                for z in high_zones
                if z.pool_count >= 2 and 0 < (z.center - current_price) <= proximity
            ]
        # Mark the sl_below / sl_above zones as traps.
        for z in sl_below + sl_above:
            z.is_sl_trap = True

        return LiquidityEngineOutput(
            tp_targets_above=tp_above,
            tp_targets_below=tp_below,
            sl_protection_zones=sl_below + sl_above,
        )
