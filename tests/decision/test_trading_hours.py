"""Tests for the trading-hours entry gate + weekend-flat (Joshua's window)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from xauusd_bot.decision.trading_hours import (
    REASON_OUTSIDE_TRADING_HOURS,
    REASON_WEEKEND_FLAT,
    TradingWindow,
)


class _Cfg:
    """Minimal duck-typed settings stand-in for TradingWindow.from_settings."""

    def __init__(self, **kw):
        self.exec_trading_window_enabled = kw.get("enabled", True)
        self.exec_trading_timezone = kw.get("tz", "UTC")
        self.exec_trading_start_local = kw.get("start", "00:00")
        self.exec_trading_end_local = kw.get("end", "22:55")
        self.exec_weekend_flat_enabled = kw.get("weekend_flat", True)
        self.exec_weekend_flat_utc = kw.get("weekend_flat_utc", "20:55")


def _win(**kw) -> TradingWindow:
    return TradingWindow.from_settings(_Cfg(**kw))


# Known weekdays (UTC, no DST): Thu 2026-01-01, Fri -02, Sat -03, Sun -04, Mon -05.
def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# ---------------------------------------------------------------- basics


def test_from_settings_parses_utc_defaults():
    win = _win()
    assert win.enabled is True
    assert win.start == time(0, 0)
    assert win.end == time(22, 55)
    assert win.tz_name == "UTC"
    assert win.weekend_flat_enabled is True
    assert win.weekend_flat_cutoff == time(20, 55)


def test_disabled_window_always_allows():
    win = _win(enabled=False)
    assert win.allows(_utc(2026, 1, 1, 23, 30)) is True


def test_bad_bounds_fail_open_disabled():
    win = _win(start="not-a-time")
    assert win.enabled is False
    assert win.allows(_utc(2026, 1, 1, 23, 30)) is True


# ---------------------------------------------------------------- UTC window [00:00, 22:55)


@pytest.mark.parametrize(
    "h, mi, allowed",
    [
        (0, 0, True),    # 00:00 — Asian open, inclusive start
        (12, 0, True),   # midday
        (22, 54, True),  # last allowed minute
        (22, 55, False), # NY-close cutoff, exclusive ("kein Volumen mehr")
        (23, 30, False), # dead hour before Asian open
    ],
)
def test_utc_boundaries(h, mi, allowed):
    assert _win().allows(_utc(2026, 1, 1, h, mi)) is allowed


def test_broker_offset_recovered_before_window_check():
    # Broker 01:54 labelled UTC, offset +180 → real 22:54 UTC → allowed;
    # broker 01:55 → real 22:55 → blocked.
    win = _win()
    assert win.allows(_utc(2026, 1, 2, 1, 54), broker_offset_minutes=180) is True
    assert win.allows(_utc(2026, 1, 2, 1, 55), broker_offset_minutes=180) is False


def test_non_utc_timezone_still_dst_adjusts():
    # Europe/Berlin window 02:00–20:00 → summer 00:00–18:00 UTC.
    win = _win(tz="Europe/Berlin", start="02:00", end="20:00")
    assert win.allows(_utc(2026, 7, 1, 0, 0)) is True    # 02:00 Berlin
    assert win.allows(_utc(2026, 7, 1, 18, 0)) is False  # 20:00 Berlin


# ---------------------------------------------------------------- robustness


def test_unknown_tz_fails_open():
    win = _win(tz="Not/AZone")
    assert win.allows(_utc(2026, 1, 1, 23, 30)) is True


def test_naive_ts_treated_as_utc():
    win = _win()
    assert win.allows(datetime(2026, 1, 1, 12, 0)) is True
    assert win.allows(datetime(2026, 1, 1, 23, 30)) is False


# ---------------------------------------------------------------- weekend flat


@pytest.mark.parametrize(
    "ts, flat",
    [
        (_utc(2026, 1, 2, 20, 55), True),   # Friday at cutoff
        (_utc(2026, 1, 2, 21, 30), True),   # Friday after cutoff
        (_utc(2026, 1, 2, 20, 54), False),  # Friday before cutoff
        (_utc(2026, 1, 1, 23, 0), False),   # Thursday late — overnight hold OK
        (_utc(2026, 1, 3, 3, 0), True),     # Saturday — catch-all
        (_utc(2026, 1, 4, 10, 0), True),    # Sunday — catch-all
        (_utc(2026, 1, 5, 0, 0), False),    # Monday open — fine
    ],
)
def test_weekend_flat_schedule(ts, flat):
    assert _win().should_flatten_for_weekend(ts) is flat


def test_weekend_flat_respects_broker_offset():
    # Broker 23:55 Fri labelled UTC, offset +180 → real 20:55 Fri UTC → flatten.
    win = _win()
    assert win.should_flatten_for_weekend(_utc(2026, 1, 2, 23, 55), broker_offset_minutes=180) is True


def test_weekend_flat_disabled():
    win = _win(weekend_flat=False)
    assert win.should_flatten_for_weekend(_utc(2026, 1, 3, 3, 0)) is False


def test_weekend_flat_sunday_after_reopen_not_flattened():
    # Review #7: a position opened at the Sunday-evening reopen (which allows()
    # permits) must NOT be force-closed on its first bar. Sunday before the cutoff
    # still flattens a weekend survivor; Sunday at/after it does not.
    win = _win()  # Sun = 2026-01-04
    assert win.should_flatten_for_weekend(_utc(2026, 1, 4, 10, 0)) is True    # pre-reopen survivor
    assert win.should_flatten_for_weekend(_utc(2026, 1, 4, 22, 30)) is False  # after reopen
    assert win.should_flatten_for_weekend(_utc(2026, 1, 4, 20, 55)) is False  # at cutoff (>= → keep)


def test_weekend_flat_cutoff_is_utc_not_window_tz():
    # Review #6: the cutoff is UTC (the field is *_utc), independent of the entry
    # window's display timezone. With a Berlin window, a Friday instant at 20:55
    # UTC flattens; one at 19:55 UTC (= 20:55 Berlin, winter) does NOT.
    win = _win(tz="Europe/Berlin")  # Fri = 2026-01-02
    assert win.should_flatten_for_weekend(_utc(2026, 1, 2, 20, 55)) is True
    assert win.should_flatten_for_weekend(_utc(2026, 1, 2, 19, 55)) is False


def test_reason_constants_stable():
    assert REASON_OUTSIDE_TRADING_HOURS == "outside_trading_hours"
    assert REASON_WEEKEND_FLAT == "weekend_flat"
