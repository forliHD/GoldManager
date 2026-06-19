"""OHLC bar aggregation from tick or M1 data.

The :class:`OHLCBuilder` builds higher-timeframe bars (M5, H1, ...) from
M1 bars (or from ticks, when available). The contract:

* All input bars / ticks must have ``time <= current_t`` (point-in-time).
* Output bars have ``time`` = bucket start, ``close_time`` = bucket end.
* Volume aggregation uses tick_volume unless real_volume is present.

The builder is **stateful** — it remembers the open bar of the current
incomplete bucket and the last fully closed bar. The bot calls
:meth:`on_bar` (or :meth:`on_tick`) for each new piece of data, then
:meth:`closed_bars` to drain finished bars.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

import structlog

from xauusd_bot.connectors.schemas import Bar, Tick

log = structlog.get_logger(__name__)

# Timeframe string → minutes. M1 / M5 / H1 / H4 / D1.
_TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
}


def _bucket_start(ts: datetime, minutes: int) -> datetime:
    """Floor ``ts`` to the start of its bucket of width ``minutes``."""

    ts = ts.astimezone(timezone.utc)
    epoch_min = int(ts.timestamp() // 60)
    bucket_min = epoch_min - (epoch_min % minutes)
    return datetime.fromtimestamp(bucket_min * 60, tz=timezone.utc)


@dataclass
class _OpenBar:
    symbol: str
    timeframe: str
    time: datetime  # bucket start
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = 0
    real_volume: int = 0
    has_real_volume: bool = False


class OHLCBuilder:
    """Aggregate bars from M1 bars (or ticks) into higher timeframes.

    Parameters
    ----------
    symbol:
        Symbol to track.
    source_timeframe:
        Source bar timeframe, e.g. ``"M1"``. The builder can also
        accept M1 ticks and will treat each tick as a price update.
    """

    def __init__(self, symbol: str = "XAUUSD", source_timeframe: str = "M1") -> None:
        self._symbol = symbol
        self._source_tf = source_timeframe
        # Per-target-timeframe: dict[bucket_start → _OpenBar] for the current
        # incomplete bar, plus a list of closed bars.
        self._open: dict[str, _OpenBar] = {}
        self._closed: dict[str, list[Bar]] = defaultdict(list)
        # Tick-to-M1 buffer for tick input
        self._tick_open_m1: _OpenBar | None = None

    @property
    def closed_bars_by_tf(self) -> dict[str, list[Bar]]:
        """All closed bars aggregated per target timeframe (read-only copy)."""

        return {tf: list(bars) for tf, bars in self._closed.items()}

    def closed_bars(self, timeframe: str) -> list[Bar]:
        """Closed bars for a single timeframe (oldest → newest)."""

        return list(self._closed.get(timeframe, []))

    def reset(self) -> None:
        self._open.clear()
        self._closed.clear()
        self._tick_open_m1 = None

    # ----------------------------------------------------------------- ingest

    def on_bar(self, bar: Bar) -> Iterable[Bar]:
        """Ingest a source bar. Returns any **closed** higher-TF bars it triggers.

        M1 source bars also produce M5/H1/D1 closings at their bucket boundaries.
        """

        if bar.symbol != self._symbol:
            raise ValueError(f"OHLCBuilder({self._symbol}) got bar for {bar.symbol}")

        if self._source_tf != "M1":
            raise NotImplementedError("Only M1 source is supported in this block; higher sources come later.")

        # Aggregate into each target timeframe (M5, H1, D1).
        closed_out: list[Bar] = []
        for tf, minutes in _TIMEFRAME_MINUTES.items():
            if tf == "M1":
                # M1 is the source — push as-is, mark closed.
                self._closed["M1"].append(bar)
                continue
            bucket = _bucket_start(bar.time, minutes)
            open_bar = self._open.get(tf)
            if open_bar is None or open_bar.time != bucket:
                # Roll: the previous open_bar (if any) gets closed.
                if open_bar is not None:
                    closed_bar = self._finalize(open_bar)
                    self._closed[tf].append(closed_bar)
                    closed_out.append(closed_bar)
                open_bar = _OpenBar(
                    symbol=self._symbol,
                    timeframe=tf,
                    time=bucket,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    tick_volume=bar.tick_volume,
                )
                if bar.real_volume is not None:
                    open_bar.real_volume = bar.real_volume
                    open_bar.has_real_volume = True
                self._open[tf] = open_bar
            else:
                # Extend in-progress bar.
                open_bar.high = max(open_bar.high, bar.high)
                open_bar.low = min(open_bar.low, bar.low)
                open_bar.close = bar.close
                open_bar.tick_volume += bar.tick_volume
                if bar.real_volume is not None:
                    open_bar.real_volume += bar.real_volume
                    open_bar.has_real_volume = True
        return closed_out

    def on_tick(self, tick: Tick) -> Iterable[Bar]:
        """Ingest a tick. Aggregates into a 1-minute bucket, then cascades.

        Returns any closed higher-TF bars triggered by the tick (same
        semantics as :meth:`on_bar`).
        """

        if tick.symbol != self._symbol:
            raise ValueError(f"OHLCBuilder({self._symbol}) got tick for {tick.symbol}")

        mid = (tick.bid + tick.ask) / 2
        bar_time = _bucket_start(tick.time, 1)
        if self._tick_open_m1 is None or self._tick_open_m1.time != bar_time:
            # Close previous M1 if any
            if self._tick_open_m1 is not None:
                closed = self._finalize(self._tick_open_m1)
                # Cascade into higher TFs via on_bar, which also handles
                # appending the M1 to self._closed["M1"]. Doing it here too
                # would double-append. (Live-Bug 2026-06-14 test-coverage.)
                yield from self.on_bar(closed)
            self._tick_open_m1 = _OpenBar(
                symbol=self._symbol,
                timeframe="M1",
                time=bar_time,
                open=mid,
                high=mid,
                low=mid,
                close=mid,
                tick_volume=1 + int(tick.volume),
            )
        else:
            ob = self._tick_open_m1
            ob.high = max(ob.high, mid)
            ob.low = min(ob.low, mid)
            ob.close = mid
            ob.tick_volume += 1 + int(tick.volume)
        # Live-Bug 2026-06-14 (test-coverage): the original implementation
        # had a stray `return` before the `yield` generator marker, which
        # aborted the function before any work happened — making on_tick
        # a no-op generator. Removed. The function is still a generator
        # (yield from inside the body) so the return type is preserved.

    # --------------------------------------------------------------- internals

    def _finalize(self, ob: _OpenBar) -> Bar:
        return Bar(
            symbol=ob.symbol,
            timeframe=ob.timeframe,
            time=ob.time,
            open=ob.open,
            high=ob.high,
            low=ob.low,
            close=ob.close,
            tick_volume=ob.tick_volume,
            real_volume=(ob.real_volume if ob.has_real_volume else None),
        )
