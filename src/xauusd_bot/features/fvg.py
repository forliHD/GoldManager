"""Fair Value Gap (FVG) Engine — H1 / M5 / M1.

A Fair Value Gap is a 3-bar pattern where the wicks of bars 1 and 3 do
not overlap, leaving a "gap" of unfilled price action:

* Bullish FVG: ``low[t] > high[t-2]`` (price gapped up)
* Bearish FVG: ``high[t] < low[t-2]`` (price gapped down)

Per Plan §8: H1 is the primary zone timeframe (zones that matter for
multi-hour trades), M5 refines them (better entry timing), M1 is the
trigger (a tap into the zone → look for entry).

Mitigation
----------
* **open** — the zone has not been touched since creation.
* **partially_mitigated** — a close (or wick) has entered the zone but
  not filled it entirely.
* **mitigated** — the entire zone has been filled (close/wick on both
  sides has crossed through).

The mitigation test is on **closes** (not wicks): a wick into the zone
counts as partial fill only if a *close* also follows. This is more
robust against wick-hunting stop-runs.

Displacement
------------
The "displacement" at FVG creation is the size of the middle bar (t-1)
relative to ATR. A big displacement = a strong impulse, the FVG is more
likely to act as support/resistance. We compute
``displacement_atr = body[t-1] / ATR``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

import structlog

from xauusd_bot.common.schemas.features import FVGOutput, FVGStatus, FVGType, FVGZone
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import (
    atr as compute_atr,
)
from xauusd_bot.features._indicators import bars_to_df, round_bars_by_time

log = structlog.get_logger(__name__)


def _detect_fvgs_on_series(
    bars: list[Bar],
    tf: Literal["H1", "M5", "M1"],
    atr_value: float | None,
    current_t: datetime,
) -> list[FVGZone]:
    """Detect FVGs on a pre-built bar series (any timeframe)."""

    if len(bars) < 3:
        return []
    zones: list[FVGZone] = []
    for i in range(2, len(bars)):
        b0 = bars[i - 2]
        b1 = bars[i - 1]  # the displacement bar
        b2 = bars[i]
        # Bullish FVG: bar 2's low is above bar 0's high.
        if float(b2.low) > float(b0.high):
            top = float(b2.low)
            bottom = float(b0.high)
            size = top - bottom
            if size <= 0:
                continue
            disp = (
                abs(float(b1.close) - float(b1.open)) / atr_value
                if atr_value and atr_value > 0
                else 0.0
            )
            zones.append(
                FVGZone(
                    tf=tf,
                    type=FVGType.BULLISH,
                    top=top,
                    bottom=bottom,
                    size_points=size,
                    created_at=b2.time,
                    age_seconds=int((current_t - b2.time).total_seconds()),
                    displacement_atr=disp,
                    status=FVGStatus.OPEN,  # refined below
                    mitigation_pct=0.0,
                    rank_score=0.0,
                )
            )
        # Bearish FVG: bar 2's high is below bar 0's low.
        elif float(b2.high) < float(b0.low):
            top = float(b0.low)
            bottom = float(b2.high)
            size = top - bottom
            if size <= 0:
                continue
            disp = (
                abs(float(b1.close) - float(b1.open)) / atr_value
                if atr_value and atr_value > 0
                else 0.0
            )
            zones.append(
                FVGZone(
                    tf=tf,
                    type=FVGType.BEARISH,
                    top=top,
                    bottom=bottom,
                    size_points=size,
                    created_at=b2.time,
                    age_seconds=int((current_t - b2.time).total_seconds()),
                    displacement_atr=disp,
                    status=FVGStatus.OPEN,
                    mitigation_pct=0.0,
                    rank_score=0.0,
                )
            )

    return zones


def _refine_mitigation(
    zones: list[FVGZone],
    bars_after: list[Bar],
) -> list[FVGZone]:
    """Walk forward from each zone's creation and update its mitigation status.

    A bar's *close* is the basis for mitigation (not the wick). A zone
    is "mitigated" only if a close on one side and a close on the other
    side have both occurred (i.e. the zone was completely filled).
    """

    out: list[FVGZone] = []
    for zone in zones:
        # Bars strictly after the zone was created.
        subsequent = [b for b in bars_after if b.time > zone.created_at]
        if not subsequent:
            out.append(zone)
            continue

        # Mitigation: for a bullish zone, "filled from above" = a close
        # <= zone.bottom. For a bearish zone, "filled from below" = a
        # close >= zone.top. A zone is "fully mitigated" only if the
        # close has crossed the *opposite* side.
        fully_mitigated = False
        partial_mitigation = False
        for b in subsequent:
            c = float(b.close)
            if zone.type == FVGType.BULLISH:
                if c <= zone.bottom:
                    fully_mitigated = True
                    break
                if zone.bottom <= c <= zone.top:
                    partial_mitigation = True
            else:  # BEARISH
                if c >= zone.top:
                    fully_mitigated = True
                    break
                if zone.bottom <= c <= zone.top:
                    partial_mitigation = True

        if fully_mitigated:
            new_status = FVGStatus.MITIGATED
            mit_pct = 100.0
        elif partial_mitigation:
            new_status = FVGStatus.PARTIALLY_MITIGATED
            # Approximate: 50 % by convention when the close sits inside.
            mit_pct = 50.0
        else:
            new_status = FVGStatus.OPEN
            mit_pct = 0.0

        out.append(
            zone.model_copy(update={"status": new_status, "mitigation_pct": mit_pct})
        )
    return out


def _fractal_extrema(
    bars: list[Bar], n: int, kind: Literal["low", "high"]
) -> list[tuple[int, float]]:
    """N-bar fractal swing lows/highs → list of ``(index, price)``.

    A swing low at index ``i`` requires the ``n`` bars on each side to have a
    strictly higher low (mirror for highs). Same definition the structure and
    fib engines use.
    """

    out: list[tuple[int, float]] = []
    for i in range(n, len(bars) - n):
        if kind == "low":
            low = float(bars[i].low)
            if all(float(bars[i - j].low) > low for j in range(1, n + 1)) and all(
                float(bars[i + j].low) > low for j in range(1, n + 1)
            ):
                out.append((i, low))
        else:
            h = float(bars[i].high)
            if all(float(bars[i - j].high) < h for j in range(1, n + 1)) and all(
                float(bars[i + j].high) < h for j in range(1, n + 1)
            ):
                out.append((i, h))
    return out


def _extend_zones_to_fractal_origin(
    zones: list[FVGZone],
    h1_bars: list[Bar],
    m5_bars: list[Bar],
    *,
    fractal_n: int,
    max_extension: float | None,
) -> list[FVGZone]:
    """Anchor each H1 demand/supply zone to the fractal that launched the impulse.

    The strategy author's zone methodology: the raw FVG gap (``b0.high..b2.low``)
    is a conservative bound, but the *true* demand/supply zone reaches the swing
    point the impulse originated from. For each H1 FVG, the origin window is the
    two H1 candles before the gap is confirmed (``b0``, ``b1``):

    * Bullish (demand): if the origin low is itself an H1 fractal swing below the
      FVG bottom → extend ``extended_bottom`` to it (``extension_tf='H1'``).
      Otherwise the H1 origin is "just a wick" (no fractal) → drop to M5, take the
      lowest M5 fractal swing low in the window, extend to it (``extension_tf='M5'``).
    * Bearish (supply): mirror — extend ``extended_top`` up to the origin fractal
      high (H1, else the highest M5 fractal high in the window).

    The extension only applies when it actually deepens the zone and stays within
    ``max_extension`` price units (``None`` = uncapped). Non-H1 zones pass through
    untouched.
    """

    if not h1_bars:
        return zones

    h1_time_idx = {b.time: i for i, b in enumerate(h1_bars)}
    h1_low_idx = {i for i, _ in _fractal_extrema(h1_bars, fractal_n, "low")}
    h1_high_idx = {i for i, _ in _fractal_extrema(h1_bars, fractal_n, "high")}

    out: list[FVGZone] = []
    for z in zones:
        if z.tf != "H1":
            out.append(z)
            continue
        b2_idx = h1_time_idx.get(z.created_at)
        if b2_idx is None or b2_idx < 2:
            out.append(z)
            continue
        i0, i1 = b2_idx - 2, b2_idx - 1
        b0, b1 = h1_bars[i0], h1_bars[i1]
        win_start, win_end = b0.time, h1_bars[b2_idx].time  # [b0, b1] candles
        m5_win = [b for b in m5_bars if win_start <= b.time < win_end]

        if z.type == FVGType.BULLISH:
            origin_idx = i0 if float(b0.low) <= float(b1.low) else i1
            new_bottom: float | None = None
            ext_tf: str | None = None
            if origin_idx in h1_low_idx and float(h1_bars[origin_idx].low) < z.bottom:
                new_bottom, ext_tf = float(h1_bars[origin_idx].low), "H1"
            else:
                m5_lows = [p for _, p in _fractal_extrema(m5_win, fractal_n, "low")]
                if m5_lows and min(m5_lows) < z.bottom:
                    new_bottom, ext_tf = min(m5_lows), "M5"
            if new_bottom is not None and (
                max_extension is None or (z.bottom - new_bottom) <= max_extension
            ):
                out.append(
                    z.model_copy(
                        update={"extended_bottom": new_bottom, "extension_tf": ext_tf}
                    )
                )
                continue

        elif z.type == FVGType.BEARISH:
            origin_idx = i0 if float(b0.high) >= float(b1.high) else i1
            new_top: float | None = None
            ext_tf = None
            if origin_idx in h1_high_idx and float(h1_bars[origin_idx].high) > z.top:
                new_top, ext_tf = float(h1_bars[origin_idx].high), "H1"
            else:
                m5_highs = [p for _, p in _fractal_extrema(m5_win, fractal_n, "high")]
                if m5_highs and max(m5_highs) > z.top:
                    new_top, ext_tf = max(m5_highs), "M5"
            if new_top is not None and (
                max_extension is None or (new_top - z.top) <= max_extension
            ):
                out.append(
                    z.model_copy(update={"extended_top": new_top, "extension_tf": ext_tf})
                )
                continue

        out.append(z)
    return out


def _rank(zones: list[FVGZone]) -> list[FVGZone]:
    """Composite rank: size × freshness × displacement, sorted desc."""

    out: list[FVGZone] = []
    for z in zones:
        # Freshness: inverse of age, capped at 1.
        freshness = max(0.0, 1.0 - (z.age_seconds / (24 * 3600)))
        score = z.size_points * (1.0 + z.displacement_atr) * (0.5 + 0.5 * freshness)
        if z.status == FVGStatus.MITIGATED:
            score *= 0.1  # dead zones are nearly worthless
        elif z.status == FVGStatus.PARTIALLY_MITIGATED:
            score *= 0.5
        out.append(z.model_copy(update={"rank_score": float(score)}))
    out.sort(key=lambda z: z.rank_score, reverse=True)
    return out


class FVGEngine:
    """Compute FVG zones for the configured timeframes."""

    def __init__(
        self,
        timeframes: tuple[Literal["H1", "M5", "M1"], ...] = ("H1", "M5", "M1"),
        *,
        extend_to_fractal: bool = True,
        extension_fractal_n: int = 2,
        extension_max_atr: float = 2.0,
    ) -> None:
        self._tfs = timeframes
        self._extend = extend_to_fractal
        self._ext_n = extension_fractal_n
        self._ext_max_atr = extension_max_atr

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> FVGOutput:
        bars_m1 = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)
        if not bars_m1:
            return FVGOutput(zones=[], top_zones=[])

        # ATR for displacement measurement. Use the full M1 series.
        df = bars_to_df(bars_m1)
        atr_val = compute_atr(df, period=14)

        all_zones: list[FVGZone] = []
        h1_bars: list[Bar] = []
        m5_bars: list[Bar] = []
        # Build higher-TF bars from M1.
        for tf in self._tfs:
            if tf == "M1":
                tf_bars = bars_m1
            elif tf == "M5":
                tf_bars = round_bars_by_time(bars_m1, 5)
                m5_bars = tf_bars
            elif tf == "H1":
                tf_bars = round_bars_by_time(bars_m1, 60)
                h1_bars = tf_bars
            else:
                continue
            if not tf_bars:
                continue
            raw_zones = _detect_fvgs_on_series(tf_bars, tf, atr_val, current_t)
            refined = _refine_mitigation(raw_zones, tf_bars)
            all_zones.extend(refined)

        # Anchor H1 zones to their impulse-origin fractal (H1, else drop to M5).
        # Needs both H1 and M5 series; skipped if either TF isn't configured.
        if self._extend and h1_bars and m5_bars:
            atr_h1 = compute_atr(bars_to_df(h1_bars), period=14)
            max_ext = (
                self._ext_max_atr * atr_h1
                if (self._ext_max_atr > 0 and atr_h1 and atr_h1 > 0)
                else None
            )
            all_zones = _extend_zones_to_fractal_origin(
                all_zones,
                h1_bars,
                m5_bars,
                fractal_n=self._ext_n,
                max_extension=max_ext,
            )

        ranked = _rank(all_zones)
        return FVGOutput(zones=ranked, top_zones=ranked[:3])
