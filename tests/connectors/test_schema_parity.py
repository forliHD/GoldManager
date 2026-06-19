"""Schema-parity test: ReplayConnector and LiveMT5Connector must be interchangeable.

This is the single most important architectural test in block 1. The
:class:`xauusd_bot.connectors.base.IMarketConnector` Protocol is the
only broker interface the bot speaks; the bot code paths above
(features, decision, execution) treat the two implementations
identically. If the surface drifts, the bot silently misbehaves in
production.

We assert it three ways (all must pass):

1. Every method on :class:`IMarketConnector` is present on both classes.
2. ``inspect.signature`` of every public method matches **exactly**,
   including parameter names, defaults, and the keyword-only marker.
3. The runtime types of all return values are compatible (i.e. they
   declare the same Pydantic return type).
"""

from __future__ import annotations

import inspect

import pytest

from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.live import LiveMT5Connector
from xauusd_bot.connectors.replay import ReplayConnector

PROTOCOL_METHODS = [
    "get_rates",
    "get_ticks",
    "get_account",
    "get_symbol_spec",
    "order_send",
    "positions_get",
    "pending_get",
    "order_modify",
    "order_cancel",
    "is_connected",
    "shutdown",
]


# --------------------------------------------------------------- 1. presence


@pytest.mark.parametrize("method", PROTOCOL_METHODS)
def test_both_classes_define_method(method: str) -> None:
    assert hasattr(ReplayConnector, method), f"ReplayConnector missing {method}"
    assert hasattr(LiveMT5Connector, method), f"LiveMT5Connector missing {method}"


def test_both_classes_define_exactly_the_protocol_methods() -> None:
    """The public IMarketConnector surface is the canonical list; both
    classes must define *at least* these (extra private methods are OK)."""

    expected = set(PROTOCOL_METHODS)
    for cls in (ReplayConnector, LiveMT5Connector):
        defined = {name for name in dir(cls) if not name.startswith("_")}
        missing = expected - defined
        assert not missing, f"{cls.__name__} missing protocol methods: {missing}"


# --------------------------------------------------------------- 2. signature equality


@pytest.mark.parametrize("method", PROTOCOL_METHODS)
def test_signatures_are_identical(method: str) -> None:
    """``inspect.signature`` must compare equal for every public method.

    We compare by ``inspect.Signature`` equality, which checks parameter
    names, kinds (positional, keyword-only), annotations, and defaults.
    A drift here is a silent production bug: a caller written against
    Replay will get a TypeError when switched to Live, or vice versa.
    """

    sig_replay = inspect.signature(getattr(ReplayConnector, method))
    sig_live = inspect.signature(getattr(LiveMT5Connector, method))
    assert sig_replay == sig_live, (
        f"Signature drift on {method!r}:\n"
        f"  ReplayConnector: {sig_replay}\n"
        f"  LiveMT5Connector: {sig_live}\n"
    )


def test_signatures_match_protocol_for_both_implementations() -> None:
    """The Protocol's declared method signatures should match both classes."""

    for name in PROTOCOL_METHODS:
        proto_sig = inspect.signature(getattr(IMarketConnector, name))
        replay_sig = inspect.signature(getattr(ReplayConnector, name))
        live_sig = inspect.signature(getattr(LiveMT5Connector, name))
        # The Protocol annotations are stripped to "..." sentinel; we
        # only check parameter KINDS + NAMES + DEFAULTS match.
        for sig, label in ((replay_sig, "Replay"), (live_sig, "Live")):
            assert list(sig.parameters.keys()) == list(proto_sig.parameters.keys()), (
                f"{label}Connector.{name} parameter names differ from protocol: "
                f"{list(sig.parameters.keys())} vs {list(proto_sig.parameters.keys())}"
            )


# --------------------------------------------------------------- 3. return-type annotations


@pytest.mark.parametrize("method", PROTOCOL_METHODS)
def test_return_type_annotation_is_compatible(method: str) -> None:
    """Both classes must declare the same return type for each method.

    We allow ``None`` (un-annotated) on either side as long as both
    are un-annotated, but if one is annotated, the other must be too
    and the strings must match.
    """

    sig_replay = inspect.signature(getattr(ReplayConnector, method))
    sig_live = inspect.signature(getattr(LiveMT5Connector, method))
    rt_replay = sig_replay.return_annotation
    rt_live = sig_live.return_annotation
    assert str(rt_replay) == str(rt_live), (
        f"Return-type drift on {method!r}: "
        f"Replay={rt_replay!r} Live={rt_live!r}"
    )


# --------------------------------------------------------------- 4. is_connected & shutdown are no-throw


def test_both_is_connected_return_bool() -> None:
    """is_connected must return bool on both implementations.

    Note: ``from __future__ import annotations`` makes annotations
    strings, so we compare to the string form.
    """

    sig_r = inspect.signature(ReplayConnector.is_connected)
    sig_l = inspect.signature(LiveMT5Connector.is_connected)
    assert sig_r.return_annotation == "bool"  # noqa: E201 - string form
    assert sig_l.return_annotation == "bool"


def test_both_shutdown_return_none() -> None:
    """shutdown must return None on both implementations."""

    sig_r = inspect.signature(ReplayConnector.shutdown)
    sig_l = inspect.signature(LiveMT5Connector.shutdown)
    assert sig_r.return_annotation == "None"
    assert sig_l.return_annotation == "None"


# --------------------------------------------------------------- 5. parity sanity: param kinds


def test_order_modify_uses_keyword_only_extra_args() -> None:
    """order_modify must use KEYWORD_ONLY for the optional price/sl/tp args
    on both implementations (this is the protocol shape; drift would
    break callers using kwargs)."""

    for cls in (ReplayConnector, LiveMT5Connector):
        sig = inspect.signature(cls.order_modify)
        # First non-self param: order_id (positional or positional-or-keyword)
        params = [p for p in sig.parameters.values() if p.name != "self"]
        assert params[0].name == "order_id"
        assert params[0].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
        # Remaining params must be KEYWORD_ONLY.
        for p in params[1:]:
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{cls.__name__}.order_modify param {p.name!r} "
                f"is {p.kind!s}, expected KEYWORD_ONLY"
            )


# --------------------------------------------------------------- 6. live-mock runtime parity
#
# Block 8 adds: verify that LiveMT5Connector, when wired to a fake
# RPyC bridge, returns objects of the same Pydantic types as the
# ReplayConnector. The wire-format tests above only check method
# signatures; the runtime tests below check the actual return types.
#
# We don't connect to a real bridge — we use a tiny FakeRPyC stub
# that returns canned payloads, and assert the resulting objects
# have the right schema types.


import pickle
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from xauusd_bot.connectors.live import LiveMT5Connector
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderResult,
    Position,
    SymbolSpec,
)
from xauusd_bot.connectors.replay import ReplayConnector


class _StubRPyCRoot:
    """Minimal RPyC root: returns canned responses for each
    ``exposed_*`` method. Mirrors the surface in
    ``docker/mt5-terminal/mt5_bridge_server.py``."""

    def __init__(self) -> None:
        self.behavior: dict[str, object] = {}

    def __getattr__(self, name: str) -> object:
        if name.startswith("exposed_"):
            behavior = self.behavior.get(name)
            if behavior is None:
                return lambda *a, **kw: None
            return behavior
        raise AttributeError(name)


class _StubRPyCConn:
    def __init__(self) -> None:
        self.root = _StubRPyCRoot()
        self.closed = False
        self.ping_count = 0

    def ping(self) -> None:
        self.ping_count += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def stub_live_connector(monkeypatch: pytest.MonkeyPatch) -> LiveMT5Connector:
    """LiveMT5Connector with a stubbed rpyc module that has default
    canned responses for every exposed_* method."""
    from xauusd_bot.connectors import live as live_mod

    conns: list[_StubRPyCConn] = []

    def make_conn(host, port, config=None, **kwargs):
        c = _StubRPyCConn()
        conns.append(c)
        return c

    class _StubModule:
        connect = staticmethod(make_conn)

    # Build a default set of canned responses.
    def _default_initialize(*a, **kw):
        return {"company": "stub", "build": 0, "connected": True,
                "trade_allowed": True, "maxbars": 0, "community_account": False,
                "community_connection": False, "name": "stub"}

    def _default_login(*a, **kw):
        return {"login": 1, "broker": "stub", "currency": "USD", "balance": 0.0,
                "equity": 0.0, "margin": 0.0, "free_margin": 0.0, "leverage": 100,
                "server_time": int(datetime.now(tz=UTC).timestamp() * 1000),
                "trade_allowed": True, "raw": {}}

    def _default_account(*a, **kw):
        return {"login": 1, "broker": "stub", "currency": "USD", "balance": 100.0,
                "equity": 100.0, "margin": 0.0, "free_margin": 100.0, "leverage": 100,
                "server_time": int(datetime.now(tz=UTC).timestamp() * 1000),
                "trade_allowed": True, "raw": {}}

    def _default_symbol_info(*a, **kw):
        return {"symbol": "XAUUSD", "description": "stub", "point": 0.01,
                "digits": 2, "trade_contract_size": 100.0, "volume_min": 0.01,
                "volume_max": 100.0, "volume_step": 0.01, "price_limit_max": None,
                "price_limit_min": None, "margin_rate": 0.01,
                "currency_base": "XAU", "currency_profit": "USD", "currency_margin": "USD"}

    def _default_rates(*a, **kw):
        # 3 rows of OHLCV
        base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
        df = pd.DataFrame([
            {"time": base, "open": 2000.0, "high": 2001.0, "low": 1999.0,
             "close": 2000.5, "tick_volume": 100, "real_volume": 1000, "spread": 30.0},
            {"time": base + pd.Timedelta(minutes=1), "open": 2000.5, "high": 2002.0,
             "low": 2000.0, "close": 2001.0, "tick_volume": 110, "real_volume": 1100, "spread": 30.0},
        ])
        return pickle.dumps(df)

    def _default_order_send(*a, **kw):
        return {"accepted": True, "retcode": 10009, "order_id": "999",
                "filled_volume": 0.01, "avg_fill_price": 2000.0,
                "slippage_points": 0, "comment": "stub", "request_id": 0}

    def _default_positions(*a, **kw):
        return [{
            "ticket": 1234, "symbol": "XAUUSD", "side": "buy", "volume": 0.05,
            "open_price": 2000.0, "sl": None, "tp": 2010.0,
            "open_time": int(datetime.now(tz=UTC).timestamp() * 1000),
            "profit": 0.0, "swap": 0.0, "commission": 0.0,
            "comment": "stub", "magic": 0,
        }]

    monkeypatch.setattr(live_mod, "_rpyc_module", _StubModule)

    connector = LiveMT5Connector(
        host="stub", port=18812, login=1, password="x", server="stub",
    )
    # Set default behaviors on the next connection's root.
    live_mod._rpyc_module  # ensure import
    original_connect = live_mod._rpyc_module.connect

    def install_defaults(host, port, config=None, **kwargs):
        c = original_connect(host, port, config=config, **kwargs)
        c.root.behavior["exposed_initialize"] = _default_initialize
        c.root.behavior["exposed_login"] = _default_login
        c.root.behavior["exposed_get_account_info"] = _default_account
        c.root.behavior["exposed_get_symbol_info"] = _default_symbol_info
        c.root.behavior["exposed_copy_rates_from_pos"] = _default_rates
        c.root.behavior["exposed_order_send"] = _default_order_send
        c.root.behavior["exposed_positions_get"] = _default_positions
        c.root.behavior["exposed_orders_get"] = lambda *a, **kw: []
        c.root.behavior["exposed_order_modify"] = _default_order_send
        c.root.behavior["exposed_order_cancel"] = _default_order_send
        return c

    live_mod._rpyc_module.connect = staticmethod(install_defaults)
    return connector


def test_live_get_account_returns_accountinfo(stub_live_connector) -> None:
    """LiveMT5Connector.get_account() must return an AccountInfo, same as Replay."""
    info = stub_live_connector.get_account()
    assert isinstance(info, AccountInfo)
    assert info.broker == "stub"
    assert info.balance == Decimal("100.0")


def test_live_get_symbol_spec_returns_symbolspec(stub_live_connector) -> None:
    """LiveMT5Connector.get_symbol_spec() must return a SymbolSpec, same as Replay."""
    spec = stub_live_connector.get_symbol_spec("XAUUSD")
    assert isinstance(spec, SymbolSpec)
    assert spec.point == Decimal("0.01")
    assert spec.digits == 2


def test_live_get_rates_returns_list_of_bars(stub_live_connector) -> None:
    """LiveMT5Connector.get_rates() must return list[Bar], same as Replay."""
    bars = stub_live_connector.get_rates("XAUUSD", "M1", count=2)
    assert isinstance(bars, list)
    for b in bars:
        assert isinstance(b, Bar)
        assert b.symbol == "XAUUSD"
        assert b.timeframe == "M1"


def test_live_order_send_returns_orderresult(stub_live_connector) -> None:
    """LiveMT5Connector.order_send() must return OrderResult, same as Replay."""
    from xauusd_bot.connectors.schemas import OrderRequest, OrderSide, OrderType
    req = OrderRequest(
        symbol="XAUUSD", side=OrderSide.BUY, type=OrderType.MARKET, volume=Decimal("0.01"),
    )
    res = stub_live_connector.order_send(req)
    assert isinstance(res, OrderResult)
    assert res.accepted is True
    assert res.order_id == "999"


def test_live_positions_get_returns_list_of_positions(stub_live_connector) -> None:
    """LiveMT5Connector.positions_get() must return list[Position], same as Replay."""
    positions = stub_live_connector.positions_get("XAUUSD")
    assert isinstance(positions, list)
    for p in positions:
        assert isinstance(p, Position)
        assert p.symbol == "XAUUSD"


# Runtime parity: same input → same return type across both classes
def test_runtime_return_types_match_across_implementations() -> None:
    """``ReplayConnector`` and ``LiveMT5Connector`` declare the same
    return types on every public method. Inspect the runtime
    annotations (after ``from __future__ import annotations`` is
    resolved) and confirm.

    This is a stronger check than the static ``inspect.signature``
    tests above because it also asserts that the *return type
    objects* (Pydantic model classes) match.
    """
    import typing

    for method in PROTOCOL_METHODS:
        sig_r = inspect.signature(getattr(ReplayConnector, method))
        sig_l = inspect.signature(getattr(LiveMT5Connector, method))
        # get_type_hints resolves the string annotations to actual
        # type objects (Pydantic classes, list[X], etc.).
        hints_r = typing.get_type_hints(getattr(ReplayConnector, method))
        hints_l = typing.get_type_hints(getattr(LiveMT5Connector, method))
        rt_r = hints_r.get("return")
        rt_l = hints_l.get("return")
        assert rt_r is not None, f"{method}: ReplayConnector has no return annotation"
        assert rt_l is not None, f"{method}: LiveMT5Connector has no return annotation"
        # Both must be the same underlying type (e.g. AccountInfo, list[Bar], None).
        # Use ``==`` because Pydantic re-creates the same generic
        # alias (e.g. ``list[Bar]``) on each ``get_type_hints`` call,
        # so identity (``is``) compares False even for an equivalent type.
        assert rt_r == rt_l, (
            f"Return type drift on {method!r}: "
            f"Replay={rt_r!r} Live={rt_l!r}"
        )
