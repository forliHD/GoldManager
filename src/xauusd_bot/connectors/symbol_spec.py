"""Vantage XAUUSD contract defaults + live-override helper.

This module is the **single source of truth** for the static parts of
the XAUUSD contract on the Vantage International demo account. The
:class:`xauusd_bot.connectors.symbol_spec.load_vantage_xauusd_spec`
function returns a fully-populated :class:`SymbolSpec` based on the
contract specification Lucas has agreed with Vantage.

Why hardcoded defaults?
    * The :class:`ReplayConnector` (dev / backtest) does not have access
      to a live ``mt5.symbol_info()`` call — it has to bootstrap with
      realistic numbers so the order-sizing and risk code paths can run.
    * In production, the :class:`LiveMT5Connector` can query the live
      ``symbol_info`` and override the defaults. ``resolve_symbol_spec``
      does that override automatically.

Source for the defaults
-----------------------
Verified against Vantage International's "Contract Specifications"
page (``https://www.vantagemarkets.com/contract-specifications/``) and
the MT5 terminal's own ``symbol_info`` output for the demo account.
The defaults below reflect the values Lucas confirmed as canonical
during the 2026-06-17 spec-review:

* **Symbol name:** ``XAUUSD`` (Vantage also lists it as
  ``XAUUSD.r``/``XAUUSDm`` on some servers; the connector does
  symbol-name discovery via ``exposed_get_symbols()`` and picks the
  first match, see :class:`xauusd_bot.connectors.live`).
* **Point:** ``0.01`` (2-decimal-place CFD; 0.1 of a cent is the
  smallest tick).
* **Digits:** ``2``.
* **Contract size:** ``100 oz`` per standard lot.
* **Pip value per standard lot:** ``$10.00`` (1 pip = 0.10 USD/oz
  × 100 oz/lot = $10). For a mini-lot of 0.1 → $1.00, for a
  micro-lot of 0.01 → $0.10.
* **Leverage:** ``1:500`` on the demo account (Live: 1:100 typical,
  configurable).
* **Min/Max/Step volume:** ``0.01`` / ``100.0`` / ``0.01`` lots.

Re-verification: if Vantage changes the contract, update
``_VANTAGE_XAUUSD_DEFAULTS`` here and re-run ``test_symbol_spec.py``.
The ``source_url`` + ``verified_at`` fields are diagnostic-only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from xauusd_bot.connectors.schemas import SymbolSpec

if TYPE_CHECKING:  # pragma: no cover
    from xauusd_bot.connectors.base import IMarketConnector

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- constants
#
# Verified against vantage international contract specifications
# (https://www.vantagemarkets.com/contract-specifications/) and the
# mt5.symbol_info() output for the live demo account on 2026-06-17.
# update verified_at when re-validating.
_VANTAGE_VERIFIED_AT = "2026-06-17"
_VANTAGE_SOURCE_URL = (
    "https://www.vantagemarkets.com/contract-specifications/"
    " (cross-checked with mt5.symbol_info() on VantageInternational-Demo)"
)


def _vantage_xauusd_defaults() -> SymbolSpec:
    """Return the canonical Vantage-International XAUUSD spec."""
    return SymbolSpec(
        symbol="XAUUSD",
        description="XAUUSD spot (CFD) — Vantage International demo",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),  # 100 oz per standard lot
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100.0"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
        # Conservative safety thresholds for the gold CFD:
        # * Warn at >50 points (5 pips) — typical spread on quiet
        #   markets is 10-30 points.
        # * Block at >120 points (12 pips) — anything wider usually
        #   means illiquidity or news.
        spread_max_warn_points=50,
        spread_max_block_points=120,
    )


def load_vantage_xauusd_spec() -> SymbolSpec:
    """Return the hardcoded Vantage-XAUUSD defaults.

    Use this in the dev / backtest path where no live MT5 connection
    is available. The values are conservative (typical Vantage demo
    conditions) and re-verified by hand in June 2026.

    For the production path, use :func:`resolve_symbol_spec` instead,
    which queries the live connector and overrides the defaults.
    """
    log.info(
        "vantage_xauusd_spec_loaded",
        source_url=_VANTAGE_SOURCE_URL,
        verified_at=_VANTAGE_VERIFIED_AT,
    )
    return _vantage_xauusd_defaults()


# ============================================================== live override


def resolve_symbol_spec(
    connector: IMarketConnector | None,
    symbol: str = "XAUUSD",
) -> SymbolSpec:
    """Return the live-queried SymbolSpec, falling back to defaults.

    If ``connector`` is connected (``is_connected()``), call
    :meth:`IMarketConnector.get_symbol_spec` and use that as the
    primary source. Fields that the broker doesn't return (or returns
    as ``None``) are filled in from the Vantage defaults.

    If ``connector`` is ``None`` or not connected, return the Vantage
    defaults verbatim. This is the dev / backtest / smoke path.

    The merge is **per-field**, not per-record: even on a connected
    connector, fields like ``currency_base`` come from the defaults
    when the broker returns ``None`` (some brokers omit margin
    currency when it equals profit currency). This keeps the result
    non-nullable for the order-sizer.
    """
    if connector is None or not connector.is_connected():
        return load_vantage_xauusd_spec()
    try:
        live = connector.get_symbol_spec(symbol)
    except Exception as exc:
        log.warning(
            "vantage_xauusd_live_query_failed_using_defaults",
            error=str(exc),
            symbol=symbol,
        )
        return load_vantage_xauusd_spec()
    defaults = _vantage_xauusd_defaults()
    # Per-field merge: live wins when present, defaults fill the gaps.
    merged = SymbolSpec(
        symbol=live.symbol or defaults.symbol,
        description=live.description or defaults.description,
        point=live.point or defaults.point,
        digits=int(live.digits or defaults.digits),
        trade_contract_size=live.trade_contract_size or defaults.trade_contract_size,
        volume_min=live.volume_min or defaults.volume_min,
        volume_max=live.volume_max or defaults.volume_max,
        volume_step=live.volume_step or defaults.volume_step,
        price_limit_max=(live.price_limit_max if live.price_limit_max is not None else defaults.price_limit_max),
        price_limit_min=(live.price_limit_min if live.price_limit_min is not None else defaults.price_limit_min),
        margin_rate=(live.margin_rate if live.margin_rate else defaults.margin_rate),
        currency_base=live.currency_base or defaults.currency_base,
        currency_profit=live.currency_profit or defaults.currency_profit,
        currency_margin=live.currency_margin or defaults.currency_margin,
        spread_max_warn_points=defaults.spread_max_warn_points,
        spread_max_block_points=defaults.spread_max_block_points,
    )
    log.info(
        "vantage_xauusd_spec_resolved",
        symbol=symbol,
        point=str(merged.point),
        contract_size=str(merged.trade_contract_size),
        source=("live" if live.point else "defaults"),
    )
    return merged


# ============================================================== diagnostics


def get_vantage_spec_metadata() -> dict[str, str]:
    """Diagnostic: who verified the spec, when, and from which URL.

    Used by the review agent (Block 5c) to surface a "last verified"
    stamp in the daily/weekly review report.
    """
    return {
        "verified_at": _VANTAGE_VERIFIED_AT,
        "source_url": _VANTAGE_SOURCE_URL,
        "verified_today": (date.today().isoformat() == _VANTAGE_VERIFIED_AT),
    }
