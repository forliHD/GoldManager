"""ZoneRegistry — one entry per zone/setup, with the strategy's lifecycle.

Kills the failure mode the backtest surfaced: the rule engine fired 3 near-
identical entries within minutes into the same chop zone and ate 3 stops. The
strategy author's rule is *one entry per zone/setup*, with this lifecycle:

* **open**   — a position is live in the zone → no second entry there.
* **used**   — the position closed; a BE/scratch/TP exit does NOT kill the zone,
  but we don't immediately re-fire on the same touch.
* **armed**  — price has LEFT the zone band and may return → a fresh re-test is
  allowed (one position at a time).
* **dead**   — an **H1 close beyond the zone** (below a long/demand zone, above
  a short/supply zone) invalidates it permanently → no more entries there.

The registry is a pure, I/O-free state machine. The orchestrator (backtest or
live execution) computes the zone band, calls :meth:`can_enter` before opening,
:meth:`open` on fill, :meth:`close` on exit, :meth:`note_price` each bar, and
:meth:`on_h1_close` when an H1 bar closes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["long", "short"]
ZoneStatus = Literal["open", "used", "armed", "dead"]


@dataclass
class Zone:
    """One tracked zone band (a price range a setup was taken in)."""

    id: int
    side: Side
    low: float
    high: float
    status: ZoneStatus = "open"

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high


def band_from_price(price: float, atr: float | None, *, atr_mult: float = 0.5, min_half: float = 0.5) -> tuple[float, float]:
    """Default zone band when no explicit (LLM) entry zone is available.

    Half-width = ``max(atr_mult * ATR, min_half)`` around the entry price. Two
    entries within ~1 ATR of each other fall in the same band → the second is
    blocked while the first is open.
    """

    half = max(atr_mult * float(atr or 0.0), min_half)
    return price - half, price + half


class ZoneRegistry:
    """Track active zones and enforce one-entry-per-zone with the lifecycle."""

    def __init__(self, *, max_zones: int = 256) -> None:
        self._zones: dict[int, Zone] = {}
        self._next_id = 0
        # Live runs never call reset() (only the backtest does, per-run), so
        # without a bound the registry would grow one entry per trade forever
        # (memory) and ancient 'dead' bands would block new setups indefinitely
        # (strangling the tradeable range). Cap the registry and evict the
        # oldest non-'open' zones past the cap. The cap is far above any realistic
        # short window (backtest runs hold a handful of zones), so this never
        # changes near-term behaviour or the verified backtest — it only ages out
        # stale history on a long-lived live process. An 'open' zone (a live
        # position) is never evicted.
        self._max_zones = max_zones

    @property
    def zones(self) -> list[Zone]:
        return list(self._zones.values())

    # ---------------------------------------------------------------- gate
    def can_enter(self, side: Side, price: float) -> bool:
        """False if an open / used / dead zone of the same side contains ``price``.

        ``armed`` zones (price left and may be re-testing) do NOT block.
        """

        for z in self._zones.values():
            if z.side == side and z.contains(price) and z.status in ("open", "used", "dead"):
                return False
        return True

    # ---------------------------------------------------------------- lifecycle
    def open(self, side: Side, low: float, high: float) -> int:
        """Register an opened position's zone. Returns a zone id (track it on the position)."""

        # Absorb an overlapping armed zone (the re-test we just acted on) so it
        # doesn't linger and double-count.
        for z in list(self._zones.values()):
            if z.side == side and z.status == "armed" and not (high < z.low or low > z.high):
                del self._zones[z.id]
        zid = self._next_id
        self._next_id += 1
        self._zones[zid] = Zone(id=zid, side=side, low=low, high=high, status="open")
        self._prune()
        return zid

    def _prune(self) -> None:
        """Bound the registry: drop the oldest non-'open' zones past ``max_zones``."""

        if len(self._zones) <= self._max_zones:
            return
        # Oldest first (ids are monotonic = insertion order); never evict a live
        # 'open' zone.
        evictable = [zid for zid in sorted(self._zones) if self._zones[zid].status != "open"]
        for zid in evictable[: len(self._zones) - self._max_zones]:
            del self._zones[zid]

    def close(self, zone_id: int) -> None:
        """Mark the zone's position closed. A BE/scratch/TP exit keeps it (→ used)."""

        z = self._zones.get(zone_id)
        if z is not None and z.status == "open":
            z.status = "used"

    def note_price(self, price: float) -> None:
        """Per-bar: a 'used' zone re-arms once price has left its band (fresh re-test possible)."""

        for z in self._zones.values():
            if z.status == "used" and not z.contains(price):
                z.status = "armed"

    def on_h1_close(self, h1_close: float) -> None:
        """An H1 close beyond a zone invalidates it permanently (the zone is 'kaputt')."""

        for z in self._zones.values():
            if z.status == "dead":
                continue
            beyond = (h1_close < z.low) if z.side == "long" else (h1_close > z.high)
            if beyond:
                z.status = "dead"

    def reset(self) -> None:
        self._zones.clear()
        self._next_id = 0


__all__ = ["Zone", "ZoneRegistry", "band_from_price"]
