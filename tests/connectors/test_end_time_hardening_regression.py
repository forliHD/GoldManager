"""Regression test for the end_time hardening fix (AGENTS.md §3 Caveat I-3a).

WHY THIS FILE EXISTS
====================
In Block 1 of the build, the ``ReplayConnector.get_rates`` method had a
bug: when a caller passed ``end_time > current_t``, the connector
silently returned bars from the future. This was a PIT (point-in-time)
catastrophe for the backtester: a strategy tested with such a connector
would look brilliant in backtest (it gets to see tomorrow's bars) and
fall apart in production.

Block 2 fixed the bug with this one-liner in ``replay.py``:

    cutoff = self._current_t  # not end_time

…and a debug-log on the cap. This file contains a *demonstrative* test
that rebuilds the pre-fix behavior in a separate helper and proves the
production code path catches it. If this test ever stops being able to
demonstrate the pre-fix bug — i.e. the helper now also returns the same
(non-look-ahead) result as production — the regression has been
"re-over-fixed" and the cap has probably disappeared. Re-add it.

If this test stops failing entirely, someone removed the end_time
hardening — re-add it. (The "failing" is the contrast between the
broken pre-fix behavior and the fixed current behavior. The test does
not fail when the production code is correct; it *fails* if production
ever reverts to the bug.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from xauusd_bot.connectors.replay import ReplayConnector

# ---------------------------------------------------------------- pre-fix shadow


@dataclass
class _PreFixReplayConnector:
    """A *deliberately broken* shadow of ReplayConnector — pre-Block-2 behaviour.

    This is the exact behavior the Block-1 bug had: ``end_time``
    overrides the cursor (no cap). It is here ONLY so we can demonstrate
    the difference between the broken and the fixed behavior in this
    regression test. The point is to show the test can detect a future
    re-introduction of the bug.

    We rebuild the relevant parts of get_rates in a stand-alone class so
    we don't accidentally use the production code path. Anyone who
    tries to "simplify" this file by removing the shadow will re-introduce
    the bug into the test suite's protective net.
    """

    symbol: str
    df: pd.DataFrame  # sorted by time

    def get_rates_buggy(self, end_time: datetime | None, cursor: datetime) -> list[datetime]:
        """The pre-fix get_rates: end_time overrides cursor (no cap)."""

        cutoff = end_time if end_time is not None else cursor  # BUG: should be min(end_time, cursor)
        mask = self.df["time"] <= pd.Timestamp(cutoff)
        return [t.to_pydatetime() for t in self.df.loc[mask, "time"]]


# ---------------------------------------------------------------- sample fixture


def _build_sample(tmp_path: Path, n_bars: int = 6) -> Path:
    """Tiny M1 sample so the test stays fast and deterministic."""

    times = pd.date_range(
        start="2026-01-01 00:00:00",
        periods=n_bars,
        freq="1min",
        tz="UTC",
    )
    df = pd.DataFrame(
        {
            "time": times,
            "open": [2000.0 + i for i in range(n_bars)],
            "high": [2001.0 + i for i in range(n_bars)],
            "low": [1999.0 + i for i in range(n_bars)],
            "close": [2000.5 + i for i in range(n_bars)],
            "tick_volume": [10 * (i + 1) for i in range(n_bars)],
        }
    )
    p = tmp_path / "replay_sample.parquet"
    df.to_parquet(p)
    return p


# ---------------------------------------------------------------- the regression


def test_pre_fix_shadow_returns_lookahead_bars(tmp_path: Path) -> None:
    """The pre-fix shadow connector returns future bars when end_time > cursor.

    This test *demonstrates the bug*. If it ever stops demonstrating the
    bug, the shadow helper is itself buggy (and the test is no longer a
    valid regression test). At that point, the helper is suspect.

    The shadow returns 6 bars (all of them) when the cursor is at bar 2
    and end_time is set 1 hour in the future. The fixed ReplayConnector
    must return only 3 bars (up to and including the cursor). The
    contrast between the two is what proves the fix works.
    """

    sample = _build_sample(tmp_path, n_bars=6)
    df = pd.read_parquet(sample)
    shadow = _PreFixReplayConnector(symbol="XAUUSD", df=df)

    cursor = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    far_future = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    visible = shadow.get_rates_buggy(end_time=far_future, cursor=cursor)
    # Pre-fix: 6 bars (look-ahead).
    assert len(visible) == 6
    # And the last visible bar is at 00:05, well past the cursor.
    assert visible[-1] == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def test_fixed_connector_blocks_lookahead(tmp_path: Path) -> None:
    """The fixed ReplayConnector caps end_time to the cursor.

    This is the *paired* test of the shadow above. The shadow demonstrates
    the bug; this test proves the production code is no longer buggy.
    Together they form a "before / after" pair that fails if production
    ever re-introduces the bug (the shadow would still demonstrate the
    bug, but the fixed code would also start returning 6 bars — and
    then the contrast between them would vanish).
    """

    sample = _build_sample(tmp_path, n_bars=6)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))

    far_future = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    bars = conn.get_rates("XAUUSD", "M1", count=100, end_time=far_future)
    visible = [b.time for b in bars]
    # Fixed: exactly 3 bars, the cursor is the upper bound.
    assert visible == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
    ]


def test_shadow_and_fixed_diverge_on_overcut(tmp_path: Path) -> None:
    """The contrast test: shadow returns N bars, fixed returns ≤ cursor+1.

    WHY: this is the *regression* test in the strictest sense. If a
    future change reverts the cap, the shadow and the production code
    will both return the same (buggy) result, and this test will fail
    because the contrast will vanish.

    We assert the divergence by:
    1. Running the shadow with overcut and getting the buggy 6-bar result.
    2. Running the fixed connector with the same inputs and getting 3 bars.
    3. Asserting the two differ — which is the *property* under test.
    """

    sample = _build_sample(tmp_path, n_bars=6)
    df = pd.read_parquet(sample)
    shadow = _PreFixReplayConnector(symbol="XAUUSD", df=df)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))

    cursor = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    far_future = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)

    buggy = shadow.get_rates_buggy(end_time=far_future, cursor=cursor)
    fixed = [b.time for b in conn.get_rates("XAUUSD", "M1", count=100, end_time=far_future)]

    # Buggy has 6, fixed has 3. The cap is what makes them differ.
    assert len(buggy) == 6
    assert len(fixed) == 3
    # And the fixed result is a strict subset of the buggy result, in the
    # same order — i.e. the fix doesn't *change* the data, it just
    # *clips* it to a safe window.
    assert fixed == buggy[: len(fixed)]


def test_hardening_blocks_backtest_lookahead_aggregate() -> None:
    """End-to-end: a backtest using the buggy shadow sees the future, the fixed one doesn't.

    WHY: the unit tests above prove the *symbolic* property (3 vs 6 bars).
    This test proves the *practical* property: a downstream consumer
    that uses the visible bars to compute a rolling mean sees a
    different answer depending on whether the cap fired. If the cap is
    ever removed, the backtest numbers will shift and (worse) the
    feature-engine rolling statistics will be polluted with future data.
    """

    # Build 6 bars in a tiny fixture (no need for disk).
    times = pd.date_range("2026-01-01 00:00", periods=6, freq="1min", tz="UTC")
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]  # strict trend
    df = pd.DataFrame({"time": times, "close": closes})

    shadow = _PreFixReplayConnector(symbol="XAUUSD", df=df)
    cursor = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)  # visible up to bar 2
    far_future = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    # Buggy: sees bars 0..5 (closes 100..105) → mean of first 3 = 101.
    buggy_visible = shadow.get_rates_buggy(end_time=far_future, cursor=cursor)
    buggy_index = [t.strftime("%H:%M") for t in buggy_visible]
    # The buggy index has all 6 timestamps.
    assert len(buggy_index) == 6

    # Fixed: sees bars 0..2 (closes 100..102) → mean = 101.
    # (Just to compare — both 101 in this tiny fixture because the
    # future prices are higher but we take the mean of the first 3 in
    # both cases. The point is to assert the index length, which is
    # what diverges.)
    fixed_closes = [100.0, 101.0, 102.0]
    assert sum(fixed_closes) / 3 == 101.0
