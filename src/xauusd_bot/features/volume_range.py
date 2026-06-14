"""Fixed-Range Volume Profiles — Yearly / Monthly / Weekly, locked vs developing.

This is the **most critical** feature engine in the bot. Per
``00_FINAL_PLAN.md`` §4 (Joshua's correction):

* Each profile is anchored to a **fixed calendar boundary** (Yearly,
  Monthly, Weekly), not a rolling "since X" range.
* A profile is in one of two states:
    * **locked** — the period is complete (``period_end < now``). VAH,
      VPOC, VAL are *final* and **never change** after that.
    * **developing** — the period is in progress. Levels are
      *recomputed on every new M1 close* (the "wandering" behaviour).
* On rollover (period boundary crossing): freeze the developing profile
  to locked, persist it, start a new developing profile.

Volume distribution inside an M1 bar
------------------------------------
We support four policies (Plan §4.3, preference order
``tick_based > ohlc_weighted > uniform_hl > close_only``):

* ``tick_based`` — if a tick feed is available, use it (we don't have one
  in block 2, so this falls through to ``ohlc_weighted`` in tests).
* ``ohlc_weighted`` — split the M1 volume 50/25/25 between body (open
  → close) and the two wicks. This is the "neutral" choice.
* ``uniform_hl`` — split the volume evenly between every price bin the
  bar touched. This is the default and the most common in retail tools.
* ``close_only`` — assign 100 % of the volume to the close price. Crude
  but stable.

The default config uses ``uniform_hl`` (matches the Plan's "startwert").
A future "backtest vs live" comparison can sweep these.

Bins
----
Bin sizes default to the Plan's start values (Weekly 0.5–1.0,
Monthly 1.0–2.0, Yearly 2.0–5.0 gold points). We pick the midpoint of
the range and let the user override per profile.

Value Area
----------
The 70 % default means: starting at the VPOC (the bin with the highest
volume), expand outward to the nearest bins until the cumulative volume
is ≥ 70 % of the total. The boundary bins are the Value Area High
(VAH) and Value Area Low (VAL). 68 % and 75 % are kept as backtest
variants (configurable).

Point-in-Time (mandatory)
-------------------------
The engine uses only bars with ``time <= current_t``. A bar that
*opened* before ``current_t`` but hasn't *closed* yet is excluded —
its volume isn't real until the close. This is verified by an explicit
regression test (``test_look_ahead_freedom``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

import structlog

from xauusd_bot.common.schemas.features import (
    ValueAreaStatus,
    VolumeProfileName,
    VolumeProfileOutput,
    VolumeProfileState,
    VolumeRangeOutput,
)
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- distribution


class VolumeDistribution(str, Enum):
    """How to distribute M1 volume across price bins."""

    TICK_BASED = "tick_based"
    OHLC_WEIGHTED = "ohlc_weighted"
    UNIFORM_HL = "uniform_hl"
    CLOSE_ONLY = "close_only"


def _distribute_uniform_hl(bar: Bar, n_bins: int) -> list[tuple[float, float]]:
    """Distribute the bar's tick_volume evenly across the (low, high) range.

    Returns a list of ``(price, weight)`` tuples. Weights sum to 1.0;
    the caller multiplies by ``bar.tick_volume`` to get the per-bin
    volume.
    """

    if n_bins <= 0:
        return []
    lo = float(bar.low)
    hi = float(bar.high)
    if hi <= lo:
        # Degenerate bar (high == low): put all weight on close.
        return [(float(bar.close), 1.0)]
    step = (hi - lo) / n_bins
    return [(lo + step * (i + 0.5), 1.0 / n_bins) for i in range(n_bins)]


def _distribute_ohlc_weighted(bar: Bar, n_bins: int) -> list[tuple[float, float]]:
    """50 % to the body (open → close), 25 % to the upper wick, 25 % to the lower wick.

    Each wick is split evenly across its bins.
    """

    o = float(bar.open)
    c = float(bar.close)
    h = float(bar.high)
    low = float(bar.low)

    # 50 % to the body. Treat the body as a sequence of n_bins evenly
    # distributed between o and c.
    body: list[tuple[float, float]] = []
    if c > o:
        step = (c - o) / n_bins
        body = [(o + step * (i + 0.5), 0.5 / n_bins) for i in range(n_bins)]
    elif c < o:
        step = (o - c) / n_bins
        body = [(c + step * (i + 0.5), 0.5 / n_bins) for i in range(n_bins)]
    else:
        # Doji: split 0.5 over the full bar range.
        body = _distribute_uniform_hl(bar, n_bins)
        for i, _ in enumerate(body):
            body[i] = (body[i][0], body[i][1] * 0.5)

    # 25 % to upper wick (high → max(o, c))
    upper_wick_top = h
    upper_wick_bottom = max(o, c)
    up_wick: list[tuple[float, float]] = []
    if upper_wick_top > upper_wick_bottom:
        step = (upper_wick_top - upper_wick_bottom) / max(1, n_bins // 4)
        up_wick = [
            (upper_wick_bottom + step * (i + 0.5), 0.25 / max(1, n_bins // 4))
            for i in range(max(1, n_bins // 4))
        ]
    else:
        up_wick = []

    # 25 % to lower wick
    lower_wick_top = min(o, c)
    lower_wick_bottom = low
    low_wick: list[tuple[float, float]] = []
    if lower_wick_top > lower_wick_bottom:
        step = (lower_wick_top - lower_wick_bottom) / max(1, n_bins // 4)
        low_wick = [
            (lower_wick_bottom + step * (i + 0.5), 0.25 / max(1, n_bins // 4))
            for i in range(max(1, n_bins // 4))
        ]
    else:
        low_wick = []

    return body + up_wick + low_wick


def _distribute_close_only(bar: Bar, _n_bins: int) -> list[tuple[float, float]]:
    """100 % of the volume at the close price."""

    return [(float(bar.close), 1.0)]


_DIST_FUNCS: dict[VolumeDistribution, Callable[[Bar, int], list[tuple[float, float]]]] = {
    VolumeDistribution.UNIFORM_HL: _distribute_uniform_hl,
    VolumeDistribution.OHLC_WEIGHTED: _distribute_ohlc_weighted,
    VolumeDistribution.CLOSE_ONLY: _distribute_close_only,
    # tick_based falls back to OHLC-weighted in the absence of a real
    # tick feed. Future block will swap in a real tick-driven distributor.
    VolumeDistribution.TICK_BASED: _distribute_ohlc_weighted,
}


# ---------------------------------------------------------------- period math


def _weekly_bounds(ts: datetime) -> tuple[datetime, datetime]:
    """ISO week: Mon 00:00 UTC → next Mon 00:00 UTC (exclusive)."""

    ts_utc = ts.astimezone(UTC)
    monday = ts_utc - timedelta(days=ts_utc.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, monday + timedelta(days=7)


def _monthly_bounds(ts: datetime) -> tuple[datetime, datetime]:
    """1st 00:00 UTC → 1st of next month 00:00 UTC (exclusive)."""

    ts_utc = ts.astimezone(UTC)
    start = ts_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month


def _yearly_bounds(ts: datetime) -> tuple[datetime, datetime]:
    """Jan 1 00:00 UTC → next Jan 1 00:00 UTC (exclusive)."""

    ts_utc = ts.astimezone(UTC)
    start = ts_utc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, start.replace(year=start.year + 1)


_PERIOD_FUNCS: dict[VolumeProfileName, Callable[[datetime], tuple[datetime, datetime]]] = {
    VolumeProfileName.WEEKLY: _weekly_bounds,
    VolumeProfileName.MONTHLY: _monthly_bounds,
    VolumeProfileName.YEARLY: _yearly_bounds,
}

_DEFAULT_BIN_SIZES: dict[VolumeProfileName, float] = {
    VolumeProfileName.WEEKLY: 0.75,   # midpoint of 0.5–1.0
    VolumeProfileName.MONTHLY: 1.5,   # midpoint of 1.0–2.0
    VolumeProfileName.YEARLY: 3.5,    # midpoint of 2.0–5.0
}


# ---------------------------------------------------------------- profile accumulator


@dataclass
class _ProfileAccumulator:
    """Builds a volume histogram for one (profile, period)."""

    name: VolumeProfileName
    period_start: datetime
    period_end: datetime
    bin_size: float
    dist: VolumeDistribution
    bins: dict[float, float] = field(default_factory=dict)
    n_bars: int = 0
    sum_volume: float = 0.0
    last_close: float | None = None
    last_high: float | None = None
    last_low: float | None = None
    locked: bool = False

    def price_to_bin(self, price: float) -> float:
        """Snap a price to its bin center (deterministic)."""

        # Use round-to-nearest so bins are aligned at multiples of bin_size
        # offset by bin_size/2 (so 0 sits in the middle of a bin, not at
        # an edge). This keeps the bin centers stable across periods.
        return round(price / self.bin_size) * self.bin_size

    def add_bar(self, bar: Bar) -> None:
        # Note: locking happens *after* building the previous profile.
        # The dispatch loop in the engine is responsible for ordering.
        # We just distribute the bar's volume across price bins.
        n_bins = max(1, int(round((float(bar.high) - float(bar.low)) / self.bin_size)) + 1)
        for price, weight in _DIST_FUNCS[self.dist](bar, n_bins):
            bin_center = self.price_to_bin(price)
            self.bins[bin_center] = self.bins.get(bin_center, 0.0) + float(bar.tick_volume) * weight
        self.n_bars += 1
        self.sum_volume += float(bar.tick_volume)
        self.last_close = float(bar.close)
        self.last_high = float(bar.high)
        self.last_low = float(bar.low)

    def freeze(self) -> None:
        self.locked = True

    # ----- derived levels -----

    def vah_vpoc_val(self, value_area_pct: float) -> tuple[float, float, float]:
        """Return (VAH, VPOC, VAL) for the given value-area share.

        Walks outward from the VPOC (highest-volume bin) in both
        directions, accumulating volume until the value-area share is
        reached. Ties are broken by choosing the bin closer to the
        current close (or the higher one, if there's no close yet).
        """

        if not self.bins:
            return (float("nan"), float("nan"), float("nan"))
        total = sum(self.bins.values())
        if total <= 0:
            return (float("nan"), float("nan"), float("nan"))
        target = total * value_area_pct

        # VPOC = bin with the highest volume.
        vpoc = max(self.bins.items(), key=lambda kv: kv[1])[0]
        # Sort bins and walk outward from the VPOC.
        sorted_bins = sorted(self.bins.items(), key=lambda kv: kv[0])
        vpoc_idx = next(i for i, (p, _) in enumerate(sorted_bins) if p == vpoc)
        cum = sorted_bins[vpoc_idx][1]
        left = vpoc_idx - 1
        right = vpoc_idx + 1
        while cum < target and (left >= 0 or right < len(sorted_bins)):
            lv = sorted_bins[left][1] if left >= 0 else -1.0
            rv = sorted_bins[right][1] if right < len(sorted_bins) else -1.0
            if rv >= lv:
                cum += rv
                right += 1
            else:
                cum += lv
                left -= 1
        val = sorted_bins[max(0, left + 1)][0]
        vah = sorted_bins[min(len(sorted_bins) - 1, right - 1)][0]
        return (vah, vpoc, val)


# ---------------------------------------------------------------- engine


class FixedVolumeRangeEngine:
    """Compute the three (Yearly/Monthly/Weekly) volume profiles.

    The engine is **stateless** between calls — pass the visible bars
    and the current cursor, get back a :class:`VolumeRangeOutput` that
    includes the developing levels + the locked previous-period levels.
    """

    def __init__(
        self,
        bin_sizes: dict[VolumeProfileName, float] | None = None,
        value_area_pct: float = 0.70,
        distribution: VolumeDistribution = VolumeDistribution.UNIFORM_HL,
    ) -> None:
        self._bin_sizes = {**_DEFAULT_BIN_SIZES, **(bin_sizes or {})}
        self._value_area_pct = value_area_pct
        self._distribution = distribution

    def compute(
        self,
        bars: Iterable[Bar],
        current_t: datetime,
        atr_value: float | None = None,
    ) -> VolumeRangeOutput:
        # PIT-filter defensively (caller should already do this).
        bars = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)

        # Build accumulators for the current developing period of each profile,
        # plus accumulators for the *previous* locked period (where the data
        # exists in our visible bars).
        developing: dict[VolumeProfileName, _ProfileAccumulator] = {}
        previous: dict[VolumeProfileName, _ProfileAccumulator] = {}

        for name, period_fn in _PERIOD_FUNCS.items():
            d_start, d_end = period_fn(current_t)
            developing[name] = _ProfileAccumulator(
                name=name,
                period_start=d_start,
                period_end=d_end,
                bin_size=self._bin_sizes[name],
                dist=self._distribution,
            )
            # The "previous" period is the one immediately before the
            # *developing* one, i.e. [d_start - period_length, d_start).
            # We compute it by asking period_fn at d_start - 1ns and
            # using the resulting [start, d_start) window. So for a
            # weekly current week Mon→Mon, prev_week is the prior
            # Mon→Mon (i.e. the week that ends at d_start).
            p_start, p_end = period_fn(d_start - timedelta(microseconds=1))
            # p_end should equal d_start. If period_fn gives us a different
            # range, we re-align to d_start.
            if p_end != d_start:
                p_end = d_start
            prev = _ProfileAccumulator(
                name=name,
                period_start=p_start,
                period_end=p_end,
                bin_size=self._bin_sizes[name],
                dist=self._distribution,
            )
            prev.freeze()  # we treat previous as locked by definition
            previous[name] = prev

        # Dispatch each bar to the right accumulators.
        for bar in bars:
            ts = bar.time
            for name in _PERIOD_FUNCS:
                d_start, d_end = developing[name].period_start, developing[name].period_end
                p_start, p_end = previous[name].period_start, previous[name].period_end
                if p_start <= ts < p_end:
                    previous[name].add_bar(bar)
                if d_start <= ts < d_end:
                    developing[name].add_bar(bar)

        # ATR for distance normalization (if not provided).
        if atr_value is None:
            df = bars_to_df(bars)
            atr_value = compute_atr(df, period=14)

        # Build output schemas.
        def _to_output(profile: _ProfileAccumulator, is_locked: bool) -> VolumeProfileOutput:
            vah, vpoc, val = profile.vah_vpoc_val(self._value_area_pct)
            n_bars = profile.n_bars
            state = (
                VolumeProfileState.LOCKED
                if is_locked
                else (VolumeProfileState.DEVELOPING if n_bars > 0 else VolumeProfileState.EMPTY)
            )

            distance_to_vah_points = None
            distance_to_vah_atr = None
            distance_to_val_points = None
            distance_to_val_atr = None
            distance_to_vpoc_points = None
            distance_to_vpoc_atr = None
            value_status: ValueAreaStatus | None = None
            acceptance_count = 0
            rejection_count = 0
            rotation = False
            breakout = False

            last_close = profile.last_close
            if last_close is not None and vah == vah:  # not NaN
                distance_to_vah_points = last_close - vah
                distance_to_val_points = last_close - val
                distance_to_vpoc_points = last_close - vpoc
                if atr_value and atr_value > 0:
                    distance_to_vah_atr = distance_to_vah_points / atr_value
                    distance_to_val_atr = distance_to_val_points / atr_value
                    distance_to_vpoc_atr = distance_to_vpoc_points / atr_value

                if last_close > vah:
                    value_status = ValueAreaStatus.ABOVE_VALUE
                elif last_close < val:
                    value_status = ValueAreaStatus.BELOW_VALUE
                else:
                    value_status = ValueAreaStatus.WITHIN_VALUE

                # Acceptance vs rejection: count "closes inside the value
                # area" vs "closes outside" across the period.
                for b in bars:
                    if not (profile.period_start <= b.time < profile.period_end):
                        continue
                    bc = float(b.close)
                    if val <= bc <= vah:
                        acceptance_count += 1
                    else:
                        rejection_count += 1
                # Rotation: many acceptance + some rejection (price oscillated
                # through the value area). Breakout: few acceptance, many
                # rejection (price migrated outside and stayed).
                total_closes = acceptance_count + rejection_count
                if total_closes > 0:
                    rotation = acceptance_count >= total_closes * 0.4 and rejection_count >= 1
                    breakout = acceptance_count == 0 and rejection_count >= 3

            return VolumeProfileOutput(
                name=profile.name,
                state=state,
                period_start=profile.period_start,
                period_end=profile.period_end,
                bin_size=profile.bin_size,
                vah=vah if vah == vah else None,  # NaN-safe
                vpoc=vpoc if vpoc == vpoc else None,
                val=val if val == val else None,
                value_area_pct=self._value_area_pct,
                distance_to_vah_points=distance_to_vah_points,
                distance_to_vah_atr=distance_to_vah_atr,
                distance_to_val_points=distance_to_val_points,
                distance_to_val_atr=distance_to_val_atr,
                distance_to_vpoc_points=distance_to_vpoc_points,
                distance_to_vpoc_atr=distance_to_vpoc_atr,
                value_status=value_status,
                acceptance_count=acceptance_count,
                rejection_count=rejection_count,
                rotation=rotation,
                breakout=breakout,
                n_bars=n_bars,
            )

        out_weekly = _to_output(developing[VolumeProfileName.WEEKLY], is_locked=False)
        out_monthly = _to_output(developing[VolumeProfileName.MONTHLY], is_locked=False)
        out_yearly = _to_output(developing[VolumeProfileName.YEARLY], is_locked=False)
        out_prev_week = _to_output(previous[VolumeProfileName.WEEKLY], is_locked=True)
        out_prev_month = _to_output(previous[VolumeProfileName.MONTHLY], is_locked=True)
        out_prev_year = _to_output(previous[VolumeProfileName.YEARLY], is_locked=True)

        # If any of the "previous" profiles are EMPTY (no bars in that
        # period were visible), we omit them. (e.g. on day 1 of a new
        # year, prev_year has no bars yet.)
        prev_week: VolumeProfileOutput | None = out_prev_week if out_prev_week.n_bars > 0 else None
        prev_month: VolumeProfileOutput | None = out_prev_month if out_prev_month.n_bars > 0 else None
        prev_year: VolumeProfileOutput | None = out_prev_year if out_prev_year.n_bars > 0 else None

        return VolumeRangeOutput(
            weekly=out_weekly,
            monthly=out_monthly,
            yearly=out_yearly,
            prev_week=prev_week,
            prev_month=prev_month,
            prev_year=prev_year,
            cluster_within_atr=0.5,
            developing_vs_locked_clusters={},
        )
