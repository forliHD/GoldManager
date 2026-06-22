"""FibRetracementEngine — fib retracement of the last H1 impulse leg.

Feeds decision_agent.md §2 (H1 structure & fib position). M1 bars are
resampled to H1, fractal swings are found, the last impulse leg (the two most
recent opposing swings) is taken, and the current price is placed inside its
fib retracement — with a golden-pocket flag (0.5–0.618) and a trend-strength
read (leg size vs H1 ATR).

Point-in-time: only bars with ``time <= current_t`` are read (I-3).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from xauusd_bot.common.schemas.features import FibRetracementOutput
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import round_bars_by_time

_H1_MINUTES = 60


def _fractal_swings(bars: list[Bar], n: int) -> list[tuple[str, float, int]]:
    """N-bar fractal swings → list of (kind, price, index), chronological."""

    out: list[tuple[str, float, int]] = []
    for i in range(n, len(bars) - n):
        h = float(bars[i].high)
        low = float(bars[i].low)
        is_high = all(float(bars[i - j].high) < h for j in range(1, n + 1)) and all(
            float(bars[i + j].high) < h for j in range(1, n + 1)
        )
        is_low = all(float(bars[i - j].low) > low for j in range(1, n + 1)) and all(
            float(bars[i + j].low) > low for j in range(1, n + 1)
        )
        if is_high:
            out.append(("high", h, i))
        elif is_low:
            out.append(("low", low, i))
    return out


class FibRetracementEngine:
    """Compute the fib retracement of the last H1 impulse leg for one snapshot."""

    def __init__(
        self,
        *,
        fractal_n: int = 2,
        h1_minutes: int = _H1_MINUTES,
        strong_atr_mult: float = 3.0,
    ) -> None:
        self._n = fractal_n
        self._h1_minutes = h1_minutes
        self._strong_mult = strong_atr_mult

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> FibRetracementOutput:
        m1 = sorted((b for b in bars if b.time <= current_t), key=lambda b: b.time)
        if not m1:
            return FibRetracementOutput()
        h1 = round_bars_by_time(m1, self._h1_minutes)
        if len(h1) < 2 * self._n + 2:
            return FibRetracementOutput()

        swings = _fractal_swings(h1, self._n)
        if len(swings) < 2:
            return FibRetracementOutput()

        last_kind, last_price, _ = swings[-1]
        prior = next((s for s in reversed(swings[:-1]) if s[0] != last_kind), None)
        if prior is None:
            return FibRetracementOutput()
        prior_price = prior[1]

        if last_kind == "high":  # up impulse: prior low → last high
            direction = "up"
            leg_low, leg_high = prior_price, last_price
        else:  # down impulse: prior high → last low
            direction = "down"
            leg_high, leg_low = prior_price, last_price

        size = leg_high - leg_low
        if size <= 0:
            return FibRetracementOutput()

        def lvl(x: float) -> float:
            # Retracement measured back from the impulse extreme.
            return (leg_high - x * size) if direction == "up" else (leg_low + x * size)

        cur = float(m1[-1].close)
        # Retracement fraction: 0 = at the impulse extreme, 1 = back at the origin.
        r = (leg_high - cur) / size if direction == "up" else (cur - leg_low) / size

        if r < 0:
            zone = "extended"          # price made a fresh extreme beyond the leg
        elif r < 0.382:
            zone = "shallow"
        elif r < 0.5:
            zone = "0.382"
        elif r <= 0.618:
            zone = "golden_pocket"
        elif r <= 1.0:
            zone = "deep"
        else:
            zone = "extended"          # retraced past the origin → leg likely invalid

        # Strength = leg size vs the typical H1 bar range. We use the mean
        # high-low range (not ATR(14)) so it's defined even on a short H1
        # history — the backtest context window is only ~10 H1 bars, where
        # ATR(14) would be undefined and strength would always read "none".
        ranges = [float(b.high) - float(b.low) for b in h1 if b.high >= b.low]
        h1_range = (sum(ranges) / len(ranges)) if ranges else 0.0
        if h1_range > 0:
            trend_strength = "strong" if (size / h1_range) >= self._strong_mult else "weak"
        else:
            trend_strength = "none"

        return FibRetracementOutput(
            direction=direction,  # type: ignore[arg-type]
            leg_low=leg_low,
            leg_high=leg_high,
            fib_236=lvl(0.236),
            fib_382=lvl(0.382),
            fib_500=lvl(0.5),
            fib_618=lvl(0.618),
            retracement_pct=r,
            price_zone=zone,  # type: ignore[arg-type]
            in_golden_pocket=(zone == "golden_pocket"),
            trend_strength=trend_strength,  # type: ignore[arg-type]
        )


__all__ = ["FibRetracementEngine"]
