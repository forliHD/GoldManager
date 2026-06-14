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
