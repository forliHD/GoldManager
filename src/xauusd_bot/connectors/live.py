"""Live MT5 connector — RPyC client to the Wine-bridged MT5 terminal.

This is the production-side connector. The Windows-only
``MetaTrader5`` package lives **only** in the bridge server
(``docker/mt5-terminal/mt5_bridge_server.py``); this module speaks
strictly RPyC to that server. This is the canonical realisation of
architecture invariant I-1 (see ``AGENTS.md`` §3 I-1).

Wire-up
-------
1. The ``mt5-terminal`` container runs Wine + MT5 + Windows-Python.
   The bridge server (port 18812) is the only Windows-Python process
   that imports ``MetaTrader5``.
2. ``LiveMT5Connector`` opens an RPyC connection to that bridge, then
   forwards every :class:`IMarketConnector` method call into the
   corresponding ``exposed_*`` method. The bridge serialises MT5
   calls (it is not thread-safe) — see the ``threading.Lock`` in
   ``mt5_bridge_server.py``.

Reconnect logic
---------------
* First call to any Protocol method triggers ``connect()`` (lazy init).
* ``connect()`` does an exponential-backoff retry (3 attempts: 1s/2s/4s).
* If a call raises any ``rpyc.core.exception.*`` or socket error, the
  internal connection is set to ``None`` and the next call re-runs
  ``connect()``. This is the "connection-lost mid-session" recovery.
* ``order_send`` is special-cased: on bridge-down it returns
  ``OrderResult(accepted=False, error_code="BRIDGE_DOWN", ...)`` so
  the execution engine gets a clean reject (no crash).

Auth
----
If the bridge was started with ``MT5_BRIDGE_AUTH_KEY`` set, the
connector passes that key in the RPyC config's ``credentials`` slot.
The bridge checks it in ``on_connect`` and refuses the connection
on mismatch.
"""

from __future__ import annotations

import pickle
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderResult,
    OrderSide,
    Position,
    SymbolSpec,
    Tick,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import rpyc  # type: ignore[import-not-found]

log = structlog.get_logger(__name__)

_DEFAULT_BRIDGE_HOST = "mt5-terminal"
_DEFAULT_BRIDGE_PORT = 18812
_DEFAULT_TIMEOUT_S = 10.0
_MAX_RECONNECT_ATTEMPTS = 3
_RECONNECT_BACKOFF_S = (1.0, 2.0, 4.0)

# Optional: rpyc is a runtime dep on the prod stack only. We import
# lazily in ``_import_rpyc`` so a missing install on macOS dev boxes
# produces a clear ``RuntimeError`` at connect time, not at module
# import time.
_rpyc_module: Any | None = None


def _import_rpyc() -> Any:
    """Lazily import rpyc; raise a clear error if it isn't installed."""
    global _rpyc_module
    if _rpyc_module is None:
        try:
            import rpyc  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - platform dependent
            raise RuntimeError(
                f"rpyc is required for LiveMT5Connector (install with `pip install rpyc`): {exc}"
            ) from exc
        _rpyc_module = rpyc
    return _rpyc_module


class LiveMT5Connector(IMarketConnector):
    """RPyC client to the Wine-MT5-bridge.

    Parameters
    ----------
    host:
        Bridge server hostname. In the prod Docker stack this is the
        service name ``mt5-terminal`` (loopback Docker network).
    port:
        Bridge server port (default ``18812``).
    login, password, server:
        Vantage-MT5 credentials. Stored verbatim — never logged.
    symbol:
        The default symbol the connector assumes. Per-call symbol
        overrides are accepted on every read method.
    auth_key:
        Optional shared secret. If set, must match
        ``MT5_BRIDGE_AUTH_KEY`` on the bridge (Production hardening,
        see AGENTS.md §4h-6).
    timeout:
        Per-call timeout in seconds (default 10s).
    max_reconnect_attempts:
        Number of reconnect attempts before raising (default 3).
    """

    def __init__(
        self,
        *,
        host: str = _DEFAULT_BRIDGE_HOST,
        port: int = _DEFAULT_BRIDGE_PORT,
        login: int,
        password: str,
        server: str,
        symbol: str = "XAUUSD",
        auth_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_reconnect_attempts: int = _MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._login = int(login)
        self._password = password
        self._server = server
        self._symbol = symbol
        self._auth_key = auth_key
        self._timeout = float(timeout)
        self._max_reconnect_attempts = int(max_reconnect_attempts)
        self._conn: Any = None
        # Lock for reconnect-throttling; _connect is idempotent under
        # the lock, so two threads racing the first call share one
        # connection attempt.
        self._lock = threading.Lock()

    # ========================================================== IMarketConnector

    def get_rates(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[Bar]:
        """Return up to ``count`` M1 bars (oldest → newest) for ``symbol``.

        ``end_time`` is implemented as a post-fetch filter. The live
        bridge has no PIT cursor — we ask for ``count`` bars and
        truncate to ``end_time``. This is a deliberate trade-off; see
        AGENTS.md §4h-2 (no backtest-style PIT semantics on the live
        side).
        """
        if end_time is None:
            payload = self._call("exposed_copy_rates_from_pos", symbol, timeframe, 0, int(count))
        else:
            # Pull more bars than needed, then filter.
            over_fetch = max(int(count) * 4, 200)
            payload = self._call("exposed_copy_rates_from_pos", symbol, timeframe, 0, over_fetch)
        df = pickle.loads(payload) if payload else None
        if df is None or len(df) == 0:
            return []
        df = df.sort_values("time").reset_index(drop=True)
        if end_time is not None:
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=UTC)
            end_time = end_time.astimezone(UTC)
            df = df[df["time"] <= end_time]
        df = df.tail(int(count))
        out: list[Bar] = []
        for _, row in df.iterrows():
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=row["time"].to_pydatetime(),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    tick_volume=int(row.get("tick_volume", 0)),
                    real_volume=(int(row["real_volume"]) if "real_volume" in row and row.get("real_volume") == row.get("real_volume") else None),
                    spread=(Decimal(str(row["spread"])) if "spread" in row and row.get("spread") == row.get("spread") else None),
                )
            )
        return out

    def get_ticks(self, symbol: str, from_ts: datetime, to_ts: datetime) -> list[Tick]:
        # Live bridge has copy_ticks_from (count-based) but not a clean
        # range. We pull a chunk of ticks starting at from_ts and
        # filter by to_ts on the client. For most live use cases
        # (entry/exit monitoring) this is good enough.
        if from_ts.tzinfo is None:
            from_ts = from_ts.replace(tzinfo=UTC)
        if to_ts.tzinfo is None:
            to_ts = to_ts.replace(tzinfo=UTC)
        from_ms = int(from_ts.timestamp() * 1000)
        payload = self._call("exposed_copy_ticks_from", symbol, from_ms, 5000, "INFO")
        df = pickle.loads(payload) if payload else None
        if df is None or len(df) == 0:
            return []
        df = df[(df["time"] >= from_ms) & (df["time"] <= int(to_ts.timestamp() * 1000))]
        out: list[Tick] = []
        for _, row in df.iterrows():
            ts_ms = int(row["time_msc"] if "time_msc" in row else row["time"])
            out.append(
                Tick(
                    symbol=symbol,
                    time=datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC),
                    bid=Decimal(str(row["bid"])),
                    ask=Decimal(str(row["ask"])),
                    last=(Decimal(str(row["last"])) if "last" in row and row.get("last") == row.get("last") else None),
                    volume=int(row.get("volume", 0) or 0),
                    flags=int(row.get("flags", 0) or 0),
                )
            )
        return out

    def get_account(self) -> AccountInfo:
        info = self._call("exposed_get_account_info")
        server_time = info.get("server_time")
        if isinstance(server_time, (int, float)):
            server_time = datetime.fromtimestamp(server_time / 1000.0, tz=UTC)
        else:
            server_time = datetime.now(tz=UTC)
        return AccountInfo(
            login=info.get("login", 0),
            broker=info.get("broker", ""),
            currency=info.get("currency", "USD"),
            balance=Decimal(str(info.get("balance", 0))),
            equity=Decimal(str(info.get("equity", 0))),
            margin=Decimal(str(info.get("margin", 0))),
            free_margin=Decimal(str(info.get("free_margin", 0))),
            leverage=int(info.get("leverage", 100)),
            server_time=server_time,
            trade_allowed=bool(info.get("trade_allowed", True)),
            raw=info.get("raw", {}),
        )

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        info = self._call("exposed_get_symbol_info", symbol)
        return SymbolSpec(
            symbol=info["symbol"],
            description=info.get("description", ""),
            point=Decimal(str(info.get("point", 0.01))),
            digits=int(info.get("digits", 2)),
            trade_contract_size=Decimal(str(info.get("trade_contract_size", 100))),
            volume_min=Decimal(str(info.get("volume_min", 0.01))),
            volume_max=Decimal(str(info.get("volume_max", 100.0))),
            volume_step=Decimal(str(info.get("volume_step", 0.01))),
            price_limit_max=(Decimal(str(info["price_limit_max"])) if info.get("price_limit_max") else None),
            price_limit_min=(Decimal(str(info["price_limit_min"])) if info.get("price_limit_min") else None),
            margin_rate=Decimal(str(info.get("margin_rate", 0.01))),
            currency_base=info.get("currency_base", "XAU"),
            currency_profit=info.get("currency_profit", "USD"),
            currency_margin=info.get("currency_margin", "USD"),
        )

    def order_send(self, request: OrderRequest) -> OrderResult:
        try:
            self._ensure_connected()
        except Exception as exc:
            log.error("live_mt5_bridge_unreachable_on_send", error=str(exc))
            return OrderResult(
                accepted=False,
                error_code="BRIDGE_DOWN",
                error_message=f"bridge unreachable: {exc}",
            )
        try:
            payload = self._call(
                "exposed_order_send",
                {
                    "symbol": request.symbol,
                    "side": request.side.value if isinstance(request.side, OrderSide) else str(request.side),
                    "type": request.type.value,
                    "volume": float(request.volume),
                    "price": (float(request.price) if request.price is not None else None),
                    "sl": (float(request.sl) if request.sl is not None else None),
                    "tp": (float(request.tp) if request.tp is not None else None),
                    "deviation_points": request.deviation_points,
                    "magic": request.magic,
                    "comment": request.comment,
                    "time_in_force": request.time_in_force.value,
                },
            )
        except Exception as exc:
            log.error("live_mt5_order_send_failed", error=str(exc))
            self._conn = None  # mark connection lost
            return OrderResult(
                accepted=False,
                error_code="BRIDGE_ERROR",
                error_message=f"order_send failed: {exc}",
            )
        return OrderResult(
            accepted=bool(payload.get("accepted")),
            order_id=(str(payload["order_id"]) if payload.get("order_id") else None),
            client_order_id=request.client_order_id,
            filled_volume=Decimal(str(payload.get("filled_volume", 0))),
            avg_fill_price=(Decimal(str(payload["avg_fill_price"])) if payload.get("avg_fill_price") else None),
            slippage_points=(Decimal(str(payload["slippage_points"])) if payload.get("slippage_points") else None),
            error_code=(None if payload.get("accepted") else str(payload.get("retcode", "MT5_ERROR"))),
            error_message=(None if payload.get("accepted") else payload.get("comment") or "rejected by MT5"),
            raw=payload,
        )

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        items = self._call("exposed_positions_get", symbol)
        out: list[Position] = []
        for item in items:
            ts_ms = int(item.get("open_time", 0))
            out.append(
                Position(
                    position_id=str(item["ticket"]),
                    symbol=item["symbol"],
                    side=OrderSide(item["side"]),
                    volume=Decimal(str(item["volume"])),
                    open_price=Decimal(str(item["open_price"])),
                    sl=(Decimal(str(item["sl"])) if item.get("sl") else None),
                    tp=(Decimal(str(item["tp"])) if item.get("tp") else None),
                    open_time=datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC),
                    profit=Decimal(str(item.get("profit", 0))),
                    swap=Decimal(str(item.get("swap", 0))),
                    commission=Decimal(str(item.get("commission", 0))),
                    comment=item.get("comment", ""),
                    magic=int(item.get("magic", 0)),
                )
            )
        return out

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:
        items = self._call("exposed_orders_get", symbol)
        out: list[OrderRequest] = []
        for item in items:
            ts_ms = int(item.get("open_time", 0))
            out.append(
                OrderRequest(
                    symbol=item["symbol"],
                    side=OrderSide(item["side"]),
                    type=item["type"],
                    volume=Decimal(str(item["volume"])),
                    price=(Decimal(str(item["price"])) if item.get("price") else None),
                    sl=(Decimal(str(item["sl"])) if item.get("sl") else None),
                    tp=(Decimal(str(item["tp"])) if item.get("tp") else None),
                    magic=int(item.get("magic", 0)),
                    comment=item.get("comment", ""),
                    time_in_force="GTC",
                )
            )
        return out

    def order_modify(
        self,
        order_id: str,
        *,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        payload = self._call("exposed_order_modify", order_id, price=price, sl=sl, tp=tp)
        return OrderResult(
            accepted=bool(payload.get("accepted")),
            order_id=(str(payload["order_id"]) if payload.get("order_id") else None),
            error_code=(None if payload.get("accepted") else str(payload.get("retcode", "MT5_ERROR"))),
            error_message=(None if payload.get("accepted") else payload.get("comment") or "modify rejected"),
            raw=payload,
        )

    def order_cancel(self, order_id: str) -> OrderResult:
        payload = self._call("exposed_order_cancel", order_id)
        return OrderResult(
            accepted=bool(payload.get("accepted")),
            order_id=(str(payload["order_id"]) if payload.get("order_id") else None),
            error_code=(None if payload.get("accepted") else str(payload.get("retcode", "MT5_ERROR"))),
            error_message=(None if payload.get("accepted") else payload.get("comment") or "cancel rejected"),
            raw=payload,
        )

    def is_connected(self) -> bool:
        if self._conn is None:
            return False
        try:
            # rpyc.Connection exposes .ping(); anything else is a stale ref.
            self._conn.ping()
            return True
        except Exception:
            self._conn = None
            return False

    def shutdown(self) -> None:
        """Best-effort shutdown. Idempotent."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001 - best-effort
                log.debug("live_mt5_close_error", error=str(exc))
        self._conn = None

    # ============================================================ Internals

    def _ensure_connected(self) -> None:
        if self._conn is not None:
            try:
                self._conn.ping()
                return
            except Exception:
                self._conn = None
        self._connect()

    def _connect(self) -> None:
        """Open the RPyC connection, retry with exponential backoff."""
        rpyc = _import_rpyc()
        last_exc: Exception | None = None
        with self._lock:
            if self._conn is not None:
                return
            for attempt in range(self._max_reconnect_attempts):
                if attempt > 0:
                    backoff = _RECONNECT_BACKOFF_S[min(attempt - 1, len(_RECONNECT_BACKOFF_S) - 1)]
                    log.warning("live_mt5_reconnect_backoff", attempt=attempt, sleep_s=backoff)
                    time.sleep(backoff)
                try:
                    config: dict[str, Any] = {
                        "allow_all_attrs": True,
                        "allow_setattr": False,
                        "allow_pickle": True,
                    }
                    if self._auth_key:
                        config["credentials"] = {"auth_key": self._auth_key}
                    conn = rpyc.connect(
                        self._host,
                        self._port,
                        config=config,
                    )
                    # Health check + initialize + login.
                    conn.ping()
                    conn.root.exposed_initialize()
                    conn.root.exposed_login(
                        login=self._login, password=self._password, server=self._server
                    )
                    self._conn = conn
                    log.info(
                        "live_mt5_connected",
                        host=self._host,
                        port=self._port,
                        server=self._server,
                        login=self._login,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    log.warning("live_mt5_connect_attempt_failed", attempt=attempt, error=str(exc))
                    continue
            raise ConnectionError(
                f"could not connect to MT5 bridge at {self._host}:{self._port} after "
                f"{self._max_reconnect_attempts} attempts: {last_exc}"
            )

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Round-trip an MT5 call through the bridge, with reconnect-on-fail.

        On any RPyC / socket error, drops the connection so the next
        call re-establishes it. We do NOT auto-retry inside the call:
        the caller's higher-level loop is the right place for retry
        policy (e.g. one retry on transient broker rejects, but
        re-raises on hard config errors).
        """
        self._ensure_connected()
        try:
            # Use ``getattr`` (not ``__getattribute__``) so that
            # rpyc's netref ``__getattr__`` proxy is honoured. With
            # the wire-protocol fake we use in tests, ``__getattribute__``
            # would not fall through to ``__getattr__``; ``getattr``
            # is the canonical way to invoke a named method on a
            # possibly-proxied object.
            root = self._conn.root
            fn = getattr(root, method, None)
            if fn is None:
                raise AttributeError(f"bridge root has no method {method!r}")
            return fn(*args, **kwargs)
        except Exception as exc:
            # Most RPyC errors expose `_conn` via their context. Drop the
            # connection so the next call rebuilds it.
            log.warning("live_mt5_call_failed", method=method, error=str(exc))
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
            raise
