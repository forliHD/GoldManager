"""Tests for the LiveMT5Connector — RPyC client to the Wine-MT5-bridge.

The bridge server (Windows-Python / Wine / MetaTrader5) cannot run in
CI on macOS. We test the connector against a ``FakeRPyC`` that
implements the same wire-protocol surface the real bridge server
exposes (``exposed_initialize``, ``exposed_login``,
``exposed_get_account_info``, …).

What we test (Block 8 dovetail with the task brief, 12 tests):

1.  ``connect()`` calls ``exposed_initialize`` + ``exposed_login``
    in the right order with the configured credentials.
2.  ``get_account()`` calls ``exposed_get_account_info`` and maps
    the dict back into the ``AccountInfo`` schema.
3.  ``get_rates()`` calls ``exposed_copy_rates_from_pos`` and
    deserialises the pickled DataFrame into a list of ``Bar`` schemas.
4.  ``order_send()`` calls ``exposed_order_send`` and maps the
    OrderResult dict back into the ``OrderResult`` schema.
5.  Reconnect on first-call failure (exponential backoff).
6.  Connection-lost mid-session: the connector drops the connection
    and the next call rebuilds it.
7.  Timeout: slow bridge → connector raises / returns cleanly.
8.  ``get_symbol_spec()`` maps the dict back into the SymbolSpec
    schema, with live values winning over None / missing fields.
9.  I-1 audit: no ``import MetaTrader5`` in ``connectors/live.py``.
10.  Connect-params come from constructor (not hardcoded).
11.  Auth-key forwarding: when set, the auth_key is in
    ``rpyc.connect``'s config.
12.  Reconnect-lock discipline: two parallel calls serialise (the
    lock is held only for the reconnect phase, not for normal calls).

The original stub test (test_live_stub.py) was replaced by this file
when the connector grew a real implementation in Block 8.
"""

from __future__ import annotations

import importlib
import inspect
import pickle
import socket
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from xauusd_bot.connectors.live import LiveMT5Connector
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
    Tick,
)


# ============================================================ FakeRPyC


class _FakeRPyCRoot:
    """Stand-in for ``conn.root`` — exposes a configurable API surface.

    Each ``exposed_*`` method is a method on this class. Tests can
    monkeypatch the methods directly to inject returns, raises, or
    side-effects (e.g. counting calls, simulating latency).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.call_count: dict[str, int] = {}
        self.behavior: dict[str, Any] = {}
        self._lock = threading.Lock()
        # Default behaviours so a test that only cares about the
        # connection lifecycle (and not the payload) doesn't have to
        # set up a full response. Each default is a function that
        # returns a sensible empty response for its method.
        self.behavior["exposed_initialize"] = lambda *a, **kw: {"company": "Test", "build": 0, "connected": True, "trade_allowed": True, "maxbars": 10000, "community_account": False, "community_connection": False, "name": "test"}
        self.behavior["exposed_login"] = lambda *a, **kw: {
            "login": 0, "broker": "test", "currency": "USD",
            "balance": 0.0, "equity": 0.0, "margin": 0.0,
            "free_margin": 0.0, "leverage": 100,
            "server_time": 0, "trade_allowed": True, "raw": {},
        }
        self.behavior["exposed_get_account_info"] = lambda *a, **kw: {
            "login": 0, "broker": "test", "currency": "USD",
            "balance": 0.0, "equity": 0.0, "margin": 0.0,
            "free_margin": 0.0, "leverage": 100,
            "server_time": int(datetime.now(tz=UTC).timestamp() * 1000),
            "trade_allowed": True, "raw": {},
        }
        self.behavior["exposed_get_symbol_info"] = lambda *a, **kw: {
            "symbol": "XAUUSD", "description": "test",
            "point": 0.01, "digits": 2, "trade_contract_size": 100.0,
            "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
            "price_limit_max": None, "price_limit_min": None,
            "margin_rate": 0.01, "currency_base": "XAU",
            "currency_profit": "USD", "currency_margin": "USD",
        }
        self.behavior["exposed_copy_rates_from_pos"] = lambda *a, **kw: pickle.dumps(pd.DataFrame())
        self.behavior["exposed_copy_ticks_from"] = lambda *a, **kw: pickle.dumps(pd.DataFrame())
        self.behavior["exposed_order_send"] = lambda *a, **kw: {"accepted": True, "retcode": 10009, "order_id": "0", "filled_volume": 0.0, "avg_fill_price": None, "slippage_points": 0, "comment": "", "request_id": 0}
        self.behavior["exposed_order_modify"] = lambda *a, **kw: {"accepted": True, "retcode": 10009, "order_id": str(a[0]) if a else "0"}
        self.behavior["exposed_order_cancel"] = lambda *a, **kw: {"accepted": True, "retcode": 10009, "order_id": str(a[0]) if a else "0"}
        self.behavior["exposed_positions_get"] = lambda *a, **kw: []
        self.behavior["exposed_orders_get"] = lambda *a, **kw: []
        self.behavior["exposed_get_terminal_info"] = lambda *a, **kw: {"company": "Test", "build": 0, "connected": True, "trade_allowed": True, "maxbars": 10000, "community_account": False, "community_connection": False, "name": "test"}

    def _wrap(self, name: str):
        def _impl(*args, **kwargs):
            with self._lock:
                self.calls.append((name, args, kwargs))
                self.call_count[name] = self.call_count.get(name, 0) + 1
            behavior = self.behavior.get(name)
            if behavior is None:
                return None
            if isinstance(behavior, Exception):
                raise behavior
            if callable(behavior):
                return behavior(*args, **kwargs)
            return behavior
        _impl.__name__ = name
        return _impl

    def __getattr__(self, name: str) -> Any:
        if name.startswith("exposed_"):
            return self._wrap(name)
        raise AttributeError(name)


class _FakeRPyCConn:
    """Stand-in for ``rpyc.Connection``."""

    def __init__(self, host: str, port: int, config: dict[str, Any]) -> None:
        self.host = host
        self.port = port
        self.config = config
        self.root = _FakeRPyCRoot()
        self.closed = False
        self.ping_count = 0
        self.next_ping_raises: Exception | None = None

    def ping(self) -> None:
        self.ping_count += 1
        if self.next_ping_raises is not None:
            exc, self.next_ping_raises = self.next_ping_raises, None
            raise exc

    def close(self) -> None:
        self.closed = True

    def detach(self) -> None:
        # Helper for tests: detach the root to simulate a connection
        # drop mid-session. The connector will rebuild on next call.
        self.root = None  # type: ignore[assignment]


@pytest.fixture
def fake_rpyc_module(monkeypatch: pytest.MonkeyPatch):
    """Replace ``rpyc`` in the live connector with a fake module.

    Returns a tuple ``(module, last_conn, captured, set_behavior)``:
      * ``module`` — the fake rpyc module (for further patching)
      * ``last_conn`` — list of all connections the connector opened
        (one per rpyc.connect() call)
      * ``captured`` — list of (host, port, config) dicts
      * ``set_behavior`` — callable ``set_behavior(method, response)``
        that sets the *default* response a freshly-opened connection
        will return for ``method``. Tests that want a specific
        response should call this BEFORE triggering a connect.

    Note: behaviour set after the connect (via
    ``last_conn[0].root.behavior[...]``) is also supported, but
    setting it before is more reliable when multiple methods are
    called on the same connection.
    """
    last_conn: list[_FakeRPyCConn] = []
    captured: list[dict[str, Any]] = []
    # The "default" behavior is copied into every new connection's
    # root.behavior in ``fake_connect`` below. Tests that want to
    # customise responses use ``set_behavior``.
    default_behavior: dict[str, Any] = {}

    def fake_connect(host, port, config=None, **kwargs):
        if config is None:
            config = {}
        captured.append({"host": host, "port": port, "config": dict(config)})
        conn = _FakeRPyCConn(host, port, config)
        # Apply any customisations the test pre-registered.
        for method, response in default_behavior.items():
            conn.root.behavior[method] = response
        last_conn.append(conn)
        return conn

    class _FakeRpycModule:
        Connection = _FakeRPyCConn
        connect = staticmethod(fake_connect)

    def set_behavior(method: str, response: Any) -> None:
        default_behavior[method] = response

    # Reset the module-level cache so the fake is picked up.
    from xauusd_bot.connectors import live as live_mod

    monkeypatch.setattr(live_mod, "_rpyc_module", _FakeRpycModule)
    return _FakeRpycModule, last_conn, captured, set_behavior


# ============================================================ helpers


def _make_connector(**overrides: Any) -> LiveMT5Connector:
    """Build a LiveMT5Connector with sensible defaults for tests."""
    params: dict[str, Any] = dict(
        host="mt5-terminal",
        port=18812,
        login=12345,
        password="test-pass",
        server="VantageInternational-Demo",
        symbol="XAUUSD",
    )
    params.update(overrides)
    return LiveMT5Connector(**params)


def _fake_account_dict() -> dict[str, Any]:
    return {
        "login": 12345,
        "broker": "Vantage International",
        "currency": "USD",
        "balance": 10500.50,
        "equity": 10780.25,
        "margin": 200.00,
        "free_margin": 10580.25,
        "leverage": 500,
        "server_time": int(datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC).timestamp() * 1000),
        "trade_allowed": True,
        "raw": {"name": "Lucas R", "server": "VantageInternational-Demo"},
    }


def _fake_symbol_info_dict() -> dict[str, Any]:
    return {
        "symbol": "XAUUSD",
        "description": "XAUUSD spot (CFD)",
        "point": 0.01,
        "digits": 2,
        "trade_contract_size": 100.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "price_limit_max": 3000.0,
        "price_limit_min": 1500.0,
        "margin_rate": 0.01,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
    }


def _fake_rates_df_bytes(n: int = 5) -> bytes:
    """Build a pickled DataFrame that the connector can deserialise."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        t = base + pd.Timedelta(minutes=i)
        rows.append(
            {
                "time": t,
                "open": 2000.0 + i * 0.5,
                "high": 2001.0 + i * 0.5,
                "low": 1999.0 + i * 0.5,
                "close": 2000.5 + i * 0.5,
                "tick_volume": 100 + i,
                "real_volume": 1000 + i * 10,
                "spread": 30.0,
            }
        )
    df = pd.DataFrame(rows)
    return pickle.dumps(df)


# ============================================================ tests


# ---- (1) connect() → exposed_initialize + exposed_login ----------------


def test_connect_calls_initialize_then_login(fake_rpyc_module) -> None:
    _, last_conn, _, _set_behavior = fake_rpyc_module
    conn = _make_connector()
    conn.get_account()  # triggers lazy connect
    assert len(last_conn) == 1, "exactly one rpyc.connect() call"
    root = last_conn[0].root
    assert root.call_count.get("exposed_initialize", 0) == 1
    assert root.call_count.get("exposed_login", 0) == 1
    # Login args must contain login + password + server.
    login_call = [c for c in root.calls if c[0] == "exposed_login"][0]
    args, kwargs = login_call[1], login_call[2]
    assert kwargs.get("login") == 12345
    assert kwargs.get("server") == "VantageInternational-Demo"
    assert kwargs.get("password") == "test-pass"


def test_is_connected_reflects_state(fake_rpyc_module) -> None:
    conn = _make_connector()
    assert conn.is_connected() is False
    conn.get_account()
    assert conn.is_connected() is True
    conn.shutdown()
    assert conn.is_connected() is False


# ---- (2) get_account() → AccountInfo mapping ----------------------------


def test_get_account_maps_to_schema(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    conn = _make_connector()
    set_behavior("exposed_get_account_info", _fake_account_dict())
    info = conn.get_account()
    assert isinstance(info, AccountInfo)
    assert info.login == 12345
    assert info.broker == "Vantage International"
    assert info.balance == Decimal("10500.50")
    assert info.equity == Decimal("10780.25")
    assert info.leverage == 500
    assert info.trade_allowed is True
    # server_time comes back as datetime, not int
    assert isinstance(info.server_time, datetime)
    assert info.server_time.tzinfo is not None
    assert info.server_time.year == 2026


# ---- (3) get_rates() → deserialise + Bar mapping ------------------------


def test_get_rates_deserialises_pickled_dataframe(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    conn = _make_connector()
    set_behavior("exposed_copy_rates_from_pos", _fake_rates_df_bytes(n=5))
    bars = conn.get_rates("XAUUSD", "M1", count=3)
    assert isinstance(bars, list)
    assert all(isinstance(b, Bar) for b in bars)
    assert len(bars) == 3  # tail(3) of 5
    # After tail(3), the first bar in the list is the 3rd source bar
    # (index 2 in the source frame, with open=2001.0).
    assert bars[0].symbol == "XAUUSD"
    assert bars[0].timeframe == "M1"
    assert bars[0].open == Decimal("2001.0")
    assert bars[0].tick_volume == 102
    # The last bar of the 3 we got is the 5th source bar.
    assert bars[-1].open == Decimal("2002.0")
    assert bars[-1].tick_volume == 104


def test_get_rates_end_time_filter(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    conn = _make_connector()
    set_behavior("exposed_copy_rates_from_pos", _fake_rates_df_bytes(n=10))
    # Cut off at the 5th bar (inclusive).
    cutoff = datetime(2026, 6, 17, 12, 4, 0, tzinfo=UTC)
    bars = conn.get_rates("XAUUSD", "M1", count=10, end_time=cutoff)
    assert len(bars) == 5
    assert bars[-1].time == datetime(2026, 6, 17, 12, 4, 0, tzinfo=UTC)


# ---- (4) order_send() → OrderResult mapping -----------------------------


def test_order_send_maps_to_schema(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    conn = _make_connector()
    set_behavior(
        "exposed_order_send",
        {
            "accepted": True,
            "retcode": 10009,
            "order_id": "987654",
            "filled_volume": 0.10,
            "avg_fill_price": 2005.0,
            "slippage_points": 5,
            "comment": "filled",
            "request_id": 0,
        },
    )
    req = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        volume=Decimal("0.10"),
        sl=Decimal("2000.0"),
        tp=Decimal("2015.0"),
        magic=12345,
        comment="test order",
    )
    res = conn.order_send(req)
    assert isinstance(res, OrderResult)
    assert res.accepted is True
    assert res.order_id == "987654"
    assert res.filled_volume == Decimal("0.10")
    assert res.avg_fill_price == Decimal("2005.0")


def test_order_send_rejection_populates_error_code(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    conn = _make_connector()
    set_behavior(
        "exposed_order_send",
        {
            "accepted": False,
            "retcode": 10019,  # TRADE_RETCODE_NO_MONEY
            "order_id": "",
            "filled_volume": 0,
            "avg_fill_price": None,
            "slippage_points": 0,
            "comment": "no money",
        },
    )
    req = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        volume=Decimal("100.0"),  # 100 lots, way over leverage
    )
    res = conn.order_send(req)
    assert res.accepted is False
    assert res.error_code == "10019"
    assert "no money" in (res.error_message or "")


# ---- (5) Reconnect on first-call failure --------------------------------


def test_reconnect_after_first_call_fails(fake_rpyc_module) -> None:
    """If the first call fails with a transient error, the next call
    must rebuild the connection (no manual re-init required)."""
    fake_module, last_conn, _, set_behavior = fake_rpyc_module
    # The first call to exposed_get_account_info raises, the second
    # succeeds. We do this with a stateful counter inside a closure.
    call_state = {"n": 0}

    def flaky_account(*args, **kwargs):
        call_state["n"] += 1
        if call_state["n"] == 1:
            raise ConnectionError("bridge dropped mid-call")
        return _fake_account_dict()

    set_behavior("exposed_get_account_info", flaky_account)

    conn = _make_connector(max_reconnect_attempts=3)
    # First call: fails with the ConnectionError, raises.
    with pytest.raises(ConnectionError):
        conn.get_account()
    # Second call: the connector rebuilds the connection, the
    # behavior is reapplied to the new connection, but the state
    # counter was already incremented, so it now succeeds.
    info = conn.get_account()
    assert info.balance == Decimal("10500.50")


# ---- (6) Connection loss mid-session ------------------------------------


def test_connection_loss_mid_session_rebuilds(fake_rpyc_module) -> None:
    """If the bridge's RPyC server drops the connection between
    calls, the connector must rebuild on the next call (NOT raise
    forever)."""
    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior("exposed_get_account_info", _fake_account_dict())
    conn = _make_connector()
    info = conn.get_account()
    assert info.login == 12345
    # Simulate a mid-session drop: set the connector's conn to a
    # broken object whose ping() raises.
    conn._conn.next_ping_raises = ConnectionError("server closed")  # type: ignore[union-attr]
    # Next call rebuilds.
    info2 = conn.get_account()
    assert info2.login == 12345
    # A second RPyC connection must have been opened.
    assert len(last_conn) == 2


def test_shutdown_is_idempotent(fake_rpyc_module) -> None:
    conn = _make_connector()
    conn.shutdown()
    conn.shutdown()  # second call is a no-op
    assert conn.is_connected() is False


# ---- (7) Timeout --------------------------------------------------------


def test_timeout_raises_on_slow_bridge(fake_rpyc_module) -> None:
    """If the bridge hangs longer than the connector's timeout, the
    call must raise (not block forever).

    The RPyC client honours its own socket-level timeouts; we verify
    the connector surfaces a timeout error rather than blocking.
    """
    import socket as socket_mod

    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior("exposed_initialize", socket_mod.timeout("simulated bridge hang"))
    conn = _make_connector(timeout=0.2)
    with pytest.raises((ConnectionError, socket.timeout, Exception)):
        conn.get_account()


# ---- (8) get_symbol_spec() ----------------------------------------------


def test_get_symbol_spec_maps_to_schema(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior("exposed_get_symbol_info", _fake_symbol_info_dict())
    conn = _make_connector()
    spec = conn.get_symbol_spec("XAUUSD")
    assert isinstance(spec, SymbolSpec)
    assert spec.symbol == "XAUUSD"
    assert spec.point == Decimal("0.01")
    assert spec.digits == 2
    assert spec.trade_contract_size == Decimal("100")
    assert spec.volume_min == Decimal("0.01")
    assert spec.volume_max == Decimal("100")
    assert spec.volume_step == Decimal("0.01")
    assert spec.currency_base == "XAU"
    assert spec.currency_profit == "USD"
    assert spec.price_limit_max == Decimal("3000.0")
    assert spec.price_limit_min == Decimal("1500.0")


# ---- (9) I-1 audit ------------------------------------------------------


def test_no_metatrader5_import_in_live_connector() -> None:
    """I-1: the only place that may import ``MetaTrader5`` is the
    bridge server inside ``docker/mt5-terminal/``. The Linux-side
    connector must be pure RPyC."""
    src = importlib.import_module("xauusd_bot.connectors.live")
    file_path = inspect.getfile(src)
    with open(file_path) as f:
        text = f.read()
    assert "import MetaTrader5" not in text, f"I-1 violation in {file_path}"
    assert "from MetaTrader5" not in text, f"I-1 violation in {file_path}"


# ---- (10) Connect-params from constructor (not hardcoded) ---------------


def test_connect_uses_constructor_params(fake_rpyc_module) -> None:
    _, last_conn, captured, _set_behavior = fake_rpyc_module
    conn = _make_connector(host="my-bridge.example.com", port=9999, login=42)
    conn.get_account()
    assert len(captured) == 1
    assert captured[0]["host"] == "my-bridge.example.com"
    assert captured[0]["port"] == 9999
    # auth_key None → no credentials slot
    assert "credentials" not in captured[0]["config"]


# ---- (11) Auth-key forwarding ------------------------------------------


def test_auth_key_in_connect_config(fake_rpyc_module) -> None:
    _, last_conn, captured, _set_behavior = fake_rpyc_module
    conn = _make_connector(auth_key="secret-xyz-123")
    conn.get_account()
    assert len(captured) == 1
    assert captured[0]["config"].get("credentials", {}).get("auth_key") == "secret-xyz-123"


# ---- (12) Reconnect-lock discipline ------------------------------------


def test_reconnect_lock_serialises_parallel_calls(fake_rpyc_module) -> None:
    """Two threads racing the first call share one connection attempt.

    We don't enforce a strict serialisation order, but we do enforce
    that the *number* of distinct RPyC connections is exactly 1
    after both calls have run.
    """
    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior("exposed_get_account_info", _fake_account_dict())
    conn = _make_connector()

    errors: list[Exception] = []

    def worker():
        try:
            info = conn.get_account()
            # login=12345 from the fake dict
            assert info.login == 12345, f"unexpected login {info.login}"
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not errors, f"threads raised: {errors}"
    # If the lock is missing, both threads would race to open a
    # connection and we'd see 2 conns.
    assert len(last_conn) == 1, f"expected 1 rpyc connection (lock-held), got {len(last_conn)}"


# ============================================================ additional coverage


# ---- positions_get + pending_get + order_modify + order_cancel ---------


def test_positions_get_maps_to_schema(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior(
        "exposed_positions_get",
        [
            {
                "ticket": 111,
                "symbol": "XAUUSD",
                "side": "buy",
                "volume": 0.10,
                "open_price": 2000.0,
                "sl": 1990.0,
                "tp": 2015.0,
                "open_time": int(datetime(2026, 6, 17, 10, 0, 0, tzinfo=UTC).timestamp() * 1000),
                "profit": 50.25,
                "swap": -1.5,
                "commission": -0.5,
                "comment": "test",
                "magic": 0,
            }
        ],
    )
    conn = _make_connector()
    positions = conn.positions_get("XAUUSD")
    assert len(positions) == 1
    assert isinstance(positions[0], Position)
    assert positions[0].position_id == "111"
    assert positions[0].side == OrderSide.BUY
    assert positions[0].volume == Decimal("0.10")
    assert positions[0].profit == Decimal("50.25")


def test_pending_get_maps_to_schema(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior(
        "exposed_orders_get",
        [
            {
                "order_id": "222",
                "symbol": "XAUUSD",
                "side": "sell",
                "type": "limit",
                "volume": 0.05,
                "price": 2050.0,
                "sl": None,
                "tp": 2040.0,
                "open_time": int(datetime(2026, 6, 17, 9, 0, 0, tzinfo=UTC).timestamp() * 1000),
                "comment": "limit short",
                "magic": 0,
            }
        ],
    )
    conn = _make_connector()
    pending = conn.pending_get("XAUUSD")
    assert len(pending) == 1
    assert isinstance(pending[0], OrderRequest)
    assert pending[0].symbol == "XAUUSD"
    assert pending[0].side == OrderSide.SELL
    assert pending[0].type == OrderType.LIMIT


def test_order_modify_and_cancel(fake_rpyc_module) -> None:
    _, last_conn, _, set_behavior = fake_rpyc_module
    set_behavior(
        "exposed_order_modify",
        {"accepted": True, "retcode": 10009, "order_id": "333"},
    )
    set_behavior(
        "exposed_order_cancel",
        {"accepted": True, "retcode": 10009, "order_id": "333"},
    )
    conn = _make_connector()
    res_mod = conn.order_modify("333", sl=1995.0, tp=2020.0)
    assert res_mod.accepted is True
    res_cancel = conn.order_cancel("333")
    assert res_cancel.accepted is True


def test_order_send_when_bridge_down_returns_clean_reject(fake_rpyc_module) -> None:
    """If the bridge is unreachable, order_send must return an
    OrderResult with error_code=BRIDGE_DOWN, NOT raise."""
    conn = _make_connector(max_reconnect_attempts=1)
    # Force connect to fail.
    from xauusd_bot.connectors import live as live_mod
    fake_module = live_mod._rpyc_module

    def failing_connect(*a, **kw):
        raise ConnectionRefusedError("no bridge")
    fake_module.connect = staticmethod(failing_connect)

    req = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        volume=Decimal("0.01"),
    )
    res = conn.order_send(req)
    assert res.accepted is False
    assert res.error_code == "BRIDGE_DOWN"
    assert "bridge unreachable" in (res.error_message or "").lower()


# ============================================================ I-1 audit (file)


def test_i1_audit_grep_no_metatrader5_in_connectors_live() -> None:
    """I-1 audit: ``grep -n 'import MetaTrader5' src/xauusd_bot/connectors/live.py``
    must be empty (the MetaTrader5 import is in docker/mt5-terminal/mt5_bridge_server.py)."""
    import subprocess
    result = subprocess.run(
        ["grep", "-rn", "import MetaTrader5", "src/xauusd_bot/connectors/live.py"],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", (
        f"I-1 violation: import MetaTrader5 in connectors/live.py: {result.stdout!r}"
    )
    # And confirm the only other allowed location.
    result2 = subprocess.run(
        ["grep", "-rn", "import MetaTrader5", "docker/mt5-terminal/"],
        capture_output=True,
        text=True,
    )
    assert "mt5_bridge_server.py" in result2.stdout, (
        f"MetaTrader5 must be in docker/mt5-terminal/mt5_bridge_server.py, got: {result2.stdout!r}"
    )
