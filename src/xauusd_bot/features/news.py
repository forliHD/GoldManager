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
    ) -> None:
        self._provider = provider or StubNewsProvider()
        self._pre = pre_blackout_minutes
        self._post = post_blackout_minutes
        self._lookahead = lookahead_hours

    def compute(self, current_t: datetime) -> NewsContextOutput:
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
