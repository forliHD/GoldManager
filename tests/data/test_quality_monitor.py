"""Tests for DataQualityMonitor — gap / spike / OHLC / spec-drift detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from xauusd_bot.connectors.schemas import Bar, SymbolSpec
from xauusd_bot.data.quality_monitor import DataQualityMonitor


# ---------------------------------------------------------------- fixtures


def _spec(price_limit_max: Decimal | None = None) -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD",
        description="XAUUSD CFD",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
        price_limit_max=price_limit_max,
    )


def _m1_bar(t: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=t,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


# =============================================================== 1. gap detection


def test_gap_detection_two_missing_m1_bars_flagged() -> None:
    """A 2-bar gap (bar skipped at +2min) must be flagged as a gap."""

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    qm.update(_m1_bar(base, 2000, 2001, 1999, 2000.5))
    # Skip +1min bar; feed +2min bar.
    qm.update(_m1_bar(base + timedelta(minutes=2), 2001, 2002, 2000, 2001.5))
    assert qm.report.n_gaps == 1
    assert qm.report.max_gap_bars == 1


def test_gap_detection_three_missing_bars() -> None:
    """A 3-bar gap (skipped 3 M1 bars) is flagged with max_gap_bars=3."""

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    qm.update(_m1_bar(base, 2000, 2001, 1999, 2000.5))
    # Skip +1, +2, +3; feed +4.
    qm.update(_m1_bar(base + timedelta(minutes=4), 2001, 2002, 2000, 2001.5))
    assert qm.report.n_gaps == 1
    assert qm.report.max_gap_bars == 3


def test_gap_detection_consecutive_gaps_accumulate() -> None:
    """Multiple gaps are counted, and max_gap_bars is the largest."""

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    qm.update(_m1_bar(base, 2000, 2001, 1999, 2000.5))
    # First gap: skip 1, feed at +2 → max_gap_bars=1
    qm.update(_m1_bar(base + timedelta(minutes=2), 2001, 2002, 2000, 2001.5))
    # Second gap: skip 2, feed at +5 → max_gap_bars=2
    qm.update(_m1_bar(base + timedelta(minutes=5), 2001, 2002, 2000, 2001.5))
    assert qm.report.n_gaps == 2
    assert qm.report.max_gap_bars == 2


def test_no_gap_on_consecutive_bars() -> None:
    """Consecutive bars at +1min each must not be flagged."""

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(10):
        qm.update(_m1_bar(base + timedelta(minutes=i), 2000 + i * 0.1, 2000.5 + i * 0.1, 1999.5 + i * 0.1, 2000.2 + i * 0.1))
    assert qm.report.n_gaps == 0


# =============================================================== 2. spike detection


def test_spike_detection_10x_normal_range_flagged() -> None:
    """A 10x range spike among 20 normal bars must be flagged.

    Note: the spike check includes the *current* bar's range in the
    rolling 20-bar ATR. So 20 normal bars (range=1.0) followed by a
    spike (range=20.0) gives an ATR of (19*1.0 + 20.0) / 20 = 1.45;
    the threshold is 8.0 * 1.45 = 11.6; the spike's range (20.0)
    easily exceeds 11.6 and is flagged.
    """

    qm = DataQualityMonitor(spec=_spec(), spike_atr_multiple=8.0)
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # 20 normal bars with range = 1.0 (so the rolling ATR window fills).
    for i in range(20):
        qm.update(_m1_bar(base + timedelta(minutes=i), 2000, 2000.5, 1999.5, 2000.2, tv=100))
    # 21st bar: range = 20.0 (20x the median bar) — must be flagged.
    qm.update(_m1_bar(base + timedelta(minutes=20), 2000, 2010, 1990, 2005, tv=100))
    assert qm.report.n_spikes == 1


def test_spike_detection_handles_2x_range_not_flagged() -> None:
    """A 2x range bar (below spike_atr_multiple=8) is NOT a spike."""

    qm = DataQualityMonitor(spec=_spec(), spike_atr_multiple=8.0)
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(20):
        qm.update(_m1_bar(base + timedelta(minutes=i), 2000, 2000.5, 1999.5, 2000.2, tv=100))
    # 2x range spike — below the 8x threshold.
    qm.update(_m1_bar(base + timedelta(minutes=20), 2000, 2001, 1998, 2000.5, tv=100))
    assert qm.report.n_spikes == 0


def test_spike_detection_requires_full_atr_window() -> None:
    """A spike cannot be flagged until at least 20 bars have been seen
    (the ATR window size)."""

    qm = DataQualityMonitor(spec=_spec(), spike_atr_multiple=8.0)
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Only 19 bars — the 20th would be a spike but the window isn't full.
    for i in range(19):
        qm.update(_m1_bar(base + timedelta(minutes=i), 2000, 2001, 1999, 2000.5))
    # Even with a 20x range, no spike is flagged yet (window < 20).
    qm.update(_m1_bar(base + timedelta(minutes=19), 2000, 2010, 1990, 2005))
    assert qm.report.n_spikes == 0


# =============================================================== 3. OHLC inconsistency


def test_ohlc_inconsistency_high_less_than_low_flagged() -> None:
    """A bar with high < low is an OHLC inconsistency."""

    qm = DataQualityMonitor(spec=_spec())
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=100, h=99, low=101, c=100)
    qm.update(bad)
    assert qm.report.n_ohlc_inconsistent == 1


def test_ohlc_inconsistency_close_above_high_flagged() -> None:
    """A bar with close > high is an OHLC inconsistency."""

    qm = DataQualityMonitor(spec=_spec())
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=100, h=100, low=99, c=101)
    qm.update(bad)
    assert qm.report.n_ohlc_inconsistent == 1


def test_ohlc_inconsistency_close_below_low_flagged() -> None:
    """A bar with close < low is an OHLC inconsistency."""

    qm = DataQualityMonitor(spec=_spec())
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=100, h=101, low=99, c=98)
    qm.update(bad)
    assert qm.report.n_ohlc_inconsistent == 1


def test_ohlc_inconsistency_open_above_high_flagged() -> None:
    """A bar with open > high is an OHLC inconsistency."""

    qm = DataQualityMonitor(spec=_spec())
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=102, h=101, low=99, c=100)
    qm.update(bad)
    assert qm.report.n_ohlc_inconsistent == 1


def test_ohlc_inconsistency_open_below_low_flagged() -> None:
    """A bar with open < low is an OHLC inconsistency."""

    qm = DataQualityMonitor(spec=_spec())
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=98, h=101, low=99, c=100)
    qm.update(bad)
    assert qm.report.n_ohlc_inconsistent == 1


def test_valid_bar_ohlc_is_not_flagged() -> None:
    """A well-formed bar (low <= open,close <= high) is not flagged."""

    qm = DataQualityMonitor(spec=_spec())
    good = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=100, h=101, low=99, c=100.5)
    qm.update(good)
    assert qm.report.n_ohlc_inconsistent == 0


# =============================================================== 4. spec drift


def test_spec_drift_bar_above_price_limit_flagged() -> None:
    """A bar with high > price_limit_max * (1 + tolerance) is flagged."""

    spec = _spec(price_limit_max=Decimal("2000"))
    qm = DataQualityMonitor(spec=spec, spec_drift_tolerance=0.10)
    # Limit is 2000; tolerance is 10% → flagged if high > 2200.
    bad = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=2200, h=2300, low=2199, c=2210)
    qm.update(bad)
    assert qm.report.n_spec_drift == 1


def test_spec_drift_bar_within_tolerance_not_flagged() -> None:
    """A bar within the spec-drift tolerance is not flagged."""

    spec = _spec(price_limit_max=Decimal("2000"))
    qm = DataQualityMonitor(spec=spec, spec_drift_tolerance=0.10)
    # 5% above the limit — within the 10% tolerance.
    ok = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=2090, h=2095, low=2085, c=2092)
    qm.update(ok)
    assert qm.report.n_spec_drift == 0


def test_spec_drift_not_checked_when_limit_is_none() -> None:
    """A spec with price_limit_max=None must not flag anything for spec drift."""

    spec = _spec(price_limit_max=None)
    qm = DataQualityMonitor(spec=spec, spec_drift_tolerance=0.10)
    # An absurd bar would be flagged by OHLC checks, but not spec drift.
    bar = _m1_bar(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), o=100000, h=100001, low=99999, c=100000)
    qm.update(bar)
    assert qm.report.n_spec_drift == 0


# =============================================================== 5. clean run / report shape


def test_clean_run_reports_no_issues() -> None:
    """20+ consecutive, well-formed bars at +1min with normal range: no issues."""

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(25):
        qm.update(_m1_bar(base + timedelta(minutes=i), 2000 + i * 0.1, 2000.5 + i * 0.1, 1999.5 + i * 0.1, 2000.2 + i * 0.1, tv=10))
    assert qm.report.n_bars == 25
    assert qm.report.n_gaps == 0
    assert qm.report.n_spikes == 0
    assert qm.report.n_ohlc_inconsistent == 0
    assert qm.report.n_spec_drift == 0


def test_report_tracks_first_and_last_bar_time() -> None:
    """The report carries the first and last seen bar time."""

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    qm.update(_m1_bar(base, 2000, 2001, 1999, 2000.5))
    qm.update(_m1_bar(base + timedelta(minutes=1), 2001, 2002, 2000, 2001.5))
    assert qm.report.first_bar_time == base
    assert qm.report.last_bar_time == base + timedelta(minutes=1)


def test_report_to_dict_serializes_issues() -> None:
    """QualityReport.to_dict() must produce a JSON-serializable dict."""

    import json

    qm = DataQualityMonitor(spec=_spec())
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    qm.update(_m1_bar(base, 2000, 2001, 1999, 2000.5))
    qm.update(_m1_bar(base + timedelta(minutes=2), 2001, 2002, 2000, 2001.5))  # gap
    d = qm.report.to_dict()
    # Must be JSON-serializable (datetime, int, etc.).
    json.dumps(d, default=str)
    assert d["n_bars"] == 2
    assert d["n_gaps"] == 1
    assert d["max_gap_bars"] == 1
    assert len(d["issues"]) >= 1
    assert d["issues"][0]["kind"] == "gap"
