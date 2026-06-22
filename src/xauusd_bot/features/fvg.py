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


def _final_leg_base(
    swings: list[tuple[int, float]], *, kind: Literal["low", "high"], max_step: float
) -> float | None:
    """Base of the FINAL tight impulse leg from a chronological swing list.

    The strategy author anchors a demand/supply zone to the base of the *last*
    impulse leg — the run of "consistent rising lows" (demand) / "falling highs"
    (supply) that launched the move — NOT the deepest swing of a multi-leg run
    (which yields an absurdly large zone). Walk the swings backward from the most
    recent one:

    * Demand: keep stepping to an *older* swing low while it is LOWER than its
      successor (descending the staircase as we go back) AND the step stays within
      ``max_step``. The first big jump (> max_step) is a leg boundary → stop. The
      base is the lowest low of that contiguous, tight run.
    * Supply: mirror — older swing high must be HIGHER, step within ``max_step``.

    Returns the base price, or ``None`` if there are no swings.
    """

    if not swings:
        return None
    base = swings[-1][1]
    for i in range(len(swings) - 2, -1, -1):
        prev = swings[i][1]
        cur = swings[i + 1][1]
        if kind == "low":
            if prev < cur and (cur - prev) <= max_step:
                base = prev
            else:
                break
        else:  # high
            if prev > cur and (prev - cur) <= max_step:
                base = prev
            else:
                break
    return base


def _extend_zones_to_fractal_origin(
    zones: list[FVGZone],
    h1_bars: list[Bar],
    m1_bars: list[Bar],
    *,
    fractal_n: int,
    leg_step: float,
    max_extension: float | None,
) -> list[FVGZone]:
    """Anchor each H1 demand/supply zone to the base of its final impulse leg.

    The raw FVG gap (``b0.high..b2.low``) is a conservative bound; the *true* zone
    reaches the point the impulse launched from. But extending to the deepest swing
    of a multi-leg move builds a far-too-large zone (the author's explicit concern).
    Instead, for each H1 FVG we drop to **M1** — where "der ursprung in den m1
    kerzen" lives — over the impulse window (the two H1 candles before the gap plus
    the gap candle) and take the base of the **final tight higher-lows / lower-highs
    leg** (see :func:`_final_leg_base`). ``leg_step`` (price units, an H1-ATR
    fraction) is the max gap between consecutive staircase swings before a leg
    boundary is declared.

    * Bullish (demand): ``extended_bottom`` = the leg-base swing LOW, if below the
      FVG bottom.
    * Bearish (supply): ``extended_top`` = the leg-base swing HIGH, if above the
      FVG top.

    The extension only applies when it deepens the zone and stays within
    ``max_extension`` price units (a hard safety cap; ``None`` = uncapped). Non-H1
    zones pass through untouched.
    """

    if not h1_bars or not m1_bars:
        return zones

    h1_time_idx = {b.time: i for i, b in enumerate(h1_bars)}

    out: list[FVGZone] = []
    for z in zones:
        if z.tf != "H1":
            out.append(z)
            continue
        b2_idx = h1_time_idx.get(z.created_at)
        if b2_idx is None or b2_idx < 2:
            out.append(z)
            continue
        # Impulse window: b0 + b1 (the lead-in and the displacement candle), in M1.
        # The gap candle b2 is EXCLUDED — by then price has already broken away, so
        # its swing lows sit above the impulse and would corrupt the leg base.
        win_start = h1_bars[b2_idx - 2].time
        win_end = z.created_at  # = b2.time
        m1_win = [b for b in m1_bars if win_start <= b.time < win_end]
        if len(m1_win) < 2 * fractal_n + 1:
            out.append(z)
            continue

        if z.type == FVGType.BULLISH:
            lows = _fractal_extrema(m1_win, fractal_n, "low")
            base = _final_leg_base(lows, kind="low", max_step=leg_step)
            if (
                base is not None
                and base < z.bottom
                and (max_extension is None or (z.bottom - base) <= max_extension)
            ):
                out.append(
                    z.model_copy(update={"extended_bottom": base, "extension_tf": "M1"})
                )
                continue
        elif z.type == FVGType.BEARISH:
            highs = _fractal_extrema(m1_win, fractal_n, "high")
            base = _final_leg_base(highs, kind="high", max_step=leg_step)
            if (
                base is not None
                and base > z.top
                and (max_extension is None or (base - z.top) <= max_extension)
            ):
                out.append(
                    z.model_copy(update={"extended_top": base, "extension_tf": "M1"})
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
        leg_step_atr: float = 0.5,
    ) -> None:
        self._tfs = timeframes
        self._extend = extend_to_fractal
        self._ext_n = extension_fractal_n
        self._ext_max_atr = extension_max_atr
        self._leg_step_atr = leg_step_atr

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> FVGOutput:
        bars_m1 = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)
        if not bars_m1:
            return FVGOutput(zones=[], top_zones=[])

        # ATR for displacement measurement. Use the full M1 series.
        df = bars_to_df(bars_m1)
        atr_val = compute_atr(df, period=14)

        all_zones: list[FVGZone] = []
        h1_bars: list[Bar] = []
        # Build higher-TF bars from M1.
        for tf in self._tfs:
            if tf == "M1":
                tf_bars = bars_m1
            elif tf == "M5":
                tf_bars = round_bars_by_time(bars_m1, 5)
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

        # Anchor H1 zones to the base of their final impulse leg, drilling to M1
        # (the rising-lows staircase). leg_step / the hard cap keep the zone tight.
        if self._extend and h1_bars:
            atr_h1 = compute_atr(bars_to_df(h1_bars), period=14)
            atr_h1 = atr_h1 if (atr_h1 and atr_h1 > 0) else None
            max_ext = (
                self._ext_max_atr * atr_h1
                if (self._ext_max_atr > 0 and atr_h1 is not None)
                else None
            )
            # leg-step in price units; fall back to a sane points default if ATR is
            # unavailable (short history) so the staircase walk still bounds legs.
            leg_step = self._leg_step_atr * atr_h1 if atr_h1 is not None else 5.0
            all_zones = _extend_zones_to_fractal_origin(
                all_zones,
                h1_bars,
                bars_m1,
                fractal_n=self._ext_n,
                leg_step=leg_step,
                max_extension=max_ext,
            )

        ranked = _rank(all_zones)
        return FVGOutput(zones=ranked, top_zones=ranked[:3])
