"""Tests for the feature-engine broker→UTC clock-offset detection guard.

Regression cover for the 2026-06-27 incident: the engine detected the
offset once at warmup from the freshest warmup bar with no freshness check,
so a Saturday (market-closed) restart read Friday's stale close and pinned
``offset=-540`` (−9h) instead of ``+180`` (+3h) for the whole process life,
shifting the VWAP / session / trading-hours anchors by ~12h.

:func:`_detect_clock_offset` must REJECT such implausible values (returning
``None``) so the caller keeps the previous offset and the first fresh
streamed bar self-heals it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xauusd_bot.feature_engine import _detect_clock_offset

# A fixed, market-open reference "now" — Tuesday 2026-06-30 09:46 UTC.
_NOW = datetime(2026, 6, 30, 9, 46, tzinfo=UTC)


@pytest.mark.parametrize(
    ("latest", "expected"),
    [
        # Fresh bar, broker UTC+3 (summer) → +180.
        (_NOW + timedelta(hours=3), 180),
        # Fresh bar, broker UTC+2 (DST shift) → +120.
        (_NOW + timedelta(hours=2), 120),
        # Bar-close lag inside the hour still rounds to the broker offset.
        (_NOW + timedelta(hours=3) - timedelta(seconds=40), 180),
        # Plausibility-band boundaries are inclusive.
        (_NOW + timedelta(hours=1), 60),
        (_NOW + timedelta(hours=4), 240),
    ],
)
def test_fresh_bar_yields_plausible_offset(latest: datetime, expected: int) -> None:
    assert _detect_clock_offset(latest, _NOW) == expected


@pytest.mark.parametrize(
    "latest",
    [
        # THE INCIDENT: a Saturday restart reads Friday's ~9h-stale close → −540.
        datetime(2026, 6, 27, 8, 33, tzinfo=UTC) - timedelta(hours=9),
        # Even staler (long weekend / stopped bot) → huge negative.
        _NOW - timedelta(days=3),
        # Broker == UTC (or replay): offset rounds to 0, below the band.
        _NOW + timedelta(minutes=20),
        # Above the band (no real broker is UTC+5).
        _NOW + timedelta(hours=5),
    ],
)
def test_stale_or_implausible_bar_is_rejected(latest: datetime) -> None:
    # For the incident case, evaluate against the actual restart instant.
    now = datetime(2026, 6, 27, 8, 33, tzinfo=UTC) if latest < _NOW - timedelta(days=1) else _NOW
    assert _detect_clock_offset(latest, now) is None


def test_incident_reproduces_minus_540_then_rejects() -> None:
    """The exact -540 the live engine logged is computed, then rejected."""
    now = datetime(2026, 6, 27, 8, 33, 37, tzinfo=UTC)  # Saturday restart instant
    friday_close = datetime(2026, 6, 26, 23, 33, 37, tzinfo=UTC)  # ~9h stale
    # Sanity: the raw formula would have produced -540 (the pinned bad value).
    raw = round((friday_close - now).total_seconds() / 3600.0) * 60
    assert raw == -540
    # The guarded helper rejects it.
    assert _detect_clock_offset(friday_close, now) is None


def test_naive_latest_is_treated_as_utc() -> None:
    naive = (_NOW + timedelta(hours=3)).replace(tzinfo=None)
    assert _detect_clock_offset(naive, _NOW) == 180
