"""News / Macro context engine.

Per Plan §8 and AGENTS.md §8.2: the calendar is config-driven, the
default is a ``stub`` provider for backtest / dev. Production wires
to TradingEconomics or FXStreet (Block 6+, not in scope for Block 2).

Output
------
* ``minutes_until_next_high_impact`` — countdown to the next
  high-impact event (None if none in the next 24h).
* ``in_blackout_flag`` — True when ``current_t`` is within the
  pre- or post-news blackout window of a high-impact event.
* ``next_high_impact`` — the next :class:`NewsEvent` (or None).
* ``upcoming_events`` — all known events in the next ``lookahead_hours``.
* ``surprise_score`` — placeholder (0.0 until live data lands).

Blackout window (configurable, defaults from Plan §8)
-----------------------------------------------------
* Pre-news: 15 minutes before the event.
* Post-news: 5 minutes (configurable 5–15 min) after the event.
* Net: ``event.ts - 15min <= current_t <= event.ts + post_minutes``
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta

import structlog

from xauusd_bot.common.schemas.features import (
    NewsContextOutput,
    NewsEvent,
    NewsImpact,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- providers


class NewsProviderClient(ABC):
    """Pluggable news-calendar provider interface."""

    @abstractmethod
    def fetch(self, current_t: datetime, lookahead_hours: int) -> list[NewsEvent]:
        """Return news events from ``current_t`` to ``current_t + lookahead_hours``."""


class StubNewsProvider(NewsProviderClient):
    """Hard-coded news events for backtest / dev. No network I/O.

    The default fixture injects 3 USD high-impact events: one in the
    past (already happened), one in 1 hour, one in 4 hours. Override
    by passing ``events=`` to the constructor.
    """

    def __init__(self, events: list[NewsEvent] | None = None) -> None:
        if events is None:
            base = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
            events = [
                NewsEvent(
                    ts=base - timedelta(hours=2),
                    currency="USD",
                    title="Stub past CPI",
                    impact=NewsImpact.HIGH,
                ),
                NewsEvent(
                    ts=base + timedelta(hours=1),
                    currency="USD",
                    title="Stub upcoming NFP",
                    impact=NewsImpact.HIGH,
                ),
                NewsEvent(
                    ts=base + timedelta(hours=4),
                    currency="USD",
                    title="Stub upcoming FOMC",
                    impact=NewsImpact.HIGH,
                ),
            ]
        self._events = events

    def fetch(self, current_t: datetime, lookahead_hours: int) -> list[NewsEvent]:
        # We return ALL known events (the caller filters); the stub is
        # deterministic so callers can rely on a fixed fixture.
        return [e for e in self._events if e.ts >= current_t - timedelta(hours=24)]


def _impact_from_str(s: str) -> NewsImpact:
    s = (s or "").strip().lower()
    if s.startswith("high"):
        return NewsImpact.HIGH
    if s.startswith("med"):
        return NewsImpact.MEDIUM
    return NewsImpact.LOW


class ForexFactoryNewsProvider(NewsProviderClient):
    """Real economic calendar via the free Forex Factory weekly JSON feed.

    No API key required. Fetches the current week's calendar, filters to the
    configured currencies (default ``USD`` — the dominant driver for XAUUSD)
    and keeps high/medium-impact events. The HTTP response is cached and
    refreshed at most every ``refresh_seconds`` so the per-bar ``fetch`` call
    stays cheap; on a network error the last good cache is kept.

    The feed gives ~1 week ahead, which comfortably covers the 24h lookahead
    the engine uses for the news-blackout filter (NFP / FOMC / CPI etc.).
    """

    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(
        self,
        *,
        currencies: tuple[str, ...] = ("USD",),
        refresh_seconds: float = 1800.0,
        timeout: float = 8.0,
        url: str | None = None,
    ) -> None:
        self._currencies = {c.upper() for c in currencies}
        self._refresh = float(refresh_seconds)
        self._timeout = float(timeout)
        self._url = url or self.URL
        self._cache: list[NewsEvent] = []
        self._fetched_at = 0.0

    def _refresh_if_stale(self) -> None:
        now = time.monotonic()
        if self._cache and (now - self._fetched_at) < self._refresh:
            return
        try:
            import httpx

            resp = httpx.get(
                self._url, timeout=self._timeout, headers={"User-Agent": "GoldManager/1.0"}
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001 - keep the last good cache on failure
            log.warning("news_forexfactory_fetch_failed", error=str(exc))
            self._fetched_at = now  # back off; avoid re-fetching every bar
            return
        events: list[NewsEvent] = []
        for it in rows if isinstance(rows, list) else []:
            cur = str(it.get("country") or it.get("currency") or "").upper()
            if self._currencies and cur not in self._currencies:
                continue
            ts = _parse_ff_date(it.get("date"))
            if ts is None:
                continue
            events.append(
                NewsEvent(
                    ts=ts,
                    currency=cur,
                    title=str(it.get("title") or "")[:120],
                    impact=_impact_from_str(str(it.get("impact") or "")),
                    forecast=(str(it["forecast"]) if it.get("forecast") else None),
                    previous=(str(it["previous"]) if it.get("previous") else None),
                )
            )
        self._cache = events
        self._fetched_at = now
        log.info("news_forexfactory_fetched", events=len(events), currencies=sorted(self._currencies))

    def fetch(self, current_t: datetime, lookahead_hours: int) -> list[NewsEvent]:
        self._refresh_if_stale()
        return [e for e in self._cache if e.ts >= current_t - timedelta(hours=24)]


def _parse_ff_date(s: object) -> datetime | None:
    """Parse a Forex Factory ISO date (e.g. ``2026-06-19T12:30:00-04:00``) → UTC."""
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def make_news_provider(settings: object) -> NewsProviderClient:
    """Construct the news provider selected by ``settings.news_api_provider``.

    ``forexfactory`` → real free calendar (no key). ``stub`` (and the
    not-yet-implemented ``tradingeconomics`` / ``fxstreet``) → deterministic
    stub. Currencies/refresh are read from settings when present.
    """
    provider = getattr(getattr(settings, "news_api_provider", None), "value", None) or str(
        getattr(settings, "news_api_provider", "stub")
    )
    if provider == "forexfactory":
        currencies = tuple(getattr(settings, "news_currencies", ("USD",)) or ("USD",))
        return ForexFactoryNewsProvider(currencies=currencies)
    if provider in ("tradingeconomics", "fxstreet"):
        log.warning("news_provider_not_implemented_using_stub", requested=provider)
    return StubNewsProvider()


# ---------------------------------------------------------------- engine


class NewsContextEngine:
    """Compute news context features for the current time.

    Parameters
    ----------
    provider:
        Configured provider (default :class:`StubNewsProvider`).
    pre_blackout_minutes:
        Minutes BEFORE a high-impact event to start the blackout. Default 15.
    post_blackout_minutes:
        Minutes AFTER a high-impact event to end the blackout. Default 5.
    lookahead_hours:
        How far ahead to scan for upcoming events. Default 24.
    """

    def __init__(
        self,
        provider: NewsProviderClient | None = None,
        pre_blackout_minutes: int = 15,
        post_blackout_minutes: int = 5,
        lookahead_hours: int = 24,
        clock_offset_minutes: float = 0.0,
    ) -> None:
        self._provider = provider or StubNewsProvider()
        self._pre = pre_blackout_minutes
        self._post = post_blackout_minutes
        self._lookahead = lookahead_hours
        # Subtracted from the incoming bar time to align it with the calendar's
        # real-UTC event times. MT5 bar times are in BROKER-server time (e.g.
        # UTC+3), so without this the blackout window would be hours off. 0 for
        # replay/stub (bar time is already the comparison frame). Set live via
        # :meth:`set_clock_offset`.
        self._offset_min = float(clock_offset_minutes)

    def set_clock_offset(self, minutes: float) -> None:
        """Set the broker→UTC offset (minutes) applied to incoming bar times."""
        self._offset_min = float(minutes)

    def compute(self, current_t: datetime) -> NewsContextOutput:
        if self._offset_min:
            current_t = current_t - timedelta(minutes=self._offset_min)
        events = self._provider.fetch(current_t, self._lookahead)
        # Only future events (relative to current_t).
        upcoming = [e for e in events if e.ts >= current_t]
        # Next high-impact (only future).
        future_high = sorted(
            [e for e in upcoming if e.impact == NewsImpact.HIGH],
            key=lambda e: e.ts,
        )
        next_hi = future_high[0] if future_high else None
        if next_hi is not None:
            minutes_until = (next_hi.ts - current_t).total_seconds() / 60.0
        else:
            minutes_until = None

        # Blackout check: are we inside any high-impact event's blackout
        # window? Check both future events (pre-blackout) and the most
        # recent past event (post-blackout). For the past event we
        # only need the closest one in the last ``post_blackout``
        # minutes.
        in_blackout = False
        for e in future_high:
            start = e.ts - timedelta(minutes=self._pre)
            end = e.ts + timedelta(minutes=self._post)
            if start <= current_t <= end:
                in_blackout = True
                break
        if not in_blackout:
            # Check the most recent past high-impact for the post-window.
            past_high = sorted(
                [e for e in events if e.ts < current_t and e.impact == NewsImpact.HIGH],
                key=lambda e: e.ts,
                reverse=True,
            )
            if past_high:
                last = past_high[0]
                if last.ts + timedelta(minutes=self._post) >= current_t:
                    in_blackout = True

        return NewsContextOutput(
            minutes_until_next_high_impact=minutes_until,
            in_blackout_flag=in_blackout,
            next_high_impact=next_hi,
            upcoming_events=upcoming,
            surprise_score=0.0,  # placeholder until live data wires in
        )
