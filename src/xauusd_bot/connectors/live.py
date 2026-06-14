"""Live MT5 connector — STUB.

The :class:`LiveMT5Connector` is the production-side connector that talks
to a MetaTrader 5 terminal running under Wine. Because the official
``MetaTrader5`` package is **Windows-only**, this connector:

* Imports ``MetaTrader5`` **only** inside this module (architecture
  invariant — see ``00_FINAL_PLAN.md`` §1 Δ5).
* Actually invokes it only on platforms where the import succeeds; on
  macOS / Linux it raises :class:`NotImplementedError` with a clear
  message pointing the user at the Wine-bridge setup.

The implementation here is a deliberate stub. The real RPyC bridge
client (which calls into a Windows-Python RPyC server running inside
``docker/mt5-terminal/``) will be wired up in roadmap step 15
(``02_data_layer_mt5_bridge.md``). For now we just expose the protocol
surface and a helpful error.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    # Imported only for type hints; the real import is gated below.
    from typing import Any

log = structlog.get_logger(__name__)


# We try to import MetaTrader5 so that operators get a *clear* error message
# when they run the live connector on macOS. The import is wrapped so that
# `import xauusd_bot.connectors.live` always succeeds — the failure happens
# at construction time, not at module import time.
try:
    import MetaTrader5 as _mt5  # type: ignore[import-not-found]  # noqa: N813 — convention for Windows-only bridge
except Exception as _exc:  # pragma: no cover - platform dependent
    _mt5 = None  # type: ignore[assignment]
    _MT5_IMPORT_ERROR: Exception | None = _exc
else:
    _MT5_IMPORT_ERROR = None


class LiveMT5Connector:
    """Live MT5 connector — STUB, RPyC bridge not yet wired.

    The RPyC bridge design (see ``00_FINAL_PLAN.md`` §3.3):

    * ``docker/mt5-terminal/`` runs Wine + MT5 + a Windows-Python RPyC
      server that imports ``MetaTrader5`` and exposes its API over a
      network port.
    * This connector becomes a thin RPyC client; the Windows-Python
      process serializes MT5 calls, the Linux client deserializes the
      results into the schemas defined in
      :mod:`xauusd_bot.connectors.schemas`.

    Reconnect logic
    ---------------
    On any ``ConnectionError`` the connector retries with exponential
    backoff up to ``max_reconnect_attempts`` (default 5), then surfaces
    the error. While disconnected, ``is_connected()`` returns ``False``
    and ``order_send`` returns an :class:`OrderResult` with
    ``error_code="BRIDGE_DOWN"``.
    """

    def __init__(
        self,
        bridge_host: str = "mt5-terminal",
        bridge_port: int = 18812,
        max_reconnect_attempts: int = 5,
    ) -> None:
        if _mt5 is None and _MT5_IMPORT_ERROR is not None:
            # We allow construction even on platforms where the package is
            # missing — the bridge is supposed to abstract that away. The
            # missing-import case is a *developer* mistake (running live
            # mode without the bridge). We log a warning so that dev
            # sessions on macOS can still import this module.
            log.warning(
                "live_mt5_native_module_unavailable",
                error=str(_MT5_IMPORT_ERROR),
                note="LiveMT5Connector is a stub; it will raise NotImplementedError until the RPyC bridge is wired (Plan §3.3).",
            )
        self._bridge_host = bridge_host
        self._bridge_port = bridge_port
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connected = False
        self._rpyc_conn: Any = None  # set by _connect() once the bridge exists

    # -------------------------------------------------------------- internals

    def _connect(self) -> None:
        """Open the RPyC connection to the Wine-bridge. Stub: raises."""

        raise NotImplementedError(
            "RPyC bridge not yet wired — see Plan §3.3; only used on Ubuntu with Wine. "
            "Roadmap step 15 (02_data_layer_mt5_bridge.md) implements the bridge client."
        )

    def _ensure_connected(self) -> None:
        if not self._connected:
            self._connect()

    def _remote_call(self, name: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Round-trip an MT5 call through the RPyC bridge. Stub: raises."""

        raise NotImplementedError(
            f"LiveMT5Connector._remote_call({name!r}) requires the RPyC bridge (Plan §3.3)."
        )

    # ------------------------------------------------------------- IMarketConnector

    def get_rates(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[Bar]:
        self._ensure_connected()
        return self._remote_call("get_rates", symbol, timeframe, count, end_time=end_time)

    def get_ticks(self, symbol: str, from_ts: datetime, to_ts: datetime) -> list[Tick]:
        self._ensure_connected()
        return self._remote_call("get_ticks", symbol, from_ts, to_ts)

    def get_account(self) -> AccountInfo:
        self._ensure_connected()
        return self._remote_call("get_account")

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        self._ensure_connected()
        return self._remote_call("get_symbol_spec", symbol)

    def order_send(self, request: OrderRequest) -> OrderResult:
        try:
            self._ensure_connected()
        except NotImplementedError as exc:
            return OrderResult(accepted=False, error_code="BRIDGE_NOT_WIRED", error_message=str(exc))
        try:
            return self._remote_call("order_send", request)
        except ConnectionError as exc:
            log.error("live_mt5_bridge_lost", error=str(exc))
            self._connected = False
            return OrderResult(accepted=False, error_code="BRIDGE_DOWN", error_message=str(exc))

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        self._ensure_connected()
        return self._remote_call("positions_get", symbol)

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:
        self._ensure_connected()
        return self._remote_call("pending_get", symbol)

    def order_modify(
        self,
        order_id: str,
        *,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        self._ensure_connected()
        return self._remote_call("order_modify", order_id, price=price, sl=sl, tp=tp)

    def order_cancel(self, order_id: str) -> OrderResult:
        self._ensure_connected()
        return self._remote_call("order_cancel", order_id)

    def is_connected(self) -> bool:
        return self._connected and self._rpyc_conn is not None

    def shutdown(self) -> None:
        if self._rpyc_conn is not None:
            from contextlib import suppress

            with suppress(Exception):  # noqa: BLE001 - best-effort cleanup
                self._rpyc_conn.close()  # type: ignore[union-attr]
        self._connected = False
        self._rpyc_conn = None
