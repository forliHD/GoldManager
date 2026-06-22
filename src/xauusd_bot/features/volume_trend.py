"""VolumeTrendEngine — tick-volume trend + spike on M1.

Feeds the AI layer's volume-confirmation step (decision_agent.md §6): the
strategy reads a *weakening* volume slope into a zone/consolidation, then a
volume *spike* on the reaction / breakout candle.

Settings (validated on real XAUUSD M1, 2026-06-20)
--------------------------------------------------
* The classic MA9/MA20 **crossover** is too noisy on M1 (~120 flips/day) to be
  a regime signal, so ``trend`` uses the **slope of the fast MA** over a short
  lookback instead (falling = weakening).
* ``is_spike`` uses ``last_volume / MA_slow > spike_mult`` (≈ 3 genuine
  spikes/day at 2.0×), NOT the MA cross.
* ``ma_fast`` (9) and ``ma_slow`` (20) are still exposed because they match the
  operator's MetaTrader chart overlay.

Point-in-time: only bars with ``time <= current_t`` are read (I-3).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from xauusd_bot.common.schemas.features import VolumeTrendOutput
from xauusd_bot.connectors.schemas import Bar


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


class VolumeTrendEngine:
    """Compute the M1 tick-volume trend + spike flag for one snapshot."""

    def __init__(
        self,
        *,
        fast: int = 9,
        slow: int = 20,
        slope_lookback: int = 10,
        spike_mult: float = 2.0,
        flat_band_pct: float = 0.03,
    ) -> None:
        if fast < 1 or slow < 1 or fast > slow:
            raise ValueError(f"need 1 <= fast <= slow, got fast={fast}, slow={slow}")
        self._fast = fast
        self._slow = slow
        self._slope_lookback = slope_lookback
        self._spike_mult = spike_mult
        self._flat_band = flat_band_pct

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> VolumeTrendOutput:
        vols = [
            float(b.tick_volume)
            for b in sorted(
                (b for b in bars if b.time <= current_t), key=lambda b: b.time
            )
        ]
        if len(vols) < self._slow:
            return VolumeTrendOutput()  # not enough history → conservative defaults

        ma_fast = _mean(vols[-self._fast:])
        ma_slow = _mean(vols[-self._slow:])
        last_volume = vols[-1]

        spike_ratio = (last_volume / ma_slow) if ma_slow > 0 else None
        is_spike = spike_ratio is not None and spike_ratio > self._spike_mult

        # Trend = slope of the fast MA over ``slope_lookback`` bars (NOT the MA cross).
        trend = "flat"
        slope_pct: float | None = None
        if len(vols) >= self._fast + self._slope_lookback:
            prev_window = vols[-(self._fast + self._slope_lookback): -self._slope_lookback]
            ma_fast_prev = _mean(prev_window)
            if ma_fast_prev > 0:
                slope_pct = (ma_fast - ma_fast_prev) / ma_fast_prev
                if slope_pct > self._flat_band:
                    trend = "rising"
                elif slope_pct < -self._flat_band:
                    trend = "falling"

        return VolumeTrendOutput(
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            last_volume=last_volume,
            spike_ratio=spike_ratio,
            is_spike=is_spike,
            trend=trend,  # type: ignore[arg-type]
            slope_pct=slope_pct,
        )


__all__ = ["VolumeTrendEngine"]
