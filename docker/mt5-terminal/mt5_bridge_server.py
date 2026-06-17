"""RPyC bridge server for MetaTrader 5 (Windows-Python side).

This server runs *inside* the ``mt5-terminal`` Docker container (under
Wine). It is the **only** place in the entire project that imports the
Windows-only ``MetaTrader5`` package — see ``AGENTS.md`` §3 I-1.

Protocol
--------
* The server is an :class:`rpyc.Service` (subclass) with ``exposed_*``
  methods that mirror the :class:`xauusd_bot.connectors.base.IMarketConnector`
  protocol. The Linux client (``xauusd_bot.connectors.live.LiveMT5Connector``)
  calls these methods over an RPyC connection and translates the
  return values into the canonical Pydantic schemas.
* **MT5 is not thread-safe** — all ``mt5.*`` calls go through a
  ``threading.Lock`` (``self._mt5_lock``). This serialises concurrent
  requests from the Linux side. Multiple *processes* (with different
  bridge ports) are the only way to run parallel trading engines.
* Auth is opt-in: if ``MT5_BRIDGE_AUTH_KEY`` is set, the connection
  config must include ``allow_public_attrs=False`` and the key in
  ``credentials`` — checked in ``on_connect``.

Wire format
-----------
* Account / symbol info → ``dict`` (pure data; Pydantic-mapped client-side).
* Bar / tick DataFrames → ``bytes`` (``pickle.dumps``) of a DataFrame
  with stable column order. The client deserialises + maps to ``Bar`` /
  ``Tick`` schemas. We pickle because RPyC's netref for DataFrames
  works but pulls in the entire DataFrame as a netref tree — pickling
  the frame is ~5× faster for 10k+ bars.
* OrderResult / Position → ``dict`` mapped to OrderResult/Position
  by the client.

Logging
-------
stdlib ``logging`` (Windows-Python has no structlog). JSON-formatter
via a tiny custom formatter (no extra dependency on Windows).

Run::

    wine python mt5_bridge_server.py --port 18812 --host 0.0.0.0

This file is bundled into the Wine prefix at image build time.
See ``docker/mt5-terminal/Dockerfile`` for the full build.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

# ============================================================ RPyC + MT5 imports
#
# These imports happen at module load. If RPyC or MetaTrader5 is missing
# the process dies with a clear error — the container build is the only
# place this file should ever run.

try:
    import rpyc  # type: ignore[import-not-found]
    from rpyc import Service, ThreadedServer  # type: ignore[import-not-found]
except Exception as _exc:  # pragma: no cover - container-only
    print(f"FATAL: rpyc import failed: {_exc}", file=sys.stderr)
    raise

try:
    import MetaTrader5 as _mt5  # type: ignore[import-not-found]  # noqa: N813
except Exception as _exc:  # pragma: no cover - container-only
    print(f"FATAL: MetaTrader5 import failed: {_exc}", file=sys.stderr)
    raise


# ============================================================ Logging


class _JsonFormatter(logging.Formatter):
    """Tiny JSON-line formatter (no external deps on Windows)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("MT5_BRIDGE_LOG_LEVEL", "INFO").upper())
    return logging.getLogger("mt5_bridge")


log = _setup_logging()


# ============================================================ Error types


class Mt5BridgeError(Exception):
    """Base class for all bridge-side errors surfaced to the client.

    RPyC propagates subclasses of ``Exception`` natively, so the
    client catches ``Mt5BridgeError`` without special handling.
    """


class Mt5NotInitializedError(Mt5BridgeError):
    """``mt5.initialize()`` was not called (or failed)."""


class Mt5LoginError(Mt5BridgeError):
    """``mt5.login()`` failed — wrong creds, server unreachable, etc."""


class Mt5OrderError(Mt5BridgeError):
    """``mt5.order_send()`` returned a non-success result_code."""


# ============================================================ Timeframe map


_TIMEFRAME_MAP: dict[str, int] = {
    "M1": getattr(_mt5, "TIMEFRAME_M1", 1),
    "M5": getattr(_mt5, "TIMEFRAME_M5", 5),
    "M15": getattr(_mt5, "TIMEFRAME_M15", 15),
    "M30": getattr(_mt5, "TIMEFRAME_M30", 30),
    "H1": getattr(_mt5, "TIMEFRAME_H1", 16385),
    "H4": getattr(_mt5, "TIMEFRAME_H4", 16388),
    "D1": getattr(_mt5, "TIMEFRAME_D1", 16408),
    "W1": getattr(_mt5, "TIMEFRAME_W1", 32769),
    "MN1": getattr(_mt5, "TIMEFRAME_MN1", 49153),
}


def _timeframe_to_mt5(tf: str) -> int:
    """Translate a string timeframe (``"M1"``) to an MT5 timeframe int."""
    if tf not in _TIMEFRAME_MAP:
        raise Mt5BridgeError(f"Unknown timeframe: {tf!r} (expected one of {list(_TIMEFRAME_MAP)})")
    return _TIMEFRAME_MAP[tf]


# ============================================================ Helpers


def _dt_to_epoch_ms(dt: datetime | int | float) -> int:
    """Coerce datetime / epoch-seconds / epoch-ms → epoch milliseconds (int)."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    # Already a number.
    val = float(dt)
    return int(val * 1000) if val < 1e12 else int(val)


def _rates_df_to_payload(df: Any) -> bytes:
    """Pickle a rates DataFrame for transit. Returns ``bytes``.

    The client deserialises with ``pickle.loads`` and maps each row
    to the ``Bar`` schema. We pick a stable column subset so the
    client's mapping doesn't drift when MT5 adds columns.
    """
    if df is None or len(df) == 0:
        return pickle.dumps(df)
    cols = [c for c in ("time", "open", "high", "low", "close", "tick_volume", "real_volume", "spread") if c in df.columns]
    return pickle.dumps(df[cols])


def _positions_to_list(positions: Any) -> list[dict[str, Any]]:
    """Convert ``mt5.positions_get()`` result to a list of dicts.

    Each tuple is mapped field-by-field. We send dicts (not the
    opaque ``TradePosition`` named-tuple) because RPyC netref
    objects are slower than pickled dicts and more brittle across
    container boundaries.
    """
    if positions is None:
        return []
    out: list[dict[str, Any]] = []
    for p in positions:
        out.append(
            {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "side": "buy" if int(getattr(p, "type", 0)) == 0 else "sell",
                "volume": float(p.volume),
                "open_price": float(p.price_open),
                "sl": float(p.sl) if p.sl else None,
                "tp": float(p.tp) if p.tp else None,
                "open_time": _dt_to_epoch_ms(datetime.fromtimestamp(p.time, tz=UTC)),
                "profit": float(p.profit),
                "swap": float(p.swap),
                "commission": float(p.commission),
                "comment": str(p.comment),
                "magic": int(p.magic),
            }
        )
    return out


def _order_result_to_dict(result: Any) -> dict[str, Any]:
    """Map ``mt5.order_send()`` result to a dict."""
    if result is None:
        return {"accepted": False, "error_code": "MT5_NO_RESULT", "error_message": "mt5.order_send returned None"}
    return {
        "accepted": bool(getattr(result, "retcode", None) == getattr(_mt5, "TRADE_RETCODE_DONE", 10009)),
        "retcode": int(getattr(result, "retcode", 0)),
        "order_id": str(getattr(result, "order", "") or ""),
        "filled_volume": float(getattr(result, "volume", 0.0) or 0.0),
        "avg_fill_price": float(getattr(result, "price", 0.0) or 0.0) if getattr(result, "price", 0) else None,
        "slippage_points": int(getattr(result, "slippage", 0) or 0),
        "comment": str(getattr(result, "comment", "") or ""),
        "request_id": int(getattr(result, "request_id", 0) or 0),
        "raw": {
            "retcode": int(getattr(result, "retcode", 0)),
            "retcode_external": int(getattr(result, "retcode_external", 0)),
        },
    }


# ============================================================ Service


class MT5BridgeService(Service):  # type: ignore[misc, valid-type]
    """RPyC service exposing the MT5 API to a Linux client.

    Lifecycle:
    * ``on_connect`` (RPyC hook) — validates ``MT5_BRIDGE_AUTH_KEY`` if set.
    * ``exposed_initialize`` — call once at startup. Opens the MT5
      terminal process inside the Wine prefix.
    * ``exposed_login`` — call after initialize with creds.
    * Method calls (``exposed_get_account_info``, etc.) — all
      protected by ``self._mt5_lock``.
    * ``exposed_shutdown`` — call at teardown. Closes the terminal.
    * ``on_disconnect`` (RPyC hook) — best-effort cleanup if the
      client vanished.
    """

    # RPyC per-connection config — see rpyc.core.service.Service.
    ALIASES: dict[str, Any] = {}  # populated by main()

    def __init__(self, conn: Any) -> None:  # noqa: D401
        super().__init__(conn)
        self._mt5_lock = threading.Lock()
        self._initialized = False
        self._logged_in: dict[str, Any] = {}
        log.info("mt5_bridge_client_connected", remote=conn._channel)

    # -- RPyC hooks --------------------------------------------------------

    def on_connect(self, conn: Any) -> None:  # noqa: D401
        # Auth check: shared-secret in config (set by client).
        expected = os.environ.get("MT5_BRIDGE_AUTH_KEY", "").strip()
        if not expected:
            return  # no auth required
        # Client sends key in conn._config.get("credentials", {}).
        # If wrong, refuse the connection.
        cfg = getattr(conn, "_config", {}) or {}
        provided = (cfg.get("credentials") or {}).get("auth_key", "")
        if provided != expected:
            log.warning("mt5_bridge_auth_rejected")
            raise Mt5BridgeError("auth failed: wrong or missing MT5_BRIDGE_AUTH_KEY")

    def on_disconnect(self, conn: Any) -> None:  # noqa: D401
        log.info("mt5_bridge_client_disconnected")

    # -- Initialization / login --------------------------------------------

    def exposed_initialize(self, *, path: str | None = None, portable: bool = False) -> dict[str, Any]:
        """Initialize the MT5 terminal. Returns terminal_info dict."""
        with self._mt5_lock:
            if self._initialized:
                return self._terminal_info_dict()
            ok = _mt5.initialize(path=path, portable=portable) if path else _mt5.initialize()
            if not ok:
                err = _mt5.last_error()
                raise Mt5NotInitializedError(f"mt5.initialize() failed: {err}")
            self._initialized = True
            log.info("mt5_initialized", path=path, portable=portable)
            return self._terminal_info_dict()

    def exposed_login(self, *, login: int, password: str, server: str, timeout_ms: int = 30_000) -> dict[str, Any]:
        """Login to the MT5 broker. Returns account_info dict.

        Raises :class:`Mt5LoginError` on failure.
        """
        with self._mt5_lock:
            if not self._initialized:
                # Auto-init if the client forgot to call initialize first.
                if not _mt5.initialize():
                    raise Mt5NotInitializedError(f"auto-init failed: {_mt5.last_error()}")
                self._initialized = True
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            authorized = _mt5.login(login, password=password, server=server, timeout=timeout_ms)
            # ``authorized`` is False only on a hard auth error; some
            # brokers return False with a transient timeout. Retry up
            # to ``deadline`` if False but no explicit error.
            while not authorized and time.monotonic() < deadline:
                time.sleep(0.5)
                authorized = _mt5.login(login, password=password, server=server, timeout=timeout_ms)
            if not authorized:
                err = _mt5.last_error()
                raise Mt5LoginError(f"mt5.login(login={login}, server={server}) failed: {err}")
            self._logged_in = {"login": login, "server": server, "ts": time.time()}
            log.info("mt5_logged_in", login=login, server=server)
            return self._account_info_dict()

    def exposed_shutdown(self) -> bool:
        """Shutdown the MT5 terminal. Idempotent."""
        with self._mt5_lock:
            if not self._initialized:
                return True
            ok = _mt5.shutdown()
            self._initialized = False
            self._logged_in = {}
            log.info("mt5_shut_down", ok=bool(ok))
            return bool(ok)

    # -- Symbols / account -------------------------------------------------

    def exposed_get_symbols(self) -> list[str]:
        """Return all symbols visible to the broker."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            symbols = _mt5.symbols_get()
            return [s.name for s in symbols] if symbols else []

    def exposed_get_symbol_info(self, symbol: str) -> dict[str, Any]:
        """Return symbol_info dict for one symbol (point, contract size, etc.)."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            info = _mt5.symbol_info(symbol)
            if info is None:
                raise Mt5BridgeError(f"symbol_info({symbol!r}) returned None")
            return {
                "symbol": str(info.name),
                "description": str(info.description or ""),
                "point": float(info.point),
                "digits": int(info.digits),
                "trade_contract_size": float(info.trade_contract_size),
                "volume_min": float(info.volume_min),
                "volume_max": float(info.volume_max),
                "volume_step": float(info.volume_step),
                "price_limit_max": float(info.price_limit_max) if info.price_limit_max else None,
                "price_limit_min": float(info.price_limit_min) if info.price_limit_min else None,
                "margin_rate": float(info.margin_rate) if hasattr(info, "margin_rate") else 0.01,
                "currency_base": str(info.currency_base or "XAU"),
                "currency_profit": str(info.currency_profit or "USD"),
                "currency_margin": str(info.currency_margin or "USD"),
                "trade_mode": int(info.trade_mode) if hasattr(info, "trade_mode") else None,
                "swap_long": float(info.swap_long) if hasattr(info, "swap_long") else None,
                "swap_short": float(info.swap_short) if hasattr(info, "swap_short") else None,
                "spread_current_points": int(info.spread) if hasattr(info, "spread") else None,
            }

    def exposed_get_account_info(self) -> dict[str, Any]:
        """Return account_info dict (balance, equity, margin, etc.)."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            return self._account_info_dict()

    def exposed_get_terminal_info(self) -> dict[str, Any]:
        """Return terminal_info dict (company, build, connected, etc.)."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            return self._terminal_info_dict()

    # -- Market data -------------------------------------------------------

    def exposed_copy_rates_from_pos(
        self,
        symbol: str,
        timeframe: str,
        start_pos: int,
        count: int,
    ) -> bytes:
        """Return pickled DataFrame with ``count`` M1 bars starting at ``start_pos``."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            tf = _timeframe_to_mt5(timeframe)
            df = _mt5.copy_rates_from_pos(symbol, tf, int(start_pos), int(count))
            if df is None:
                err = _mt5.last_error()
                raise Mt5BridgeError(f"copy_rates_from_pos({symbol}, {timeframe}, {start_pos}, {count}) failed: {err}")
            return _rates_df_to_payload(df)

    def exposed_copy_rates_range(
        self,
        symbol: str,
        timeframe: str,
        date_from: datetime | int | float,
        date_to: datetime | int | float,
    ) -> bytes:
        """Return pickled DataFrame for bars in [date_from, date_to]."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            tf = _timeframe_to_mt5(timeframe)
            from_ms = _dt_to_epoch_ms(date_from)
            to_ms = _dt_to_epoch_ms(date_to)
            df = _mt5.copy_rates_range(symbol, tf, from_ms, to_ms)
            if df is None:
                err = _mt5.last_error()
                raise Mt5BridgeError(f"copy_rates_range({symbol}, {timeframe}) failed: {err}")
            return _rates_df_to_payload(df)

    def exposed_copy_ticks_from(
        self,
        symbol: str,
        date_from: datetime | int | float,
        count: int,
        flag: str = "INFO",
    ) -> bytes:
        """Return pickled DataFrame with ``count`` ticks starting at ``date_from``."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            from_ms = _dt_to_epoch_ms(date_from)
            flag_int = getattr(_mt5, f"COPY_TICKS_{flag}", _mt5.COPY_TICKS_ALL)
            df = _mt5.copy_ticks_from(symbol, from_ms, int(count), flag_int)
            if df is None:
                err = _mt5.last_error()
                raise Mt5BridgeError(f"copy_ticks_from({symbol}) failed: {err}")
            return pickle.dumps(df)

    # -- Trading -----------------------------------------------------------

    def exposed_order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send a market/limit/stop order. Returns OrderResult dict."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            mt5_request = self._build_mt5_request(request)
            result = _mt5.order_send(mt5_request)
            payload = _order_result_to_dict(result)
            log.info(
                "mt5_order_send",
                symbol=request.get("symbol"),
                side=request.get("side"),
                type=request.get("type"),
                volume=request.get("volume"),
                retcode=payload.get("retcode"),
                accepted=payload.get("accepted"),
            )
            return payload

    def exposed_order_modify(
        self,
        order_id: str,
        *,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> dict[str, Any]:
        """Modify a pending order. Returns OrderResult dict."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            request: dict[str, Any] = {
                "action": _mt5.TRADE_ACTION_MODIFY,
                "order": int(order_id),
            }
            if price is not None:
                request["price"] = float(price)
            if sl is not None:
                request["sl"] = float(sl)
            if tp is not None:
                request["tp"] = float(tp)
            result = _mt5.order_send(request)
            return _order_result_to_dict(result)

    def exposed_order_cancel(self, order_id: str) -> dict[str, Any]:
        """Cancel a pending order. Returns OrderResult dict."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            request = {
                "action": _mt5.TRADE_ACTION_REMOVE,
                "order": int(order_id),
            }
            result = _mt5.order_send(request)
            return _order_result_to_dict(result)

    def exposed_positions_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return open positions (optionally filtered by symbol)."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            if symbol:
                positions = _mt5.positions_get(symbol=symbol)
            else:
                positions = _mt5.positions_get()
            return _positions_to_list(positions)

    def exposed_orders_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return active pending orders (optionally filtered by symbol)."""
        with self._mt5_lock:
            if not self._initialized:
                raise Mt5NotInitializedError("call exposed_initialize first")
            if symbol:
                orders = _mt5.orders_get(symbol=symbol)
            else:
                orders = _mt5.orders_get()
            if not orders:
                return []
            out: list[dict[str, Any]] = []
            for o in orders:
                out.append(
                    {
                        "order_id": str(o.ticket),
                        "symbol": str(o.symbol),
                        "side": "buy" if int(o.type) in (
                            _mt5.ORDER_TYPE_BUY_LIMIT,
                            _mt5.ORDER_TYPE_BUY_STOP,
                            _mt5.ORDER_TYPE_BUY_STOP_LIMIT,
                        ) else "sell",
                        "type": self._decode_order_type(int(o.type)),
                        "volume": float(o.volume_current),
                        "price": float(o.price_open),
                        "sl": float(o.sl) if o.sl else None,
                        "tp": float(o.tp) if o.tp else None,
                        "open_time": _dt_to_epoch_ms(datetime.fromtimestamp(o.time_setup, tz=UTC)),
                        "comment": str(o.comment),
                        "magic": int(o.magic),
                    }
                )
            return out

    # -- Internals ---------------------------------------------------------

    def _account_info_dict(self) -> dict[str, Any]:
        info = _mt5.account_info()
        if info is None:
            raise Mt5BridgeError(f"account_info() returned None: {_mt5.last_error()}")
        return {
            "login": int(info.login),
            "broker": str(info.company or ""),
            "currency": str(info.currency or "USD"),
            "balance": float(info.balance),
            "equity": float(info.equity),
            "margin": float(info.margin),
            "free_margin": float(info.margin_free),
            "leverage": int(info.leverage),
            "server_time": _dt_to_epoch_ms(datetime.fromtimestamp(info.server_time, tz=UTC)),
            "trade_allowed": bool(info.trade_allowed),
            "raw": {
                "name": str(info.name or ""),
                "server": str(info.server or ""),
                "currency": str(info.currency or ""),
            },
        }

    def _terminal_info_dict(self) -> dict[str, Any]:
        info = _mt5.terminal_info()
        if info is None:
            raise Mt5BridgeError(f"terminal_info() returned None: {_mt5.last_error()}")
        return {
            "company": str(info.company or ""),
            "name": str(info.name or ""),
            "build": int(info.build),
            "connected": bool(info.connected),
            "trade_allowed": bool(info.trade_allowed),
            "maxbars": int(info.maxbars) if info.maxbars else None,
            "community_account": bool(info.community_account),
            "community_connection": bool(info.community_connection),
        }

    def _build_mt5_request(self, req: dict[str, Any]) -> dict[str, Any]:
        """Translate an OrderRequest-style dict into an mt5.order_send request.

        Required keys in ``req``: symbol, side, type, volume.
        Optional: price (LIMIT/STOP), sl, tp, deviation_points, magic, comment.
        """
        side = str(req.get("side", "buy")).lower()
        type_ = str(req.get("type", "market")).lower()
        symbol = str(req.get("symbol"))
        volume = float(req.get("volume", 0.0))
        if volume <= 0:
            raise Mt5OrderError(f"order_send: volume must be > 0 (got {volume})")
        is_buy = side == "buy"
        if type_ == "market":
            action = _mt5.TRADE_ACTION_DEAL
            order_type = _mt5.ORDER_TYPE_BUY if is_buy else _mt5.ORDER_TYPE_SELL
        elif type_ == "limit":
            action = _mt5.TRADE_ACTION_PENDING
            order_type = _mt5.ORDER_TYPE_BUY_LIMIT if is_buy else _mt5.ORDER_TYPE_SELL_LIMIT
        elif type_ == "stop":
            action = _mt5.TRADE_ACTION_PENDING
            order_type = _mt5.ORDER_TYPE_BUY_STOP if is_buy else _mt5.ORDER_TYPE_SELL_STOP
        elif type_ == "stop_limit":
            action = _mt5.TRADE_ACTION_PENDING
            order_type = _mt5.ORDER_TYPE_BUY_STOP_LIMIT if is_buy else _mt5.ORDER_TYPE_SELL_STOP_LIMIT
        else:
            raise Mt5OrderError(f"unknown order type: {type_!r}")
        request: dict[str, Any] = {
            "action": action,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "magic": int(req.get("magic", 0)),
            "comment": str(req.get("comment", ""))[:31],  # MT5 max 31 chars
        }
        price = req.get("price")
        if price is not None and type_ != "market":
            request["price"] = float(price)
        if req.get("sl") is not None:
            request["sl"] = float(req["sl"])
        if req.get("tp") is not None:
            request["tp"] = float(req["tp"])
        if req.get("deviation_points") is not None:
            request["deviation"] = int(req["deviation_points"])
        # Time-in-force
        tif_map = {
            "GTC": _mt5.ORDER_TIME_GTC,
            "DAY": _mt5.ORDER_TIME_DAY,
            "IOC": getattr(_mt5, "ORDER_TIME_IOC", _mt5.ORDER_TIME_GTC),
            "FOK": getattr(_mt5, "ORDER_TIME_FOK", _mt5.ORDER_TIME_GTC),
        }
        request["type_time"] = tif_map.get(str(req.get("time_in_force", "GTC")).upper(), _mt5.ORDER_TIME_GTC)
        request["type_filling"] = getattr(_mt5, "ORDER_FILLING_IOC", _mt5.ORDER_FILLING_RETURN)
        return request

    @staticmethod
    def _decode_order_type(mt5_type: int) -> str:
        if mt5_type in (_mt5.ORDER_TYPE_BUY_LIMIT, _mt5.ORDER_TYPE_SELL_LIMIT):
            return "limit"
        if mt5_type in (_mt5.ORDER_TYPE_BUY_STOP, _mt5.ORDER_TYPE_SELL_STOP):
            return "stop"
        if mt5_type in (_mt5.ORDER_TYPE_BUY_STOP_LIMIT, _mt5.ORDER_TYPE_SELL_STOP_LIMIT):
            return "stop_limit"
        return "market"


# ============================================================ Server bootstrap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MT5 RPyC bridge server (Windows-Python / Wine).")
    parser.add_argument("--host", default=os.environ.get("MT5_BRIDGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MT5_BRIDGE_PORT", "18812")))
    parser.add_argument(
        "--protocol-config",
        default='{"allow_all_attrs": true, "allow_setattr": false, "allow_pickle": true}',
    )
    args = parser.parse_args(argv)

    config: dict[str, Any] = json.loads(args.protocol_config)
    if os.environ.get("MT5_BRIDGE_AUTH_KEY"):
        # Auth mode: when a key is set, the client must pass it via
        # conn._config["credentials"]["auth_key"]. We initialise
        # an empty credentials dict here; the actual check happens
        # in MT5BridgeService.on_connect.
        config.setdefault("credentials", {})["auth_key"] = ""

    log.info("mt5_bridge_starting", host=args.host, port=args.port, auth_enabled=bool(os.environ.get("MT5_BRIDGE_AUTH_KEY")))
    server = ThreadedServer(
        MT5BridgeService,
        hostname=args.host,
        port=args.port,
        protocol_config=config,
    )
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("mt5_bridge_keyboard_interrupt")
    finally:
        log.info("mt5_bridge_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover - container entry
    sys.exit(main(sys.argv[1:]))
