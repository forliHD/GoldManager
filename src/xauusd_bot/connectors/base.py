"""``IMarketConnector`` protocol — the only broker interface the bot speaks.

Two implementations exist:

* :class:`xauusd_bot.connectors.replay.ReplayConnector` — reads Parquet / CSV
  and replays bar-by-bar. No Windows / Wine dependency. Used in dev + backtest.
* :class:`xauusd_bot.connectors.live.LiveMT5Connector` — RPyC client to the
  Wine-bridged MT5 terminal. Only used on the Ubuntu prod stack.

Both must return **identical schemas** (``Bar``, ``Tick``, ``SymbolSpec``,
``AccountInfo``, ``OrderResult``, ``Position``). That contract is what
``tests/test_schemas.py`` enforces.

Notes
-----
The connector layer is the **only** place that may import the Windows-only
``MetaTrader5`` package. See ``connectors/live.py`` for the import guard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)


@runtime_checkable
class IMarketConnector(Protocol):
    """The connector protocol — all broker / data-feed access goes through this."""

    # ------------------------------------------------------------------ data

    def get_rates(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[Bar]:
        """Return up to ``count`` historical bars, oldest → newest.

        Parameters
        ----------
        symbol:
            Instrument symbol, e.g. ``"XAUUSD"``.
        timeframe:
            One of ``"M1"``, ``"M5"``, ``"H1"``, ``"D1"``, ...
        count:
            Maximum number of bars to return.
        end_time:
            If set, return bars whose ``time`` is strictly less than this
            cutoff. Live MT5 ignores it (returns the most recent ``count``
            bars); ReplayConnector **enforces** it for point-in-time
            correctness.
        """
        ...

    def get_ticks(
        self,
        symbol: str,
        from_ts: datetime,
        to_ts: datetime,
    ) -> list[Tick]:
        """Return ticks for ``[from_ts, to_ts]`` (both inclusive)."""
        ...

    # ---------------------------------------------------------------- account

    def get_account(self) -> AccountInfo:
        """Return the current account snapshot."""
        ...

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        """Return the static ``SymbolSpec`` for ``symbol``."""
        ...

    # --------------------------------------------------------------- trading

    def order_send(self, request: OrderRequest) -> OrderResult:
        """Submit an order. Returns an :class:`OrderResult` (filled or rejected)."""
        ...

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        """Return currently open positions (optionally filtered by symbol)."""
        ...

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:
        """Return pending orders (LIMIT/STOP). Not yet supported by all brokers."""
        ...

    def order_modify(
        self,
        order_id: str,
        *,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """Modify an existing pending order. Returns success/failure."""
        ...

    def order_cancel(self, order_id: str) -> OrderResult:
        """Cancel a pending order."""
        ...

    # ---------------------------------------------------------------- health

    def is_connected(self) -> bool:
        """Return True if the connector is healthy (feed online, bridge alive)."""
        ...

    def shutdown(self) -> None:
        """Tear down any background tasks / connections. Idempotent."""
        ...
