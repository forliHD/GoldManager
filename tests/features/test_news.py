"""Tests for the NewsContextEngine."""

from __future__ import annotations

from datetime import UTC, datetime

from xauusd_bot.common.schemas.features import NewsEvent, NewsImpact
from xauusd_bot.features.news import NewsContextEngine, StubNewsProvider

# ---------------------------------------------------------------- stub


def test_stub_provider_returns_deterministic_events() -> None:
    """The stub returns 3 USD high-impact events around a fixed anchor."""

    provider = StubNewsProvider()
    base = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    events = provider.fetch(base, lookahead_hours=24)
    # 1 past (still in the 24h lookback) + 2 future.
    assert len(events) == 3
    assert all(e.currency == "USD" for e in events)
    assert all(e.impact == NewsImpact.HIGH for e in events)


def test_stub_provider_filters_to_window() -> None:
    """A query far in the future gets no events."""

    provider = StubNewsProvider()
    base = datetime(2027, 1, 5, 12, 0, tzinfo=UTC)
    events = provider.fetch(base, lookahead_hours=24)
    assert events == []


# ---------------------------------------------------------------- countdown


def test_minutes_until_next_high_impact() -> None:
    eng = NewsContextEngine()
    # The default stub puts the next high-impact 1h after 12:00 UTC.
    current_t = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    out = eng.compute(current_t)
    assert out.minutes_until_next_high_impact == 60.0
    assert out.next_high_impact is not None
    assert out.next_high_impact.title == "Stub upcoming NFP"


def test_minutes_until_none_when_no_upcoming() -> None:
    """No future events → None countdown."""

    eng = NewsContextEngine(
        provider=StubNewsProvider(
            events=[
                NewsEvent(
                    ts=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
                    currency="USD",
                    title="Past event",
                    impact=NewsImpact.HIGH,
                ),
            ]
        )
    )
    current_t = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    out = eng.compute(current_t)
    assert out.minutes_until_next_high_impact is None
    assert out.next_high_impact is None


# ---------------------------------------------------------------- blackout


def test_blackout_flag_15_min_before_event() -> None:
    """T-15min from a high-impact event → in blackout.

    The default stub places the next high-impact at base+1h = 13:00 UTC.
    Querying at 12:50 (10 min before) is inside the 15-min pre-window.
    """

    eng = NewsContextEngine(pre_blackout_minutes=15, post_blackout_minutes=5)
    current_t = datetime(2026, 1, 5, 12, 50, tzinfo=UTC)
    out = eng.compute(current_t)
    assert out.in_blackout_flag is True


def test_blackout_flag_5_min_after_event() -> None:
    """T+5min from a high-impact event → still in blackout (post 5 min).

    Querying at 13:03 (3 min after the 13:00 stub NFP) is inside the
    5-min post-window.
    """

    eng = NewsContextEngine(pre_blackout_minutes=15, post_blackout_minutes=5)
    current_t = datetime(2026, 1, 5, 13, 3, tzinfo=UTC)
    out = eng.compute(current_t)
    assert out.in_blackout_flag is True


def test_no_blackout_outside_window() -> None:
    """Far from any high-impact → no blackout.

    Querying at 13:20 is 20 min after the 13:00 NFP (post=5).
    """

    eng = NewsContextEngine()
    current_t = datetime(2026, 1, 5, 13, 20, tzinfo=UTC)
    out = eng.compute(current_t)
    assert out.in_blackout_flag is False


# ---------------------------------------------------------------- low-impact


def test_low_impact_event_does_not_trigger_blackout() -> None:
    eng = NewsContextEngine(
        provider=StubNewsProvider(
            events=[
                NewsEvent(
                    ts=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
                    currency="USD",
                    title="Low impact",
                    impact=NewsImpact.LOW,
                ),
            ]
        )
    )
    out = eng.compute(datetime(2026, 1, 5, 11, 59, tzinfo=UTC))
    assert out.in_blackout_flag is False


# ---------------------------------------------------------------- surprise


def test_surprise_score_default_zero() -> None:
    """surprise_score is a placeholder (0.0) until live data lands."""

    eng = NewsContextEngine()
    out = eng.compute(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    assert out.surprise_score == 0.0


def test_minutes_until_zero_at_event_time() -> None:
    """At the exact event time, minutes_until = 0."""

    eng = NewsContextEngine()
    out = eng.compute(datetime(2026, 1, 5, 13, 0, tzinfo=UTC))
    assert out.minutes_until_next_high_impact == 0.0
