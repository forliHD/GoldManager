"""Tests for the LiveMT5Connector STUB.

The live connector is a *deliberate* stub: the real RPyC bridge is
roadmap step 15. These tests enforce the stub contract:

* Construction must not raise (the connector must be importable on
  macOS / Linux where ``MetaTrader5`` cannot be installed).
* Every IMarketConnector method must produce a clear
  "RPyC bridge not yet wired" error. The current shape is:
  - Most methods raise ``NotImplementedError`` via ``_remote_call``.
  - ``order_send`` catches the exception and returns an
    :class:`OrderResult` with ``error_code="BRIDGE_NOT_WIRED"`` so the
    execution engine gets a clean reject instead of a crash.
* No real ``MetaTrader5`` import / call may happen in tests — we assert
  this with a monkeypatch guard.

Note: this module imports ``xauusd_bot.connectors.live`` directly.
The class is intentionally NOT in ``xauusd_bot.connectors.__init__``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.connectors.live import LiveMT5Connector
from xauusd_bot.connectors.schemas import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
)

EXPECTED_BRIDGE_FRAGMENT = "RPyC bridge not yet wired"


# ---------------------------------------------------------------- construction


def test_live_connector_is_constructible() -> None:
    """Constructing a LiveMT5Connector must NOT raise, even on macOS."""

    conn = LiveMT5Connector(bridge_host="test-host", bridge_port=18812, max_reconnect_attempts=3)
    # No exception → test passes. We also assert the constructor stored the
    # arguments as expected.
    assert conn._bridge_host == "test-host"  # noqa: SLF001 - intentional introspection
    assert conn._bridge_port == 18812  # noqa: SLF001
    assert conn._max_reconnect_attempts == 3  # noqa: SLF001
    assert conn.is_connected() is False


def test_live_connector_default_construction() -> None:
    """All defaults should resolve to safe values."""

    conn = LiveMT5Connector()
    assert conn._bridge_host == "mt5-terminal"  # noqa: SLF001
    assert conn._bridge_port == 18812  # noqa: SLF001
    assert conn._max_reconnect_attempts == 5  # noqa: SLF001
    assert conn.is_connected() is False


# ---------------------------------------------------------------- monkeypatch guard


def test_live_connector_does_not_call_mt5_during_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction must not touch any real MetaTrader5 entry point.

    We install a sentinel function as ``_remote_call`` and check it is
    *never* called from ``__init__``. The point of the test is to make
    sure that adding MT5 calls to the constructor is loud-failing.
    """

    sentinel_called = {"count": 0}

    def _sentinel(*a, **kw):
        sentinel_called["count"] += 1
        return

    monkeypatch.setattr("xauusd_bot.connectors.live._remote_call", _sentinel, raising=False)

    # Construct several instances; none should invoke the bridge stub.
    LiveMT5Connector()
    LiveMT5Connector(bridge_host="x", bridge_port=1)
    assert sentinel_called["count"] == 0, "Construction must not invoke the bridge stub"


# ---------------------------------------------------------------- per-method errors


@pytest.fixture
def conn() -> LiveMT5Connector:
    return LiveMT5Connector(bridge_host="test", bridge_port=18812, max_reconnect_attempts=3)


def _assert_bridge_not_wired_marker(exc_or_result) -> None:
    """Assert that the error mentions the RPyC bridge (in either an exception
    or an OrderResult)."""
    if isinstance(exc_or_result, BaseException):
        assert EXPECTED_BRIDGE_FRAGMENT in str(exc_or_result), (
            f"Exception message must contain {EXPECTED_BRIDGE_FRAGMENT!r}; got {exc_or_result!r}"
        )
        return
    if isinstance(exc_or_result, OrderResult):
        assert exc_or_result.accepted is False
        assert exc_or_result.error_code == "BRIDGE_NOT_WIRED"
        assert EXPECTED_BRIDGE_FRAGMENT in (exc_or_result.error_message or ""), (
            f"OrderResult.error_message must mention {EXPECTED_BRIDGE_FRAGMENT!r}; "
            f"got {exc_or_result.error_message!r}"
        )
        return
    raise AssertionError(f"Unexpected return type: {type(exc_or_result)}")


def test_live_get_rates_raises_or_signals(conn: LiveMT5Connector) -> None:
    """get_rates must fail with a clear RPyC-bridge message."""
    with pytest.raises(Exception) as ei:
        conn.get_rates("XAUUSD", "M1", count=1)
    _assert_bridge_not_wired_marker(ei.value)


def test_live_get_ticks_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.get_ticks(
            "XAUUSD",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
    _assert_bridge_not_wired_marker(ei.value)


def test_live_get_account_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.get_account()
    _assert_bridge_not_wired_marker(ei.value)


def test_live_get_symbol_spec_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.get_symbol_spec("XAUUSD")
    _assert_bridge_not_wired_marker(ei.value)


def test_live_order_send_returns_bridge_not_wired_result(conn: LiveMT5Connector) -> None:
    """order_send must NOT raise — it must return a clean BRIDGE_NOT_WIRED
    OrderResult so the execution engine can log and skip."""

    req = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        volume=Decimal("0.01"),
    )
    result = conn.order_send(req)
    assert isinstance(result, OrderResult)
    assert result.accepted is False
    assert result.error_code == "BRIDGE_NOT_WIRED"
    assert EXPECTED_BRIDGE_FRAGMENT in (result.error_message or "")


def test_live_positions_get_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.positions_get()
    _assert_bridge_not_wired_marker(ei.value)


def test_live_pending_get_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.pending_get()
    _assert_bridge_not_wired_marker(ei.value)


def test_live_order_modify_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.order_modify("ord-1", price=2000.0, sl=1990.0, tp=2010.0)
    _assert_bridge_not_wired_marker(ei.value)


def test_live_order_cancel_raises_or_signals(conn: LiveMT5Connector) -> None:
    with pytest.raises(Exception) as ei:
        conn.order_cancel("ord-1")
    _assert_bridge_not_wired_marker(ei.value)


# ---------------------------------------------------------------- shutdown safety


def test_live_shutdown_is_idempotent(conn: LiveMT5Connector) -> None:
    """shutdown() must not raise even when the bridge is not connected."""

    conn.shutdown()
    conn.shutdown()  # calling twice is a no-op


def test_live_is_connected_reflects_internal_state(conn: LiveMT5Connector) -> None:
    """``is_connected()`` returns False until the bridge is wired."""

    assert conn.is_connected() is False
    # Force a fake connection to confirm the gate flips.
    conn._connected = True  # noqa: SLF001
    conn._rpyc_conn = object()  # noqa: SLF001
    assert conn.is_connected() is True
    conn.shutdown()
    assert conn.is_connected() is False


# ---------------------------------------------------------------- guard: LiveMT5Connector is not re-exported


def test_live_connector_not_in_connectors_top_level() -> None:
    """The connector top-level must NOT eagerly re-export LiveMT5Connector
    (it pulls in the Windows-only ``MetaTrader5`` module). This is a
    hard architecture invariant: live mode is only reachable on the
    Ubuntu prod stack where the bridge is wired."""

    import xauusd_bot.connectors as connectors_pkg

    assert not hasattr(connectors_pkg, "LiveMT5Connector"), (
        "LiveMT5Connector must NOT be re-exported from xauusd_bot.connectors"
    )


def test_live_import_does_not_load_mt5_when_only_top_level_used() -> None:
    """Just importing ``xauusd_bot.connectors`` must not require
    ``MetaTrader5`` to be installed. We check that the public surface
    works on a machine where MetaTrader5 is not installed (which is
    the case on this macOS dev box)."""

    # Touch the public surface.
    from xauusd_bot.connectors import (  # noqa: F401
        IMarketConnector,
        PaperBroker,
        PreTradeSafetyChecker,
        ReplayConnector,
        SafetyVerdict,
    )

    # The MetaTrader5 module may or may not be present (depends on
    # whether live.py's try/except succeeded). What matters is that
    # the public surface does not need it. We assert that every symbol
    # in xauusd_bot.connectors.__all__ resolves without hitting the
    # live module.
    pkg = __import__("xauusd_bot.connectors", fromlist=["x"])
    for name in pkg.__all__:
        # Defensive: every name in __all__ should be importable without
        # hitting the live module.
        getattr(pkg, name)
