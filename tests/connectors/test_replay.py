"""Tests for the ReplayConnector — schema, point-in-time, determinism, spec.

Architectural contract under test:

* ``get_rates`` returns values that conform to the ``Bar`` schema (the
  same one the :class:`IMarketConnector` protocol promises).
* Point-in-Time is strict: no bar with ``time > current_t`` ever escapes.
* Time is monotonic — ``advance_time`` is the only way to unblock new
  bars, and the cursor never goes backwards.
* ``get_symbol_spec`` returns a fully populated :class:`SymbolSpec`.
* Replay is deterministic: same file + same cursor = byte-identical
  output across runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.schemas import Bar, SymbolSpec

# ---------------------------------------------------------------- helpers


def _build_sample(tmp_path: Path, n_bars: int = 5) -> Path:
    """Create a tiny deterministic parquet sample with M1 bars."""

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


def _df_to_bars_df(bars: list[Bar]) -> pd.DataFrame:
    """Turn a list[Bar] into a DataFrame for schema/equality assertions."""

    return pd.DataFrame(
        {
            "symbol": [b.symbol for b in bars],
            "timeframe": [b.timeframe for b in bars],
            "time": [b.time for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "tick_volume": [b.tick_volume for b in bars],
        }
    )


# --------------------------------------------------------------- schema


def test_get_rates_returns_schema_conformant_bars(tmp_path: Path) -> None:
    """Every bar returned by get_rates must satisfy the Bar schema."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn.advance_time(datetime(2026, 1, 1, 0, 4, tzinfo=UTC))
    bars = conn.get_rates("XAUUSD", "M1", count=10)

    assert len(bars) == 4
    # All values are Bars, all are timezone-aware UTC, all required fields populated.
    for b in bars:
        assert isinstance(b, Bar)
        assert b.symbol == "XAUUSD"
        assert b.timeframe == "M1"
        assert isinstance(b.time, datetime)
        assert b.time.tzinfo is not None
        assert b.time.utcoffset() == timedelta(0)
        assert b.high >= b.low
        assert b.open >= b.low and b.open <= b.high
        assert b.close >= b.low and b.close <= b.high
        assert b.tick_volume > 0

    # The DataFrame projection (what callers normally want) has the columns
    # the spec demands: time, open, high, low, close, tick_volume, and the
    # identity columns symbol/timeframe.
    df = _df_to_bars_df(bars)
    assert list(df.columns) == [
        "symbol", "timeframe", "time", "open", "high", "low", "close", "tick_volume",
    ]
    # Decimal round-trip preserves the source values (fixture uses 2000..2003 + 0.5).
    assert df["open"].iloc[0] == Decimal("2000")
    assert df["close"].iloc[-1] == Decimal("2003.5")


# --------------------------------------------------------------- point-in-time


def test_point_in_time_cursor_before_all_bars(tmp_path: Path) -> None:
    """An explicit ``end_time`` strictly before the first bar → no bars.

    The cursor is the *max* visible time; if a query specifies
    ``end_time < first_bar.time``, the connector must return the empty
    list. We use the explicit end_time knob (rather than moving the
    cursor backwards, which the API forbids) to test the strict
    "nothing visible" branch.
    """

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    # Default cursor is at the first bar; we narrow via end_time.
    assert conn.get_rates(
        "XAUUSD",
        "M1",
        count=10,
        end_time=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
    ) == []


def test_point_in_time_cursor_in_middle(tmp_path: Path) -> None:
    """Cursor in the middle → exactly the bars strictly <= cursor are visible."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    cutoff = datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)
    conn.advance_time(cutoff)
    bars = conn.get_rates("XAUUSD", "M1", count=100)
    assert [b.time for b in bars] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    ]
    assert all(b.time <= cutoff for b in bars)


def test_point_in_time_cursor_after_all_bars(tmp_path: Path) -> None:
    """Cursor after every bar → every bar is returned, and end_time still filters."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    far_future = datetime(2027, 1, 1, tzinfo=UTC)
    conn.advance_time(far_future)
    bars = conn.get_rates("XAUUSD", "M1", count=100)
    assert len(bars) == 4
    assert all(b.time <= far_future for b in bars)

    # An explicit end_time still clips to <= end_time, even if the cursor is later.
    early_end = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    clipped = conn.get_rates("XAUUSD", "M1", count=100, end_time=early_end)
    assert [b.time for b in clipped] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    ]
    assert all(b.time <= early_end for b in clipped)


def test_end_time_above_current_t_is_capped(tmp_path: Path) -> None:
    """end_time > current_t must be capped to current_t (Caveat I-3a / AGENTS.md §3 I-3).

    A caller passing end_time in the future used to silently receive
    look-ahead bars. The hardening fix: cap to min(end_time, current_t)
    and emit a debug log. This test asserts the visible window is
    bounded by the cursor, NOT by the requested end_time, and that
    the result equals the result of an equivalent query without
    end_time (both should be the same set of visible bars).
    """

    sample = _build_sample(tmp_path, n_bars=6)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    # Pin the cursor at bar index 2 (time 00:02:00). Bars at 00:03, 00:04, 00:05
    # exist in the source but are "future" relative to the cursor.
    cursor = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    conn.advance_time(cursor)

    # Caller asks for everything up to a time 1 hour in the future. Before
    # the fix, this would have returned all 6 bars (look-ahead). After the
    # fix, the cutoff is capped to the cursor (00:02) → exactly 3 bars.
    far_future = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    capped = conn.get_rates("XAUUSD", "M1", count=100, end_time=far_future)
    assert [b.time for b in capped] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
    ]
    # The cap is on the *time axis*, not on `count`. We asked for 100
    # bars; the cap is what limits the result, not the count.
    assert all(b.time <= cursor for b in capped)

    # The capped result must equal a no-end_time query at the same cursor —
    # both should see exactly the same 3 bars.
    no_end = conn.get_rates("XAUUSD", "M1", count=100)
    assert [b.time for b in capped] == [b.time for b in no_end]

    # And a valid (in-the-past) end_time still works: an even earlier cutoff
    # should return fewer bars. This proves the cap branch is independent
    # from the normal filtering branch.
    earlier = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
    earlier_bars = conn.get_rates("XAUUSD", "M1", count=100, end_time=earlier)
    assert [b.time for b in earlier_bars] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    ]


# --------------------------------------------------------------- monotone time


def test_advance_time_is_monotone_and_strict(tmp_path: Path) -> None:
    """advance_time moves the cursor forward; no future bars escape until the next advance."""

    sample = _build_sample(tmp_path, n_bars=6)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")

    # First advance reveals bar 0 only.
    conn.advance_time(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    visible = [b.time for b in conn.get_rates("XAUUSD", "M1", count=100)]
    assert visible == [datetime(2026, 1, 1, 0, 0, tzinfo=UTC)]

    # Re-issuing the same query yields the same single bar (cursor didn't move).
    visible2 = [b.time for b in conn.get_rates("XAUUSD", "M1", count=100)]
    assert visible2 == visible

    # A jumpy advance_time: skip directly to bar 3.
    conn.advance_time(datetime(2026, 1, 1, 0, 3, tzinfo=UTC))
    visible3 = [b.time for b in conn.get_rates("XAUUSD", "M1", count=100)]
    assert visible3 == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
    ]


def test_advance_time_rejects_time_travel(tmp_path: Path) -> None:
    """advance_time must raise if called with a time before current_t."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
    with pytest.raises(ValueError, match=r"(?i)time travel"):
        conn.advance_time(datetime(2026, 1, 1, 0, 1, tzinfo=UTC))


def test_advance_time_requires_tz_aware(tmp_path: Path) -> None:
    """A naive datetime must be rejected with a clear ValueError."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    with pytest.raises(ValueError, match=r"timezone-aware"):
        conn.advance_time(datetime(2026, 1, 1, 0, 0))  # noqa: DTZ001 — intentional naive


# --------------------------------------------------------------- symbol spec


def test_get_symbol_spec_returns_full_spec(tmp_path: Path) -> None:
    """get_symbol_spec must return a fully populated SymbolSpec, not a partial stub."""

    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    spec = conn.get_symbol_spec("XAUUSD")
    assert isinstance(spec, SymbolSpec)
    assert spec.symbol == "XAUUSD"
    # Concrete defaults must be populated, not None / 0.
    assert spec.point == Decimal("0.01")
    assert spec.digits == 2
    assert spec.trade_contract_size == Decimal("100")
    assert spec.volume_min > 0
    assert spec.volume_max > spec.volume_min
    assert spec.volume_step > 0
    assert spec.margin_rate > 0
    assert spec.spread_max_warn_points > 0
    assert spec.spread_max_block_points > spec.spread_max_warn_points


def test_get_symbol_spec_rejects_wrong_symbol(tmp_path: Path) -> None:
    """A query for a different symbol must raise — replay is single-symbol."""

    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    with pytest.raises(ValueError, match="XAUUSD"):
        conn.get_symbol_spec("EURUSD")


# --------------------------------------------------------------- determinism


def test_replay_is_deterministic(tmp_path: Path) -> None:
    """Same file + same current_t = byte-identical output across two replays."""

    sample = _build_sample(tmp_path, n_bars=8)
    # Run #1
    conn1 = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn1.advance_time(datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
    bars1 = conn1.get_rates("XAUUSD", "M1", count=10)
    # Run #2 — fresh connector, same input
    conn2 = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn2.advance_time(datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
    bars2 = conn2.get_rates("XAUUSD", "M1", count=10)

    df1 = _df_to_bars_df(bars1)
    df2 = _df_to_bars_df(bars2)
    # pandas' frame equality is the strongest check we can make.
    pd.testing.assert_frame_equal(df1, df2)


def test_replay_satisfies_protocol(tmp_path: Path) -> None:
    """A constructed ReplayConnector must be a runtime instance of IMarketConnector."""

    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert isinstance(conn, IMarketConnector)


def test_replay_rejects_wrong_symbol_query(tmp_path: Path) -> None:
    """A get_rates call for a non-loaded symbol must raise with a clear message."""

    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    with pytest.raises(ValueError, match="XAUUSD"):
        conn.get_rates("EURUSD", "M1", count=1)


def test_replay_replay_source_missing_raises(tmp_path: Path) -> None:
    """A non-existent path must fail loudly with FileNotFoundError."""

    missing = tmp_path / "does_not_exist.parquet"
    with pytest.raises(FileNotFoundError, match="Replay source not found"):
        ReplayConnector(source_path=missing, symbol="XAUUSD")


def test_replay_rejects_unsupported_extension(tmp_path: Path) -> None:
    """A file extension outside {parquet, csv} must fail with ValueError."""

    bad = tmp_path / "sample.weird"
    bad.write_text("not real data")
    with pytest.raises(ValueError, match="Unsupported replay source format"):
        ReplayConnector(source_path=bad, symbol="XAUUSD")


def test_replay_replay_source_missing_columns(tmp_path: Path) -> None:
    """A parquet without the required columns must fail with a clear message."""

    p = tmp_path / "bad.parquet"
    pd.DataFrame({"time": [pd.Timestamp("2026-01-01", tz="UTC")], "open": [1.0]}).to_parquet(p)
    with pytest.raises(ValueError, match="missing required columns"):
        ReplayConnector(source_path=p, symbol="XAUUSD")


# --------------------------------------------------------------- advance_bars


def test_advance_bars_moves_cursor_forward(tmp_path: Path) -> None:
    """advance_bars(n) moves the cursor forward (and never backwards)."""

    sample = _build_sample(tmp_path, n_bars=10)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    initial = conn.current_t
    new_t = conn.advance_bars(3)
    assert new_t == conn.current_t
    # The new cursor must be at or after the initial position.
    assert new_t >= initial
    # And the cursor must not exceed the last bar's time.
    last_bar_time = conn.bars["time"].iloc[-1].to_pydatetime()
    assert new_t <= last_bar_time


def test_advance_bars_rejects_non_positive_n(tmp_path: Path) -> None:
    """advance_bars(0) or advance_bars(-1) raises ValueError."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    with pytest.raises(ValueError, match="must be positive"):
        conn.advance_bars(0)
    with pytest.raises(ValueError, match="must be positive"):
        conn.advance_bars(-1)


def test_advance_bars_clamps_to_end_of_data(tmp_path: Path) -> None:
    """advance_bars with a count larger than the dataset clamps to the last bar."""

    sample = _build_sample(tmp_path, n_bars=4)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    new_t = conn.advance_bars(1000)  # way past the end
    # The cursor must equal the last bar's time.
    assert new_t == conn.current_t
    visible = conn.get_rates("XAUUSD", "M1", count=100)
    assert len(visible) == 4


# --------------------------------------------------------------- get_ticks


def test_get_ticks_synthesizes_deterministic_ticks(tmp_path: Path) -> None:
    """When no tick source is loaded, get_ticks synthesizes 4 ticks per M1 bar."""

    sample = _build_sample(tmp_path, n_bars=3)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
    ticks = conn.get_ticks(
        "XAUUSD",
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
    )
    # 3 bars × 4 ticks/bar = 12 ticks.
    assert len(ticks) == 12
    # All ticks are XAUUSD.
    assert all(t.symbol == "XAUUSD" for t in ticks)
    # The bid-ask spread is positive (half-spread = 0.1% of bar range).
    assert all(t.ask >= t.bid for t in ticks)


def test_get_ticks_rejects_wrong_symbol(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    with pytest.raises(ValueError, match="XAUUSD"):
        conn.get_ticks(
            "EURUSD",
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        )


# --------------------------------------------------------------- positions / pending


def test_positions_get_empty_initially(tmp_path: Path) -> None:
    """A fresh connector has no positions."""

    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert conn.positions_get() == []


def test_positions_get_filter_by_wrong_symbol_returns_empty(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert conn.positions_get(symbol="EURUSD") == []


def test_pending_get_empty_initially(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert conn.pending_get() == []


def test_pending_get_filter_by_wrong_symbol_returns_empty(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert conn.pending_get(symbol="EURUSD") == []


def test_order_modify_updates_pending_order(tmp_path: Path) -> None:
    """order_modify changes the price/sl/tp of a pending order."""

    from xauusd_bot.connectors.schemas import OrderRequest, OrderSide, OrderType

    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    # Inject a pending order into the connector state.
    pending_id = "pend-1"
    conn._state.pending[pending_id] = OrderRequest(  # noqa: SLF001
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        volume=Decimal("0.10"),
        price=Decimal("2005.00"),
    )
    result = conn.order_modify(pending_id, price=2003.50, sl=1995.0, tp=2010.0)
    assert result.accepted is True
    modified = conn._state.pending[pending_id]  # noqa: SLF001
    assert modified.price == Decimal("2003.50")
    assert modified.sl == Decimal("1995.0")
    assert modified.tp == Decimal("2010.0")


def test_order_modify_unknown_order_returns_not_found(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    result = conn.order_modify("nope", price=2000.0)
    assert result.accepted is False
    assert result.error_code == "NOT_FOUND"


def test_order_send_returns_accepted_with_client_order_id(tmp_path: Path) -> None:
    """order_send on Replay records the order and returns an accepted result."""

    from xauusd_bot.connectors.schemas import OrderRequest, OrderSide, OrderType

    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    result = conn.order_send(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            volume=Decimal("0.10"),
            client_order_id="my-order-1",
        )
    )
    assert result.accepted is True
    assert result.order_id == "my-order-1"
    assert result.client_order_id == "my-order-1"


# --------------------------------------------------------------- account / status


def test_account_initial_balance_respected(tmp_path: Path) -> None:
    """The initial_balance constructor argument overrides the default 10000."""

    from decimal import Decimal

    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD", initial_balance=Decimal("5000"))
    acct = conn.get_account()
    assert acct.balance == Decimal("5000")
    assert acct.equity == Decimal("5000")
    assert acct.free_margin == Decimal("5000")


def test_account_server_time_matches_current_t(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=2)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    acct = conn.get_account()
    assert acct.server_time == conn.current_t


def test_is_connected_returns_true_for_replay(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert conn.is_connected() is True


def test_shutdown_is_noop(tmp_path: Path) -> None:
    sample = _build_sample(tmp_path, n_bars=1)
    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    # shutdown must return None and not raise.
    assert conn.shutdown() is None
    assert conn.shutdown() is None  # idempotent
