"""Triple-VWAP engine — three anchored VWAPs (00:00, 07:00, 12:00 UTC).

A "Volume-Weighted Average Price" anchored to a calendar boundary is the
cumulative (typical_price × volume) ÷ cumulative(volume) over all bars
since the anchor. The triple setup gives us three reference levels that
each session can be traded against.

Anchors (UTC)
-------------
* utc00: 00:00 — Asia (or start of day)
* utc07: 07:00 — London open
* utc12: 12:00 — NY open (and London+NY overlap)

Rollover behaviour
------------------
The 00:00-anchor VWAP starts fresh at midnight UTC every day. The
07:00 and 12:00 anchors do the same. The plan §8 specifies "Vortags-VWAP
weiterführen bis Anker erreicht" — i.e. on Monday morning the 07:00 and
12:00 anchors haven't fired yet, so the 00:00 anchor's VWAP is the only
"live" level. Once 07:00 hits, the 07:00 anchor kicks in and the
00:00 anchor's role for the rest of the day is just "yesterday's 00:00
VWAP, frozen at 24:00 UTC". In this implementation, the *current* 00:00
anchor only has bars from 00:00 → current_t; yesterday's VWAP is not
continued into today (the spec calls for "Vortags-VWAP weiterführen"
only until the new anchor fires, which it does at 00:00). We implement
the simpler "anchored to the *most recent* occurrence of the anchor time,
strictly < current_t" model.

Tick-Volume as weight
---------------------
Per AGENTS.md §3 I-5: tick_volume is a *relative* signal. It is used
as a weight in the VWAP calculation, which is fine — that's the standard
VWAP definition. Consumers that want absolute volume (e.g. comparing
"is today a high-volume day?") must normalize to a 30-day percentile.
We expose the raw VWAP value; the percentile/distance normalization
is computed by the engine for downstream consumers.

PIT guarantee
-------------
All bars consumed have ``time <= current_t``. The anchor for each level
is the most recent occurrence of (00:00 / 07:00 / 12:00) UTC strictly
before ``current_t``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import structlog

from xauusd_bot.common.schemas.features import (
    TripleVWAPOutput,
    VWAPLevel,
    VWAPLevelOutput,
)
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df, percentile_rank

log = structlog.get_logger(__name__)


_ANCHORS: tuple[tuple[VWAPLevel, time], ...] = (
    (VWAPLevel.UTC00, time(0, 0)),
    (VWAPLevel.UTC07, time(7, 0)),
    (VWAPLevel.UTC12, time(12, 0)),
)

_CLUSTER_ATR = 1.5  # how tight the 3 VWAPs must be (in ATR) to call it a cluster
_DISTANCE_WINDOW_30D = 30 * 24 * 60  # minutes (30 days × 24h × 60min) — for percentile lookback


def _last_anchor_before(ts: datetime, anchor_time: time) -> datetime | None:
    """The most recent occurrence of ``anchor_time`` on the *same* UTC day.

    Returns ``None`` if the anchor has not fired yet *today* (i.e. ``ts``
    is before today's ``anchor_time``). The "Vortags-VWAP" carry-forward
    rule from Plan §8 only applies to the 00:00 anchor: yesterday's
    00:00-VWAP lives until today's 00:00, then a new one starts. The
    07:00 and 12:00 anchors are day-local: if they haven't fired today
    yet, they have no state.
    """

    ts_utc = ts.astimezone(UTC)
    candidate = ts_utc.replace(hour=anchor_time.hour, minute=anchor_time.minute, second=0, microsecond=0)
    if candidate >= ts_utc:
        # Anchor hasn't fired today yet → no state.
        return None
    return candidate


def _previous_day_anchor(ts: datetime, anchor_time: time) -> datetime:
    """Same time on the *previous* UTC day, used for the 00:00 carry-forward."""

    ts_utc = ts.astimezone(UTC)
    today = ts_utc.replace(hour=anchor_time.hour, minute=anchor_time.minute, second=0, microsecond=0)
    return today - timedelta(days=1)


@dataclass
class _VwapState:
    """Internal accumulator for one VWAP level."""

    level: VWAPLevel
    anchor_ts: datetime
    sum_pv: float = 0.0  # sum of (typical_price × volume)
    sum_v: float = 0.0  # sum of volumes
    n_bars: int = 0
    # Distance history (one entry per bar): the close - vwap distance at
    # the time the bar was first seen. Used for the 30-day percentile.
    distance_history: list[float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.distance_history is None:
            self.distance_history = []

    @property
    def value(self) -> float | None:
        if self.sum_v <= 0:
            return None
        return self.sum_pv / self.sum_v


def _typical_price(bar: Bar) -> float:
    """(H + L + C) / 3 — standard VWAP input."""

    return (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0


class TripleVWAPEngine:
    """Compute the three anchored VWAPs for a given ``current_t``.

    The engine is **stateless** between calls — you pass in the visible
    bars and the cursor, and it returns the snapshot. This makes it
    trivially PIT-safe (a call at ``t1`` cannot leak data from a future
    ``t2 > t1``).
    """

    def __init__(self, cluster_atr: float = _CLUSTER_ATR, clock_offset_minutes: float = 0.0) -> None:
        self._cluster_atr = cluster_atr
        # Broker→UTC offset (minutes), subtracted from incoming times so the
        # 00:00/07:00/12:00 anchors fire at real-UTC session opens (MT5 bar
        # times are broker-server time, e.g. UTC+3). 0 for replay/tests; set
        # live via :meth:`set_clock_offset` (mirrors NewsContextEngine).
        self._offset_min = float(clock_offset_minutes)

    def set_clock_offset(self, minutes: float) -> None:
        """Set the broker→UTC offset (minutes) applied to incoming times."""
        self._offset_min = float(minutes)

    def compute(self, bars: Iterable[Bar], current_t: datetime) -> TripleVWAPOutput:
        bars = sorted([b for b in bars if b.time <= current_t], key=lambda b: b.time)

        # Each level has a "primary" anchor (today's occurrence, if it has
        # fired) and, for the 00:00 level, an optional "carry-forward" from
        # yesterday. See Plan §8: the 00:00-VWAP is the cumulative VWAP
        # that started at 00:00 today, but on Monday morning before 00:00
        # there's no today-VWAP yet — there *is* a yesterday-VWAP (from
        # Sunday 00:00) but it stopped at Monday 00:00, so on Monday
        # morning there is *no* 00:00-VWAP yet. The 07:00 and 12:00 levels
        # are day-local: they only have state once the anchor has fired
        # *today*.
        # Compute anchors in real UTC (subtract the broker offset), then shift
        # them back into the broker frame so they compare against native bar
        # times below. MT5 bar times are broker-server time (e.g. UTC+3).
        off = timedelta(minutes=self._offset_min)
        ct = current_t - off
        anchors: dict[VWAPLevel, datetime | None] = {
            lvl: _last_anchor_before(ct, t) for lvl, t in _ANCHORS
        }
        # If today's 00:00 hasn't fired, fall back to yesterday's 00:00.
        # This implements the "Vortags-VWAP weiterführen" rule from Plan §8.
        if anchors[VWAPLevel.UTC00] is None:
            anchors[VWAPLevel.UTC00] = _previous_day_anchor(ct, _ANCHORS[0][1])
        if self._offset_min:
            anchors = {lvl: (a + off if a is not None else None) for lvl, a in anchors.items()}

        states: dict[VWAPLevel, _VwapState] = {}
        for lvl, anchor_ts in anchors.items():
            if anchor_ts is None:
                # Engine still emits a row, but with 0 bars and value=None.
                states[lvl] = _VwapState(level=lvl, anchor_ts=current_t)
                states[lvl].n_bars = 0
            else:
                states[lvl] = _VwapState(level=lvl, anchor_ts=anchor_ts)

        # Per-level distance-history buffer (in a 30-day window, expressed
        # as "minutes ago" cap of _DISTANCE_WINDOW_30D).
        for bar in bars:
            tp = _typical_price(bar)
            v = max(1, int(bar.tick_volume))  # never weight by 0
            for state in states.values():
                if bar.time >= state.anchor_ts:
                    state.sum_pv += tp * v
                    state.sum_v += v
                    state.n_bars += 1
                    if state.value is not None:
                        state.distance_history.append(float(bar.close) - state.value)

        # Trim history to the last 30 days of bars (rough cap: keep last
        # ~30k M1 bars). For 30 days × 1440 M1/day = 43_200 bars; the
        # exact cap matters less than "not unbounded".
        for state in states.values():
            if len(state.distance_history) > 50_000:
                state.distance_history = state.distance_history[-50_000:]

        # ATR for distance normalization.
        df = bars_to_df(bars)
        atr_val = compute_atr(df, period=14)

        # Per-level outputs.
        levels: dict[str, VWAPLevelOutput] = {}
        vwap_values: list[float] = []
        for level_key, state in states.items():
            v = state.value

            # Cross / reclaim / loss: derived purely from distance history
            # (no ATR needed). We compute these for every level that has
            # at least 2 distance samples, even if ATR is unavailable.
            cross_up = False
            cross_down = False
            reclaim = False
            loss = False
            if v is not None and len(state.distance_history) >= 2:
                prev_dist = state.distance_history[-2]
                cur_dist = state.distance_history[-1]
                if prev_dist <= 0 < cur_dist:
                    cross_up = True
                elif prev_dist >= 0 > cur_dist:
                    cross_down = True
                if cur_dist > 0 and any(d < 0 for d in state.distance_history[-5:-1]):
                    reclaim = True
                if cur_dist < 0 and any(d > 0 for d in state.distance_history[-5:-1]):
                    loss = True

            # Distance-based metrics need ATR. If ATR isn't ready (e.g.
            # fewer than 14 bars), we still emit value + cross/reclaim/loss
            # but leave distance_atr / percentile_30d as None.
            if v is not None and atr_val is not None and atr_val > 0:
                last_close = float(bars[-1].close)
                distance_points = last_close - v
                distance_atr = distance_points / atr_val
                if state.distance_history:
                    pct = percentile_rank(
                        _hist_series(state.distance_history),
                        distance_points,
                    )
                else:
                    pct = 50.0
                levels[level_key.value] = VWAPLevelOutput(
                    level=state.level,
                    value=v,
                    distance_points=distance_points,
                    distance_atr=distance_atr,
                    distance_percentile_30d=pct,
                    cross_up=cross_up,
                    cross_down=cross_down,
                    reclaim=reclaim,
                    loss=loss,
                    n_bars_anchored=state.n_bars,
                )
                vwap_values.append(v)
            elif v is not None:
                last_close = float(bars[-1].close)
                levels[level_key.value] = VWAPLevelOutput(
                    level=state.level,
                    value=v,
                    distance_points=last_close - v,
                    distance_atr=None,
                    distance_percentile_30d=None,
                    cross_up=cross_up,
                    cross_down=cross_down,
                    reclaim=reclaim,
                    loss=loss,
                    n_bars_anchored=state.n_bars,
                )
                vwap_values.append(v)
            else:
                levels[level_key.value] = VWAPLevelOutput(
                    level=state.level,
                    value=None,
                    distance_points=None,
                    distance_atr=None,
                    distance_percentile_30d=None,
                    cross_up=False,
                    cross_down=False,
                    reclaim=False,
                    loss=False,
                    n_bars_anchored=state.n_bars,
                )

        # Cluster: all three VWAPs within 1.5*ATR of each other.
        is_cluster = False
        cluster_center: float | None = None
        if len(vwap_values) == 3 and atr_val and atr_val > 0:
            spread = max(vwap_values) - min(vwap_values)
            is_cluster = spread <= self._cluster_atr * atr_val
            cluster_center = sum(vwap_values) / 3.0

        return TripleVWAPOutput(
            levels=levels,
            cluster_within_atr=self._cluster_atr,
            is_cluster=is_cluster,
            cluster_center=cluster_center,
        )


def _hist_series(history: list[float]):
    """Convert a distance-history list to a pandas Series for percentile_rank."""
    import pandas as pd

    return pd.Series(history)
