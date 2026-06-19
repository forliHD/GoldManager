"""Spread monitor — rolling window, percentiles, outlier flags.

Tracks the live spread (in *points*) per symbol and produces:

* a rolling percentile summary (p50, p90, p95, p99)
* a flag for "spread is currently in the tail" (above warn threshold)
* a flag for "spread is at the block threshold" (above hard cap)

The :class:`SpreadMonitor` is the canonical source of ``spread_points``
for the :class:`xauusd_bot.connectors.safety.PreTradeSafetyChecker` and
for the paper broker's :meth:`PaperBroker.record_spread` input.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Deque

import structlog

from xauusd_bot.connectors.schemas import Bar, Tick

log = structlog.get_logger(__name__)


def _spread_points_from_bar(bar: Bar) -> float | None:
    """If a bar carries a ``spread`` field (price units), convert to points."""

    if bar.spread is None or bar.symbol is None:
        return None
    # Note: this method receives bars with a pre-computed price spread; we
    # need the symbol spec's point size to convert to points. The caller
    # supplies the point indirectly via ``_record(..., point=...)``.
    return None  # actual conversion done in update() with a known point


@dataclass
class SpreadSnapshot:
    """Point-in-time snapshot of the rolling spread stats."""

    p50: float
    p90: float
    p95: float
    p99: float
    current: float
    is_outlier: bool
    is_block: bool
    n: int


class SpreadMonitor:
    """Rolling-window spread monitor in *points*.

    Parameters
    ----------
    symbol:
        Symbol this monitor is tracking.
    point:
        :class:`SymbolSpec.point` value — the smallest price increment.
        Used to convert price-unit spread to points.
    window:
        Number of recent samples to keep in the rolling window.
    warn_percentile:
        Percentile (0..1) above which a sample is "elevated".
    block_percentile:
        Percentile (0..1) above which a sample is "block-worthy".
    warn_points:
        Optional absolute point threshold (e.g. spec.spread_max_warn_points).
        When set, takes precedence over ``warn_percentile``.
    block_points:
        Optional absolute point threshold. When set, takes precedence.
    """

    def __init__(
        self,
        symbol: str = "XAUUSD",
        point: Decimal = Decimal("0.01"),
        window: int = 2000,
        warn_percentile: float = 0.95,
        block_percentile: float = 0.99,
        warn_points: float | None = None,
        block_points: float | None = None,
    ) -> None:
        self._symbol = symbol
        self._point = point
        self._window: Deque[float] = deque(maxlen=window)
        self._warn_pct = warn_percentile
        self._block_pct = block_percentile
        self._warn_points = warn_points
        self._block_points = block_points
        self._last: float = 0.0

    @property
    def last(self) -> float:
        return self._last

    def update_from_bar(self, bar: Bar) -> None:
        """Update from a bar with an explicit ``spread`` (price units) field."""

        if bar.spread is None:
            return
        self._update(float(bar.spread) / float(self._point))

    def update_from_tick(self, tick: Tick) -> None:
        """Update from a tick (bid/ask spread in price units → points)."""

        if self._point == 0:
            return
        spread_price = float(tick.ask - tick.bid)
        self._update(spread_price / float(self._point))

    def update_from_points(self, points: float) -> None:
        """Update from a pre-computed spread (in points)."""

        self._update(float(points))

    def _update(self, points: float) -> None:
        points = max(0.0, float(points))
        self._last = points
        self._window.append(points)

    # --------------------------------------------------------------- outputs

    def snapshot(self) -> SpreadSnapshot:
        """Return a :class:`SpreadSnapshot` of the current rolling stats."""

        if not self._window:
            return SpreadSnapshot(0, 0, 0, 0, self._last, False, False, 0)
        samples = sorted(self._window)
        n = len(samples)

        def q(p: float) -> float:
            # Linear-interpolated percentile over the sorted window. For
            # n samples, the rank is p * (n - 1); we floor to an index and
            # interpolate the gap to the next sample.
            if n == 1:
                return samples[0]
            rank = p * (n - 1)
            lo = int(rank)
            hi = min(lo + 1, n - 1)
            frac = rank - lo
            return samples[lo] + (samples[hi] - samples[lo]) * frac

        # Absolute thresholds take precedence when configured.
        warn_threshold = self._warn_points if self._warn_points is not None else q(self._warn_pct)
        block_threshold = self._block_points if self._block_points is not None else q(self._block_pct)

        return SpreadSnapshot(
            p50=q(0.50),
            p90=q(0.90),
            p95=q(0.95),
            p99=q(0.99),
            current=self._last,
            is_outlier=self._last >= warn_threshold,
            is_block=self._last >= block_threshold,
            n=n,
        )

    def reset(self) -> None:
        self._window.clear()
        self._last = 0.0
