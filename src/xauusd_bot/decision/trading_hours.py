"""Trading-hours window — a deterministic time-of-day entry gate + weekend flat.

Joshua's rule: only OPEN new entries inside the tradeable-volume window —
from the **Asian-session open (00:00 UTC)** to **just before the New-York
close (22:55 UTC)**, after which there is no volume and a fresh entry makes
no sense. Outside the window the bot observes and manages open positions but
takes no new risk. A running trade MAY be held overnight, but a position is
**never carried over the weekend** — :meth:`TradingWindow.should_flatten_for_weekend`
forces a flat before the Friday close.

Timezone
--------
The window is expressed in a configurable IANA timezone (default **UTC**,
matching Joshua's "00:00 / 22:55 UTC"). A non-UTC tz (e.g. ``Europe/Berlin``)
is converted *per bar* via :mod:`zoneinfo` so DST self-adjusts. The default
UTC path is short-circuited — it needs no tz database, so the gate works even
in a slim container without ``tzdata``.

Broker time vs real UTC
-----------------------
The bar ``ts`` is **broker-server time** (MT5 labels it ``UTC`` but it is
e.g. UTC+3). :attr:`FeatureSnapshotBundle.broker_offset_minutes` carries
the offset the feature-engine detected at startup; we subtract it to
recover the true UTC instant before applying the window. In replay/backtest
the offset is 0 and ``ts`` is treated as already-UTC (the data's own frame).

Fail-open / fail-safe
---------------------
This is a live trading hot path. A bad/missing timezone or an unparseable
``HH:MM`` must never crash the loop: the entry gate **allows** the trade
(fail-open), and the weekend flat does **not** fire (fail-safe — never take a
destructive close on an uncertain clock). Both log a warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger(__name__)

# Stable block-reason string. Tests + the journal assert on this exact value.
REASON_OUTSIDE_TRADING_HOURS = "outside_trading_hours"
# Mon=0 … Sun=6. Friday is the last session before the weekend gap.
_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6


def _parse_hm(value: str) -> time | None:
    """Parse an ``'HH:MM'`` string into a :class:`datetime.time`. None on garbage."""

    try:
        hh, mm = value.strip().split(":")
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class TradingWindow:
    """A configurable-timezone entry window + weekend-flat rule.

    Construct from :class:`~xauusd_bot.common.config.Settings` via
    :meth:`from_settings`. Entry membership is tested with :meth:`allows`;
    the weekend flat with :meth:`should_flatten_for_weekend`.
    """

    enabled: bool
    tz_name: str
    start: time
    end: time
    weekend_flat_enabled: bool = True
    weekend_flat_cutoff: time = time(20, 55)

    @classmethod
    def from_settings(cls, settings: object) -> TradingWindow:
        """Build from the ``exec_trading_*`` settings, defensively.

        An unparseable start/end disables the window (fail-open) with a
        warning, so a typo in ``.env`` can never block every trade.
        """

        enabled = bool(getattr(settings, "exec_trading_window_enabled", False))
        tz_name = str(getattr(settings, "exec_trading_timezone", "UTC"))
        start = _parse_hm(str(getattr(settings, "exec_trading_start_local", "00:00")))
        end = _parse_hm(str(getattr(settings, "exec_trading_end_local", "22:55")))
        if enabled and (start is None or end is None):
            log.warning(
                "trading_window_bad_bounds_disabled",
                start=getattr(settings, "exec_trading_start_local", None),
                end=getattr(settings, "exec_trading_end_local", None),
            )
            enabled = False
        flat_cut = _parse_hm(str(getattr(settings, "exec_weekend_flat_utc", "20:55")))
        return cls(
            enabled=enabled,
            tz_name=tz_name,
            start=start or time(0, 0),
            end=end or time(0, 0),
            weekend_flat_enabled=bool(getattr(settings, "exec_weekend_flat_enabled", True)),
            weekend_flat_cutoff=flat_cut or time(20, 55),
        )

    # ----------------------------------------------------------------

    def local_time(self, ts: datetime, broker_offset_minutes: float = 0.0) -> datetime | None:
        """Convert a broker-time ``ts`` to window-timezone time. None on tz failure.

        ``ts`` is interpreted as a broker-server instant (tz-naive is treated as
        UTC-labelled). We subtract ``broker_offset_minutes`` to recover the true
        UTC instant, then project into ``tz_name``. The UTC default is
        short-circuited so it needs no tz database (works without ``tzdata``).
        """

        try:
            aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
            true_utc = aware - timedelta(minutes=float(broker_offset_minutes))
            if self.tz_name.upper() == "UTC":
                return true_utc.astimezone(UTC)
            return true_utc.astimezone(ZoneInfo(self.tz_name))
        except (ZoneInfoNotFoundError, ValueError, OverflowError, OSError) as exc:
            log.warning("trading_window_tz_lookup_failed", tz=self.tz_name, error=str(exc))
            return None

    def allows(self, ts: datetime, broker_offset_minutes: float = 0.0) -> bool:
        """True if a NEW entry may open at ``ts`` (broker time).

        Disabled window → always True. tz failure → fail-open (True).
        The window is half-open ``[start, end)``: ``start`` is included,
        ``end`` is excluded (no fresh entry at/after the no-volume cutoff).
        Supports overnight windows (``start > end``) too.
        """

        if not self.enabled:
            return True
        local = self.local_time(ts, broker_offset_minutes)
        if local is None:
            return True  # fail-open — never block the whole loop on a tz error
        now = local.time()
        if self.start <= self.end:
            return self.start <= now < self.end
        # Overnight window (e.g. 22:00–06:00): allowed at the ends, blocked mid-day.
        return now >= self.start or now < self.end

    def should_flatten_for_weekend(self, ts: datetime, broker_offset_minutes: float = 0.0) -> bool:
        """True if an open position must be CLOSED before the weekend gap.

        Evaluated in **UTC** — the weekend gap is a broker-clock event and the
        cutoff is named ``..._utc``, so this is independent of the entry window's
        display timezone. Fires Friday at/after ``weekend_flat_cutoff`` (flatten
        while there is still liquidity, before the broker's Friday close), all of
        Saturday, and Sunday **only before the cutoff** — so a position opened at
        the Sunday-evening reopen (which ``allows()`` permits) is not force-closed
        on its very first management bar. Disabled / clock failure → False
        (fail-safe: never force a close on an uncertain clock). Entry-window
        management is independent: a running trade may still be held overnight.
        """

        if not self.weekend_flat_enabled:
            return False
        try:
            aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
            utc = (aware - timedelta(minutes=float(broker_offset_minutes))).astimezone(UTC)
        except (ValueError, OverflowError, OSError, TypeError, AttributeError) as exc:
            log.warning("weekend_flat_clock_failed", error=str(exc))
            return False  # fail-safe — do not take a destructive action on a bad clock
        weekday = utc.weekday()
        now = utc.time()
        if weekday == _SATURDAY:
            return True  # market fully closed all day
        if weekday == _SUNDAY:
            # Closed until the Sunday-evening reopen (~22:00 UTC). Flatten a weekend
            # survivor before the cutoff, but don't force-close a freshly opened
            # position once the new session has started (consistent with allows()).
            return now < self.weekend_flat_cutoff
        return weekday == _FRIDAY and now >= self.weekend_flat_cutoff


REASON_WEEKEND_FLAT = "weekend_flat"

__all__ = ["TradingWindow", "REASON_OUTSIDE_TRADING_HOURS", "REASON_WEEKEND_FLAT"]
