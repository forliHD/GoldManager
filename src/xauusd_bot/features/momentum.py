"""Candle / Momentum Engine — quantitative-only candle features (no pattern names).

Per Plan §8 and the AGENTS.md "no pattern names" rule, this engine emits
**only** numeric candle-shape features. It never labels a bar as a
"hammer", "shooting star", "engulfing", etc. — the decision layer can
pattern-match on the numeric features if it wants to, but the engine
itself stays pattern-agnostic.

Per-bar features
----------------
* ``body_size_atr`` = |close - open| / ATR  (large body → strong move)
* ``wick_body_ratio`` = (range - body) / body  (high → wicky, low → blocky)
* ``close_position`` = (close - low) / (high - low) ∈ [0, 1]  (1 = closed at high)
* ``displacement`` = body > 2×ATR OR body > 1.5×median(body)
* ``impulsive_follow_through`` = number of consecutive bars in the same
  direction (close > open, or close < open) ending at this bar
* ``tick_volume_percentile`` = this bar's tick_volume percentile vs the
  last 100 bars (relative, AGENTS.md I-5)

Aggregate
---------
A 0-100 score per timeframe, weighted by component importance:
* displacement 35 %
* impulsive follow-through 25 %
* body/ATR 20 %
* tick-volume percentile 20 %

The score is a *momentum* measure, not a direction. The decision layer
maps score + direction (via other engines) to trade intent.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

import pandas as pd
import structlog

from xauusd_bot.common.schemas.features import (
    CandleMomentumOutput,
    CandleMomentumPerBar,
)
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df, round_bars_by_time

log = structlog.get_logger(__name__)

_TIMEFRAME_BUCKETS: dict[str, int] = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240}

_DISPLACEMENT_ATR = 2.0
_DISPLACEMENT_MEDIAN = 1.5
_FOLLOW_THROUGH_TARGET = 3
_VOLUME_LOOKBACK = 100


def _per_bar_features(
    bars: list[Bar], atr_value: float | None, current_t: datetime
) -> CandleMomentumPerBar | None:
    """Compute per-bar features for the last bar in the series."""

    if not bars:
        return None
    last = bars[-1]
    o = float(last.open)
    h = float(last.high)
    low = float(last.low)
    c = float(last.close)
    body = abs(c - o)
    rng = h - low
    if rng <= 0:
        # Degenerate bar; doji with no range.
        return CandleMomentumPerBar(
            body_size_atr=0.0,
            wick_body_ratio=0.0,
            close_position=0.5,
            displacement=False,
            impulsive_follow_through=0,
            tick_volume_percentile=50.0,
            tick_volume=float(last.tick_volume),
        )

    # body_size_atr
    body_atr = body / atr_value if atr_value and atr_value > 0 else 0.0

    # wick_body_ratio
    wick_body = (rng - body) / body if body > 0 else 1.0

    # close_position
    close_pos = (c - low) / rng

    # displacement
    displacement = False
    if atr_value and atr_value > 0 and body > _DISPLACEMENT_ATR * atr_value:
        displacement = True
    elif len(bars) >= 20:
        # 1.5× median body across the last 20 bars.
        bodies = [abs(float(b.close) - float(b.open)) for b in bars[-20:]]
        bodies_sorted = sorted(bodies)
        median_body = bodies_sorted[len(bodies_sorted) // 2]
        if median_body > 0 and body > _DISPLACEMENT_MEDIAN * median_body:
            displacement = True

    # impulsive follow-through: count consecutive bars in the same direction
    # ending at `last`. "Same direction" = bullish (close > open) or
    # bearish (close < open). Doji (close == open) resets the count.
    follow_through = 0
    if c > o:
        direction = "up"
    elif c < o:
        direction = "down"
    else:
        direction = None
    if direction is not None:
        for b in reversed(bars):
            bc = float(b.close)
            bo = float(b.open)
            if direction == "up" and bc > bo or direction == "down" and bc < bo:
                follow_through += 1
            else:
                break

    # tick-volume percentile (relative, last 100 bars)
    if len(bars) >= 2:
        lookback = bars[-_VOLUME_LOOKBACK:] if len(bars) >= _VOLUME_LOOKBACK else bars
        vols = pd.Series([int(b.tick_volume) for b in lookback])
        if len(vols) >= 2:
            rank = (vols < int(last.tick_volume)).sum() / (len(vols) - 1) * 100.0
            tv_pct = float(rank)
        else:
            tv_pct = 50.0
    else:
        tv_pct = 50.0

    return CandleMomentumPerBar(
        body_size_atr=body_atr,
        wick_body_ratio=wick_body,
        close_position=close_pos,
        displacement=displacement,
        impulsive_follow_through=follow_through,
        tick_volume_percentile=tv_pct,
        tick_volume=float(last.tick_volume),
    )


def _aggregate_score(per: CandleMomentumPerBar | None) -> float:
    """Combine per-bar features into a 0-100 momentum score."""

    if per is None:
        return 0.0
    # displacement: 35 points
    disp_score = 35.0 if per.displacement else 0.0
    # follow-through: 25 points, scaled by 1 - exp(-n/3)
    import math

    ft_score = 25.0 * (1 - math.exp(-per.impulsive_follow_through / _FOLLOW_THROUGH_TARGET))
    # body/ATR: 20 points, scaled by 1 - exp(-body/2)
    body_score = 20.0 * (1 - math.exp(-per.body_size_atr / 2.0))
    # tick-vol percentile: 20 points (already 0-100)
    vol_score = 0.2 * per.tick_volume_percentile
    return min(100.0, max(0.0, disp_score + ft_score + body_score + vol_score))


class CandleMomentumEngine:
    """Compute per-bar features and aggregate scores per timeframe."""

    def __init__(
        self,
        timeframes: tuple[Literal["M1", "M5", "M15", "H1", "H4"], ...] = ("M1", "M5", "H1"),
    ) -> None:
        self._tfs = timeframes

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> CandleMomentumOutput:
        bars_m1 = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)
        # ATR for body/ATR normalization (use the full M1 series).
        df = bars_to_df(bars_m1)
        atr_val = compute_atr(df, period=14)

        by_tf: dict[str, CandleMomentumPerBar] = {}
        for tf in self._tfs:
            tf_bars = (
                bars_m1
                if tf == "M1"
                else round_bars_by_time(bars_m1, _TIMEFRAME_BUCKETS.get(tf, 1))
            )
            if not tf_bars:
                continue
            by_tf[tf] = _per_bar_features(tf_bars, atr_val, current_t)  # type: ignore[assignment]

        # Aggregate score: weighted mean of per-TF scores.
        scores = [_aggregate_score(by_tf[tf]) for tf in self._tfs if tf in by_tf]
        score = sum(scores) / len(scores) if scores else 0.0
        return CandleMomentumOutput(by_tf=by_tf, score=score)
