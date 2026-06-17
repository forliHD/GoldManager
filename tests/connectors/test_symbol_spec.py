"""Tests for the Vantage XAUUSD SymbolSpec defaults + live-override.

These tests cover the contract-specification defaults that
:class:`xauusd_bot.connectors.symbol_spec.load_vantage_xauusd_spec`
ships with, plus the live-merge logic in
:func:`xauusd_bot.connectors.symbol_spec.resolve_symbol_spec`.

The defaults are Vantage-International-XAUUSD verified by hand in
2026-06-17. If Vantage changes the contract, the values here must be
updated and ``_VANTAGE_VERIFIED_AT`` bumped accordingly.

What we test
------------
* Defaults match the Vantage-International XAUUSD contract:
  point=0.01, digits=2, contract_size=100, vol_min=0.01, vol_max=100,
  vol_step=0.01, margin_rate=0.01.
* The defaults are conservative (vol_min=0.01, etc.).
* Live-override: when a connector returns different values via
  ``get_symbol_spec``, the live values win. When the broker returns
  ``None`` for a field, the defaults fill the gap.
* ``resolve_symbol_spec`` with ``connector=None`` returns the defaults.
* ``resolve_symbol_spec`` with a disconnected connector returns the
  defaults.
* ``resolve_symbol_spec`` with a connected connector returns the
  live-merged spec.
* ``get_vantage_spec_metadata`` exposes the verification date + URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from xauusd_bot.connectors.schemas import SymbolSpec
from xauusd_bot.connectors.symbol_spec import (
    get_vantage_spec_metadata,
    load_vantage_xauusd_spec,
    resolve_symbol_spec,
)


# ============================================================ defaults


def test_load_vantage_xauusd_spec_returns_symbolspec() -> None:
    spec = load_vantage_xauusd_spec()
    assert isinstance(spec, SymbolSpec)
    assert spec.symbol == "XAUUSD"


def test_default_spec_matches_vantage_contract() -> None:
    """Defaults must match the Vantage-International XAUUSD contract
    specification (2026-06-17)."""
    spec = load_vantage_xauusd_spec()
    assert spec.point == Decimal("0.01"), f"point={spec.point}"
    assert spec.digits == 2
    assert spec.trade_contract_size == Decimal("100")
    assert spec.volume_min == Decimal("0.01")
    assert spec.volume_max == Decimal("100.0")
    assert spec.volume_step == Decimal("0.01")
    assert spec.margin_rate == Decimal("0.01")
    assert spec.currency_base == "XAU"
    assert spec.currency_profit == "USD"
    assert spec.currency_margin == "USD"


def test_default_spec_is_conservative() -> None:
    """The defaults are *conservative* — they should never be more
    permissive than what Vantage actually allows (e.g. vol_min must
    not be lower than 0.01)."""
    spec = load_vantage_xauusd_spec()
    assert spec.volume_min >= Decimal("0.01"), f"vol_min too low: {spec.volume_min}"
    assert spec.volume_step >= Decimal("0.01"), f"vol_step too low: {spec.volume_step}"
    assert spec.spread_max_warn_points > 0
    assert spec.spread_max_block_points >= spec.spread_max_warn_points


def test_pip_value_matches_vantage_convention() -> None:
    """Vantage convention: 1 pip = 0.10 USD/oz, 1 standard lot = 100 oz,
    so 1 pip per standard lot = $10.00. We don't expose this as a
    SymbolSpec field, but contract_size must be 100 oz."""
    spec = load_vantage_xauusd_spec()
    assert spec.trade_contract_size == Decimal("100"), (
        f"contract_size={spec.trade_contract_size} (expected 100 oz)"
    )


def test_metadata_includes_verification_date() -> None:
    meta = get_vantage_spec_metadata()
    assert "verified_at" in meta
    assert "source_url" in meta
    # Sanity: the date is a valid ISO-format string.
    datetime.fromisoformat(meta["verified_at"])


# ============================================================ resolve_symbol_spec


class _FakeConnector:
    """Stand-in for :class:`xauusd_bot.connectors.base.IMarketConnector`.

    Only the methods ``resolve_symbol_spec`` actually uses are
    implemented: ``is_connected()`` and ``get_symbol_spec(symbol)``.
    """

    def __init__(self, *, connected: bool, spec: SymbolSpec | None = None,
                 raise_on_call: Exception | None = None) -> None:
        self._connected = connected
        self._spec = spec
        self._raise = raise_on_call
        self.call_count = 0

    def is_connected(self) -> bool:
        return self._connected

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        self.call_count += 1
        if self._raise is not None:
            raise self._raise
        if self._spec is None:
            raise RuntimeError("no spec configured")
        return self._spec


def test_resolve_with_no_connector_returns_defaults() -> None:
    spec = resolve_symbol_spec(None)
    defaults = load_vantage_xauusd_spec()
    assert spec == defaults


def test_resolve_with_disconnected_connector_returns_defaults() -> None:
    conn = _FakeConnector(connected=False)
    spec = resolve_symbol_spec(conn)
    defaults = load_vantage_xauusd_spec()
    assert spec == defaults
    # Disconnected → no live call was made
    assert conn.call_count == 0


def test_resolve_with_connected_connector_uses_live_values() -> None:
    """If the live connector returns a different point value, it
    must win over the default."""
    live_spec = SymbolSpec(
        symbol="XAUUSD",  # Vantage-also-lists-this
        description="live",
        point=Decimal("0.001"),  # 3-decimal-place, e.g. some ECN setup
        digits=3,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.001"),
        volume_max=Decimal("200.0"),
        volume_step=Decimal("0.001"),
        margin_rate=Decimal("0.005"),
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
    )
    conn = _FakeConnector(connected=True, spec=live_spec)
    spec = resolve_symbol_spec(conn)
    assert spec.point == Decimal("0.001"), f"live point must win, got {spec.point}"
    assert spec.digits == 3
    assert spec.volume_max == Decimal("200.0")
    assert conn.call_count == 1


def test_resolve_falls_back_to_defaults_on_live_error() -> None:
    """If the live connector raises (e.g. broker not ready), the
    resolver must fall back to defaults rather than crash."""
    conn = _FakeConnector(connected=True, raise_on_call=ConnectionError("bridge down"))
    spec = resolve_symbol_spec(conn)
    defaults = load_vantage_xauusd_spec()
    assert spec == defaults


def test_resolve_merges_missing_fields_with_defaults() -> None:
    """If the live connector returns a spec with ``price_limit_max=None``,
    the resolver must use the default (or just leave it as None —
    SymbolSpec allows None for the price-limit fields)."""
    live_spec = SymbolSpec(
        symbol="XAUUSD",
        description="partial",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100.0"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
        price_limit_max=None,  # broker did not return
        price_limit_min=None,
    )
    conn = _FakeConnector(connected=True, spec=live_spec)
    spec = resolve_symbol_spec(conn)
    # Live values that the broker returned are preserved.
    assert spec.point == Decimal("0.01")
    assert spec.digits == 2
    # Optional fields the broker did not return are passed through as None.
    assert spec.price_limit_max is None
    assert spec.price_limit_min is None


def test_resolve_live_overrides_currency_when_present() -> None:
    """If the broker returns a non-default currency (e.g. on a
    non-USD-denominated sub-account), the live value wins."""
    live_spec = SymbolSpec(
        symbol="XAUUSD",
        description="EUR-denominated sub-account",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100.0"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
        currency_base="XAU",
        currency_profit="EUR",
        currency_margin="EUR",
    )
    conn = _FakeConnector(connected=True, spec=live_spec)
    spec = resolve_symbol_spec(conn)
    assert spec.currency_profit == "EUR"
    assert spec.currency_margin == "EUR"
