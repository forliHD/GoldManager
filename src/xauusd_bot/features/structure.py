"""Market Structure Engine — Swing H/L, BOS, CHOCH, Liquidity Pools.

Definitions (Plan §8)
----------------------
* **Swing H/L** — fractal: a high (or low) is a swing point if the
  ``N`` bars to its left and right are all lower (or higher). Default
  ``N=3``.
* **BOS (Break of Structure)** — a close strictly beyond the most
  recent *external* swing high (or low) in the trend direction.
* **CHOCH (Change of Character)** — a close beyond the *most recent
  swing in the direction of the prevailing trend* in the **opposite**
  direction. E.g. after a series of BOS_UP, the first BOS_DOWN through
  a swing low is a CHOCH_DOWN.
* **Internal vs External** — a swing that is significant in the larger
  context (e.g. an H4 swing during an H1 analysis) is "external". Local
  swings are "internal".

Filtering
---------
To avoid noise, BOS/CHOCH events must satisfy:
* Minimum distance: the breaking close must be at least
  ``min_distance_atr * ATR`` away from the level. Default 0.5.
* Minimum bars between events: ``min_bars_between``. Default 10.

Liquidity Pools
---------------
A liquidity pool is a *swing* that has not yet been "swept" (i.e. price
has not wicked through it and reversed). A pool is "swept" if a
subsequent bar wicked past the level (high > pool for a high pool, or
low < pool for a low pool) and closed back on the *other* side.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

import structlog

from xauusd_bot.common.schemas.features import (
    LiquidityPool,
    MarketStructureOutput,
    StructureEvent,
    StructureEventType,
    SwingPoint,
)
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df, round_bars_by_time

log = structlog.get_logger(__name__)


def _find_swings(bars: list[Bar], n: int) -> list[SwingPoint]:
    """N-bar fractal swings. A swing high at index i needs the N bars
    on each side to have a strictly lower high."""

    out: list[SwingPoint] = []
    for i in range(n, len(bars) - n):
        h = float(bars[i].high)
        low = float(bars[i].low)
        # Check left and right.
        is_high = all(float(bars[i - j].high) < h for j in range(1, n + 1)) and all(
            float(bars[i + j].high) < h for j in range(1, n + 1)
        )
        is_low = all(float(bars[i - j].low) > low for j in range(1, n + 1)) and all(
            float(bars[i + j].low) > low for j in range(1, n + 1)
        )
        if is_high:
            out.append(
                SwingPoint(
                    kind="high",
                    price=h,
                    time=bars[i].time,
                    bar_index=i,
                    is_external=True,  # refined by caller
                )
            )
        elif is_low:
            out.append(
                SwingPoint(
                    kind="low",
                    price=low,
                    time=bars[i].time,
                    bar_index=i,
                    is_external=True,
                )
            )
    return out


def _detect_events(
    bars: list[Bar],
    swings: list[SwingPoint],
    atr_value: float | None,
    min_distance_atr: float,
    min_bars_between: int,
) -> list[StructureEvent]:
    """Walk the bars forward, detecting BOS/CHOCH at every swing break."""

    events: list[StructureEvent] = []
    last_event_idx = -10_000  # start far in the past
    last_trend: Literal["up", "down", "range"] = "range"
    last_swing_high_idx = -1
    last_swing_low_idx = -1

    # Pre-compute swing lookups.
    swing_highs: list[SwingPoint] = sorted([s for s in swings if s.kind == "high"], key=lambda s: s.bar_index)
    swing_lows: list[SwingPoint] = sorted([s for s in swings if s.kind == "low"], key=lambda s: s.bar_index)

    for i, b in enumerate(bars):
        # Find the most recent swing high and low seen so far.
        while last_swing_high_idx + 1 < len(swing_highs) and swing_highs[last_swing_high_idx + 1].bar_index <= i:
            last_swing_high_idx += 1
        while last_swing_low_idx + 1 < len(swing_lows) and swing_lows[last_swing_low_idx + 1].bar_index <= i:
            last_swing_low_idx += 1

        if i - last_event_idx < min_bars_between:
            continue

        c = float(b.close)
        # BOS_UP: close above the last swing high.
        if last_swing_high_idx >= 0:
            sh = swing_highs[last_swing_high_idx]
            min_dist = min_distance_atr * atr_value if atr_value and atr_value > 0 else 0
            if c > sh.price and (c - sh.price) >= min_dist:
                events.append(
                    StructureEvent(
                        type=StructureEventType.BOS_UP,
                        level=sh.price,
                        time=b.time,
                        bar_index=i,
                        close=c,
                        distance_atr=(c - sh.price) / atr_value if atr_value and atr_value > 0 else 0.0,
                    )
                )
                last_event_idx = i
                if last_trend != "up":
                    last_trend = "up"
                continue
        # BOS_DOWN: close below the last swing low.
        if last_swing_low_idx >= 0:
            sl = swing_lows[last_swing_low_idx]
            min_dist = min_distance_atr * atr_value if atr_value and atr_value > 0 else 0
            if c < sl.price and (sl.price - c) >= min_dist:
                # If we were in an uptrend, this is a CHOCH_DOWN.
                event_type = (
                    StructureEventType.CHOCH_DOWN
                    if last_trend == "up"
                    else StructureEventType.BOS_DOWN
                )
                events.append(
                    StructureEvent(
                        type=event_type,
                        level=sl.price,
                        time=b.time,
                        bar_index=i,
                        close=c,
                        distance_atr=(sl.price - c) / atr_value if atr_value and atr_value > 0 else 0.0,
                    )
                )
                last_event_idx = i
                last_trend = "down"
                continue
        # CHOCH_UP: when in a downtrend, close above the last swing high.
        if last_trend == "down" and last_swing_high_idx >= 0:
            sh = swing_highs[last_swing_high_idx]
            min_dist = min_distance_atr * atr_value if atr_value and atr_value > 0 else 0
            if c > sh.price and (c - sh.price) >= min_dist:
                events.append(
                    StructureEvent(
                        type=StructureEventType.CHOCH_UP,
                        level=sh.price,
                        time=b.time,
                        bar_index=i,
                        close=c,
                        distance_atr=(c - sh.price) / atr_value if atr_value and atr_value > 0 else 0.0,
                    )
                )
                last_event_idx = i
                last_trend = "up"

    return events


def _liquidity_pools(
    swings: list[SwingPoint],
    bars: list[Bar],
) -> list[LiquidityPool]:
    """Swings that haven't been swept (price hasn't wicked through and reversed)."""

    out: list[LiquidityPool] = []
    for s in swings:
        swept = False
        sweep_time: datetime | None = None
        for b in bars[s.bar_index + 1 :]:
            if s.kind == "high":
                if float(b.high) > s.price and float(b.close) < s.price:
                    swept = True
                    sweep_time = b.time
                    break
            else:  # low
                if float(b.low) < s.price and float(b.close) > s.price:
                    swept = True
                    sweep_time = b.time
                    break
        out.append(
            LiquidityPool(
                kind=s.kind,
                price=s.price,
                created_at=s.time,
                swept=swept,
                sweep_time=sweep_time,
            )
        )
    return out


class MarketStructureEngine:
    """Compute market-structure features for a given bar series."""

    def __init__(
        self,
        fractal_n: int = 3,
        min_distance_atr: float = 0.5,
        min_bars_between: int = 10,
        timeframe_minutes: int = 5,
    ) -> None:
        self._n = fractal_n
        self._min_dist_atr = min_distance_atr
        self._min_bars = min_bars_between
        self._tf_minutes = timeframe_minutes

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> MarketStructureOutput:
        bars_pit = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)
        # If the bars are already at or above the target timeframe, skip
        # resampling. We detect this by checking the most common delta
        # between consecutive bars.
        tf_bars = self._maybe_resample(bars_pit)
        if not tf_bars:
            return MarketStructureOutput(
                swings=[],
                last_bos=None,
                last_choch=None,
                liquidity_pools=[],
                trend="range",
                fractal_n=self._n,
            )

        df = bars_to_df(bars_pit)
        atr_value = compute_atr(df, period=14)

        swings = _find_swings(tf_bars, self._n)
        events = _detect_events(tf_bars, swings, atr_value, self._min_dist_atr, self._min_bars)
        pools = _liquidity_pools(swings, tf_bars)

        # Trend: the direction of the most recent event.
        last_bos = None
        last_choch = None
        trend: Literal["up", "down", "range"] = "range"
        for e in events:
            if e.type in (StructureEventType.BOS_UP, StructureEventType.BOS_DOWN):
                last_bos = e
                trend = "up" if e.type == StructureEventType.BOS_UP else "down"
            elif e.type in (StructureEventType.CHOCH_UP, StructureEventType.CHOCH_DOWN):
                last_choch = e
                trend = "up" if e.type == StructureEventType.CHOCH_UP else "down"

        return MarketStructureOutput(
            swings=swings,
            last_bos=last_bos,
            last_choch=last_choch,
            liquidity_pools=pools,
            trend=trend,
            fractal_n=self._n,
        )

    def _maybe_resample(self, bars: list[Bar]) -> list[Bar]:
        """If bars are already at the target TF (or coarser), skip resampling.

        We detect this by looking at the *typical* (median) gap between
        consecutive bars. If it's >= the target minutes, the bars are
        already at or above the target TF and we don't resample.
        """

        if len(bars) < 2 or self._tf_minutes == 1:
            return bars
        # Median gap in minutes (more robust than average for small samples).
        gaps_min: list[float] = []
        for a, b in zip(bars, bars[1:], strict=False):
            gaps_min.append((b.time - a.time).total_seconds() / 60.0)
        gaps_min.sort()
        median_gap = gaps_min[len(gaps_min) // 2]
        if median_gap >= self._tf_minutes:
            return bars
        return round_bars_by_time(bars, self._tf_minutes)
