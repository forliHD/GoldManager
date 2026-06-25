"""Live MT5 connector via the ``mt5linux`` RPyC bridge (gmag11 image).

This is the second live-connector implementation (alongside
:class:`xauusd_bot.connectors.live.LiveMT5Connector`). It targets the
``gmag11/metatrader5_vnc`` container, which runs MetaTrader 5 under Wine
and exposes an ``mt5linux`` RPyC server (default port ``8001``). The
``mt5linux`` client mirrors the Windows-only ``MetaTrader5`` package API
over RPyC, so this module never imports ``MetaTrader5`` directly — keeping
architecture invariant I-1 intact (see ``AGENTS.md`` §3 I-1).

Attach vs. login
----------------
Unlike :class:`LiveMT5Connector` (which logs in programmatically with
``MT5_LOGIN``/``MT5_PASSWORD``/``MT5_SERVER``), this connector **attaches**
to whatever terminal is already running and logged in inside the container
— the operator logs in once via the KasmVNC browser desktop, and the
session persists in the ``mt5-config`` volume. ``initialize()`` (no path,
no credentials) connects to that running terminal. If ``login``/``password``
/``server`` *are* provided they are passed to ``initialize`` as a
best-effort programmatic login, but attach-mode is the supported path.

Threading / reconnect
---------------------
The ``mt5linux`` client holds one RPyC connection. Calls are serialised
under a lock (the MT5 terminal is single-threaded). A lost connection is
detected per-call and lazily re-established on the next call. ``order_send``
returns a clean ``OrderResult(accepted=False, ...)`` on bridge-down rather
than raising, so the execution engine degrades gracefully.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    ClosedPositionInfo,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
    Tick,
)

log = structlog.get_logger(__name__)

_DEFAULT_BRIDGE_HOST = "mt5-terminal"
_DEFAULT_BRIDGE_PORT = 8001


def _build_mt5linux_client(host: str, port: int) -> Any:
    """Construct the ``mt5linux`` client. Imported lazily (prod-only dep)."""
    try:
        from mt5linux import MetaTrader5  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - platform dependent
        raise RuntimeError(
            "mt5linux is required for Mt5LinuxConnector "
            "(install with `pip install mt5linux rpyc==5.2.3`): "
            f"{exc}"
        ) from exc
    return MetaTrader5(host=host, port=int(port))


# Map our string timeframes to the MT5 TIMEFRAME_* integer constants.
# Resolved lazily against the live client (the constants live on the
# proxied MetaTrader5 module) so we never hard-code magic numbers.
_TF_NAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")


class Mt5LinuxConnector(IMarketConnector):
    """``IMarketConnector`` backed by an ``mt5linux`` RPyC bridge.

    Parameters
    ----------
    host, port:
        Bridge server address. In the dev/prod Docker stack this is the
        service name ``mt5-terminal`` on port ``8001``.
    symbol:
        Default symbol (e.g. ``"XAUUSD+"`` on Vantage). Per-call symbol
        overrides are accepted on every read method.
    login, password, server:
        Optional. When all three are set they are passed to ``initialize``
        for a best-effort programmatic login; otherwise the connector
        attaches to the already-logged-in terminal (the supported path).
    client:
        Injected mt5linux-style client (tests). When ``None`` a real client
        is built lazily on first use.
    """

    def __init__(
        self,
        *,
        host: str = _DEFAULT_BRIDGE_HOST,
        port: int = _DEFAULT_BRIDGE_PORT,
        symbol: str = "XAUUSD",
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._symbol = symbol
        self._login = int(login) if login else None
        self._password = password
        self._server = server
        self._mt5: Any = client
        self._initialized = False
        self._tf_cache: dict[str, int] = {}
        self._lock = threading.RLock()

    # ========================================================== connection

    def _ensure(self) -> Any:
        """Return a connected, initialized mt5 client (lazy + reconnect)."""
        with self._lock:
            if self._mt5 is None:
                self._mt5 = _build_mt5linux_client(self._host, self._port)
                self._initialized = False
            if not self._initialized:
                kwargs: dict[str, Any] = {}
                if self._login and self._password and self._server:
                    kwargs = {
                        "login": self._login,
                        "password": self._password,
                        "server": self._server,
                    }
                ok = self._mt5.initialize(**kwargs)
                if not ok:
                    err = self._safe(lambda: self._mt5.last_error())
                    raise RuntimeError(f"mt5 initialize() failed: {err}")
                self._initialized = True
            return self._mt5

    def _reset(self) -> None:
        with self._lock:
            self._initialized = False

    @staticmethod
    def _safe(fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - diagnostics only
            return default

    def _timeframe(self, mt5: Any, timeframe: str) -> int:
        tf = timeframe.upper()
        if tf in self._tf_cache:
            return self._tf_cache[tf]
        const = getattr(mt5, f"TIMEFRAME_{tf}", None)
        if const is None:
            raise ValueError(f"unsupported timeframe: {timeframe!r}")
        value = int(const)
        self._tf_cache[tf] = value
        return value

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _field(row: Any, name: str, default: Any = None) -> Any:
        """Read ``name`` from a numpy structured-array row or namedtuple."""
        try:
            return row[name]
        except Exception:  # noqa: BLE001 - fall through to attribute access
            return getattr(row, name, default)

    @staticmethod
    def _is_nan(v: Any) -> bool:
        try:
            return v != v  # noqa: PLR0124 - NaN check
        except Exception:  # noqa: BLE001
            return False

    # ================================================================ data

    def get_rates(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[Bar]:
        mt5 = self._ensure()
        tf = self._timeframe(mt5, timeframe)
        try:
            if end_time is None:
                rates = mt5.copy_rates_from_pos(symbol, tf, 0, int(count))
            else:
                if end_time.tzinfo is None:
                    end_time = end_time.replace(tzinfo=UTC)
                end_dt = end_time.astimezone(UTC)
                rates = mt5.copy_rates_from(symbol, tf, end_dt, int(count))
        except Exception as exc:  # noqa: BLE001
            self._reset()
            raise RuntimeError(f"copy_rates failed: {exc}") from exc
        if rates is None:
            return []
        out: list[Bar] = []
        for r in rates:
            t = int(self._field(r, "time", 0))
            bar_time = datetime.fromtimestamp(t, tz=UTC)
            if end_time is not None and bar_time >= end_time.astimezone(UTC):
                continue
            real_vol = self._field(r, "real_volume")
            spread = self._field(r, "spread")
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe.upper(),
                    time=bar_time,
                    open=Decimal(str(self._field(r, "open"))),
                    high=Decimal(str(self._field(r, "high"))),
                    low=Decimal(str(self._field(r, "low"))),
                    close=Decimal(str(self._field(r, "close"))),
                    tick_volume=int(self._field(r, "tick_volume", 0) or 0),
                    real_volume=(int(real_vol) if real_vol is not None and not self._is_nan(real_vol) else None),
                    spread=(Decimal(str(spread)) if spread is not None and not self._is_nan(spread) else None),
                )
            )
        out.sort(key=lambda b: b.time)
        return out[-int(count):] if count and len(out) > int(count) else out

    def get_ticks(self, symbol: str, from_ts: datetime, to_ts: datetime) -> list[Tick]:
        mt5 = self._ensure()
        if from_ts.tzinfo is None:
            from_ts = from_ts.replace(tzinfo=UTC)
        if to_ts.tzinfo is None:
            to_ts = to_ts.replace(tzinfo=UTC)
        flags = getattr(mt5, "COPY_TICKS_ALL", 3)
        try:
            ticks = mt5.copy_ticks_range(
                symbol, from_ts.astimezone(UTC), to_ts.astimezone(UTC), flags
            )
        except Exception as exc:  # noqa: BLE001
            self._reset()
            raise RuntimeError(f"copy_ticks_range failed: {exc}") from exc
        if ticks is None:
            return []
        out: list[Tick] = []
        for r in ticks:
            tmsc = self._field(r, "time_msc")
            if tmsc is not None and not self._is_nan(tmsc) and int(tmsc) > 0:
                tick_time = datetime.fromtimestamp(int(tmsc) / 1000.0, tz=UTC)
            else:
                tick_time = datetime.fromtimestamp(int(self._field(r, "time", 0)), tz=UTC)
            last = self._field(r, "last")
            out.append(
                Tick(
                    symbol=symbol,
                    time=tick_time,
                    bid=Decimal(str(self._field(r, "bid"))),
                    ask=Decimal(str(self._field(r, "ask"))),
                    last=(Decimal(str(last)) if last is not None and not self._is_nan(last) and float(last) > 0 else None),
                    volume=int(self._field(r, "volume", 0) or 0),
                    flags=int(self._field(r, "flags", 0) or 0),
                )
            )
        return out

    # ------------------------------------------------------------- account

    def get_account(self) -> AccountInfo:
        mt5 = self._ensure()
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info() returned None: {self._safe(lambda: mt5.last_error())}")
        spread = None
        tick = self._safe(lambda: mt5.symbol_info_tick(self._symbol))
        si = self._safe(lambda: mt5.symbol_info(self._symbol))
        if tick is not None and si is not None:
            point = self._safe(lambda: float(getattr(si, "point", 0.0)), 0.0)
            if point:
                spread = Decimal(str(round((float(tick.ask) - float(tick.bid)) / point)))
        return AccountInfo(
            login=int(getattr(info, "login", 0)),
            broker=str(getattr(info, "company", "") or getattr(info, "server", "")),
            currency=str(getattr(info, "currency", "USD")),
            balance=Decimal(str(getattr(info, "balance", 0))),
            equity=Decimal(str(getattr(info, "equity", 0))),
            margin=Decimal(str(getattr(info, "margin", 0))),
            free_margin=Decimal(str(getattr(info, "margin_free", 0))),
            leverage=int(getattr(info, "leverage", 100)),
            server_time=datetime.now(tz=UTC),
            trade_allowed=bool(getattr(info, "trade_allowed", True)),
            current_spread=spread,
            raw={"server": str(getattr(info, "server", "")), "name": str(getattr(info, "name", ""))},
        )

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        mt5 = self._ensure()
        si = mt5.symbol_info(symbol)
        if si is None:
            raise RuntimeError(f"symbol_info({symbol!r}) returned None — symbol not in Market Watch?")

        def g(name: str, default: Any) -> Any:
            v = getattr(si, name, default)
            return default if v is None else v

        return SymbolSpec(
            symbol=str(g("name", symbol)),
            description=str(g("description", "")),
            point=Decimal(str(g("point", 0.01))),
            digits=int(g("digits", 2)),
            trade_contract_size=Decimal(str(g("trade_contract_size", 100))),
            volume_min=Decimal(str(g("volume_min", 0.01))),
            volume_max=Decimal(str(g("volume_max", 100.0))),
            volume_step=Decimal(str(g("volume_step", 0.01))),
            currency_base=str(g("currency_base", "XAU")),
            currency_profit=str(g("currency_profit", "USD")),
            currency_margin=str(g("currency_margin", "USD")),
        )

    # ------------------------------------------------------------- trading

    def order_send(self, request: OrderRequest) -> OrderResult:
        try:
            mt5 = self._ensure()
        except Exception as exc:  # noqa: BLE001
            log.error("mt5linux_bridge_unreachable_on_send", error=str(exc))
            return OrderResult(accepted=False, error_code="BRIDGE_DOWN", error_message=str(exc))
        try:
            mt5_req = self._build_order_request(mt5, request)
            result = mt5.order_send(mt5_req)
        except Exception as exc:  # noqa: BLE001
            self._reset()
            log.error("mt5linux_order_send_failed", error=str(exc))
            return OrderResult(accepted=False, error_code="BRIDGE_ERROR", error_message=str(exc))
        retcode = int(getattr(result, "retcode", -1))
        done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        accepted = retcode == int(done)
        order_id = getattr(result, "order", 0) or getattr(result, "deal", 0)
        return OrderResult(
            accepted=accepted,
            order_id=(str(order_id) if order_id else None),
            client_order_id=request.client_order_id,
            filled_volume=Decimal(str(getattr(result, "volume", 0) or 0)),
            avg_fill_price=(Decimal(str(result.price)) if getattr(result, "price", 0) else None),
            error_code=(None if accepted else str(retcode)),
            error_message=(None if accepted else str(getattr(result, "comment", "") or "rejected by MT5")),
            raw={"retcode": retcode, "comment": str(getattr(result, "comment", ""))},
        )

    def _build_order_request(self, mt5: Any, request: OrderRequest) -> dict[str, Any]:
        is_buy = request.side == OrderSide.BUY
        tick = mt5.symbol_info_tick(request.symbol)
        if request.type == OrderType.MARKET:
            action = getattr(mt5, "TRADE_ACTION_DEAL")
            otype = getattr(mt5, "ORDER_TYPE_BUY") if is_buy else getattr(mt5, "ORDER_TYPE_SELL")
            price = float(tick.ask if is_buy else tick.bid)
        else:
            action = getattr(mt5, "TRADE_ACTION_PENDING")
            otype = self._pending_type(mt5, request, is_buy)
            price = float(request.price) if request.price is not None else float(tick.ask if is_buy else tick.bid)
        req: dict[str, Any] = {
            "action": int(action),
            "symbol": request.symbol,
            "volume": float(request.volume),
            "type": int(otype),
            "price": price,
            "deviation": int(request.deviation_points or 20),
            "magic": int(request.magic),
            "comment": request.comment or "",
            "type_time": int(getattr(mt5, "ORDER_TIME_GTC", 0)),
            "type_filling": int(getattr(mt5, "ORDER_FILLING_IOC", 1)),
        }
        if request.sl is not None:
            req["sl"] = float(request.sl)
        if request.tp is not None:
            req["tp"] = float(request.tp)
        return req

    @staticmethod
    def _pending_type(mt5: Any, request: OrderRequest, is_buy: bool) -> int:
        if request.type == OrderType.LIMIT:
            return int(getattr(mt5, "ORDER_TYPE_BUY_LIMIT") if is_buy else getattr(mt5, "ORDER_TYPE_SELL_LIMIT"))
        return int(getattr(mt5, "ORDER_TYPE_BUY_STOP") if is_buy else getattr(mt5, "ORDER_TYPE_SELL_STOP"))

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        mt5 = self._ensure()
        items = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if not items:
            return []
        out: list[Position] = []
        for p in items:
            ptype = int(getattr(p, "type", 0))
            side = OrderSide.BUY if ptype == 0 else OrderSide.SELL
            sl = getattr(p, "sl", 0) or 0
            tp = getattr(p, "tp", 0) or 0
            out.append(
                Position(
                    position_id=str(getattr(p, "ticket", 0)),
                    symbol=str(getattr(p, "symbol", symbol or self._symbol)),
                    side=side,
                    volume=Decimal(str(getattr(p, "volume", 0))),
                    open_price=Decimal(str(getattr(p, "price_open", 0))),
                    sl=(Decimal(str(sl)) if float(sl) else None),
                    tp=(Decimal(str(tp)) if float(tp) else None),
                    open_time=datetime.fromtimestamp(int(getattr(p, "time", 0)), tz=UTC),
                    profit=Decimal(str(getattr(p, "profit", 0) or 0)),
                    swap=Decimal(str(getattr(p, "swap", 0) or 0)),
                    comment=str(getattr(p, "comment", "") or ""),
                    magic=int(getattr(p, "magic", 0) or 0),
                )
            )
        return out

    def close_position(self, ticket: str, volume: Decimal | None = None) -> OrderResult:
        """Close a position (fully, or ``volume`` lots partially) at market.

        Used by the per-bar position-management loop for TP1/TP2 partial closes
        and runner exits. Closing = a market DEAL in the opposite direction with
        the ``position`` field set to the ticket.
        """
        try:
            mt5 = self._ensure()
        except Exception as exc:  # noqa: BLE001
            return OrderResult(accepted=False, error_code="BRIDGE_DOWN", error_message=str(exc))
        try:
            found = mt5.positions_get(ticket=int(ticket))
            found = list(found) if found else []
            if not found:
                return OrderResult(accepted=False, error_code="NO_POSITION", error_message=f"position {ticket} not open")
            p = found[0]
            ptype = int(getattr(p, "type", 0))
            sym = str(getattr(p, "symbol", self._symbol))
            pos_vol = float(getattr(p, "volume", 0))
            close_vol = min(float(volume), pos_vol) if volume is not None else pos_vol
            if close_vol <= 0:
                return OrderResult(accepted=False, error_code="ZERO_VOLUME")
            tick = mt5.symbol_info_tick(sym)
            if ptype == 0:  # long → close with a SELL at bid
                otype = getattr(mt5, "ORDER_TYPE_SELL")
                price = float(tick.bid)
            else:  # short → close with a BUY at ask
                otype = getattr(mt5, "ORDER_TYPE_BUY")
                price = float(tick.ask)
            req = {
                "action": int(getattr(mt5, "TRADE_ACTION_DEAL")),
                "symbol": sym,
                "volume": close_vol,
                "type": int(otype),
                "position": int(ticket),
                "price": price,
                "deviation": 20,
                "type_time": int(getattr(mt5, "ORDER_TIME_GTC", 0)),
                "type_filling": int(getattr(mt5, "ORDER_FILLING_IOC", 1)),
                "comment": "mgmt_close",
            }
            result = mt5.order_send(req)
        except Exception as exc:  # noqa: BLE001
            self._reset()
            return OrderResult(accepted=False, error_code="BRIDGE_ERROR", error_message=str(exc))
        retcode = int(getattr(result, "retcode", -1))
        accepted = retcode == int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        return OrderResult(
            accepted=accepted,
            order_id=str(ticket),
            filled_volume=(Decimal(str(close_vol)) if accepted else Decimal("0")),
            error_code=(None if accepted else str(retcode)),
            error_message=(None if accepted else str(getattr(result, "comment", "") or "close rejected")),
            raw={"retcode": retcode},
        )

    def closed_position_info(self, ticket: str) -> ClosedPositionInfo | None:
        """Reconcile a now-closed position from broker deal history.

        Sums the OUT (closing) deals for ``ticket`` to get realized PnL
        (profit + swap + commission + fee) and a volume-weighted exit price;
        ``close_time`` is the last deal, ``reason_code`` the last deal's reason
        (MT5 ``DEAL_REASON_*``: 4 = SL, 5 = TP). Returns ``None`` if no deal
        history is available (best-effort — never raises into the caller).
        """
        try:
            mt5 = self._ensure()
            deals = mt5.history_deals_get(position=int(ticket))
        except Exception as exc:  # noqa: BLE001 - history is best-effort
            log.warning("mt5_closed_position_info_failed", ticket=ticket, error=str(exc))
            return None
        deals = list(deals) if deals else []
        if not deals:
            return None
        entry_out = int(getattr(mt5, "DEAL_ENTRY_OUT", 1))
        out_deals = [d for d in deals if int(getattr(d, "entry", 0)) == entry_out]
        if not out_deals:
            return None
        # Realized PnL = price profit + ALL costs across EVERY deal of the
        # position. Commission is charged per side, so the entry (IN) deal
        # carries its own commission — summing only OUT deals silently drops it.
        # The spread is already inside ``profit`` (the bid/ask fills).
        pnl = Decimal("0")
        for d in deals:
            prof = float(getattr(d, "profit", 0) or 0)
            swap = float(getattr(d, "swap", 0) or 0)
            comm = float(getattr(d, "commission", 0) or 0)
            fee = float(getattr(d, "fee", 0) or 0)
            pnl += Decimal(str(prof + swap + comm + fee))
        # Exit price (volume-weighted), close time and reason come from the
        # closing (OUT) deals only.
        vol_sum = 0.0
        px_vol = 0.0
        last_time = 0
        reason_code: int | None = None
        for d in out_deals:
            v = float(getattr(d, "volume", 0) or 0)
            px = float(getattr(d, "price", 0) or 0)
            vol_sum += v
            px_vol += px * v
            t = int(getattr(d, "time", 0) or 0)
            if t >= last_time:
                last_time = t
                reason_code = int(getattr(d, "reason", 0)) if hasattr(d, "reason") else None
        exit_price = Decimal(str(px_vol / vol_sum)) if vol_sum > 0 else Decimal(str(getattr(out_deals[-1], "price", 0) or 0))
        return ClosedPositionInfo(
            ticket=str(ticket),
            exit_price=exit_price,
            pnl_realized=pnl,
            close_time=datetime.fromtimestamp(last_time, tz=UTC) if last_time else datetime.now(tz=UTC),
            reason_code=reason_code,
        )

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:
        mt5 = self._ensure()
        items = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if not items:
            return []
        out: list[OrderRequest] = []
        for o in items:
            otype = int(getattr(o, "type", 0))
            is_buy = otype in (getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2), getattr(mt5, "ORDER_TYPE_BUY_STOP", 4))
            is_limit = otype in (getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2), getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3))
            sl = getattr(o, "sl", 0) or 0
            tp = getattr(o, "tp", 0) or 0
            out.append(
                OrderRequest(
                    symbol=str(getattr(o, "symbol", symbol or self._symbol)),
                    side=OrderSide.BUY if is_buy else OrderSide.SELL,
                    type=OrderType.LIMIT if is_limit else OrderType.STOP,
                    volume=Decimal(str(getattr(o, "volume_current", 0) or getattr(o, "volume_initial", 0))),
                    price=Decimal(str(getattr(o, "price_open", 0))),
                    sl=(Decimal(str(sl)) if float(sl) else None),
                    tp=(Decimal(str(tp)) if float(tp) else None),
                    magic=int(getattr(o, "magic", 0) or 0),
                    comment=str(getattr(o, "comment", "") or ""),
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
        mt5 = self._ensure()
        # An OPEN position and a PENDING order need different MT5 actions:
        #   - position SL/TP  → TRADE_ACTION_SLTP with `position`
        #   - pending order   → TRADE_ACTION_MODIFY with `order`
        # The manage loop only ever trails an open position's SL; the old code
        # used TRADE_ACTION_MODIFY+order for it, which the broker rejects (that
        # ticket is a position, not a pending order) → the trail silently never
        # reached the broker. Detect the position case and use SLTP. Critically,
        # an SLTP modify must carry BOTH legs — a missing `sl`/`tp` is read as 0
        # and *removes* that level — so we preserve the un-passed leg from the
        # live position (e.g. trailing the SL keeps the TP backstop intact).
        positions = self.positions_get()
        position = next(
            (p for p in positions if str(p.position_id) == str(order_id)),
            None,
        )
        req: dict[str, Any]
        if position is not None:
            cur_sl = float(position.sl) if position.sl is not None else 0.0
            cur_tp = float(position.tp) if position.tp is not None else 0.0
            req = {
                "action": int(getattr(mt5, "TRADE_ACTION_SLTP")),
                "position": int(order_id),
                "symbol": position.symbol,
                "sl": float(sl) if sl is not None else cur_sl,
                "tp": float(tp) if tp is not None else cur_tp,
            }
        else:
            # Not in the positions book. Only use the pending-order action if the
            # bridge actually confirms a pending order with this ticket. If it does
            # NOT and the positions snapshot was EMPTY, that is most likely a
            # transient read failure for a real position — refuse rather than send
            # the pending action to a live position (the silent SL-trail rejection
            # this method was fixed to avoid). The manage loop retries next bar.
            try:
                is_pending = any(
                    str(getattr(o, "ticket", "")) == str(order_id) for o in (mt5.orders_get() or [])
                )
            except Exception:  # noqa: BLE001 - orders read is best-effort
                is_pending = False
            if not positions and not is_pending:
                return OrderResult(
                    accepted=False,
                    order_id=str(order_id),
                    error_code="POSITION_NOT_FOUND",
                    error_message="order_modify target not in positions/orders snapshot (transient read?) — will retry",
                )
            req = {
                "action": int(getattr(mt5, "TRADE_ACTION_MODIFY")),
                "order": int(order_id),
            }
            if price is not None:
                req["price"] = float(price)
            if sl is not None:
                req["sl"] = float(sl)
            if tp is not None:
                req["tp"] = float(tp)
        try:
            result = mt5.order_send(req)
        except Exception as exc:  # noqa: BLE001
            self._reset()
            return OrderResult(accepted=False, error_code="BRIDGE_ERROR", error_message=str(exc))
        retcode = int(getattr(result, "retcode", -1))
        accepted = retcode == int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        return OrderResult(
            accepted=accepted,
            order_id=str(order_id),
            error_code=(None if accepted else str(retcode)),
            error_message=(None if accepted else str(getattr(result, "comment", "") or "modify rejected")),
            raw={"retcode": retcode},
        )

    def order_cancel(self, order_id: str) -> OrderResult:
        mt5 = self._ensure()
        req = {"action": int(getattr(mt5, "TRADE_ACTION_REMOVE")), "order": int(order_id)}
        try:
            result = mt5.order_send(req)
        except Exception as exc:  # noqa: BLE001
            self._reset()
            return OrderResult(accepted=False, error_code="BRIDGE_ERROR", error_message=str(exc))
        retcode = int(getattr(result, "retcode", -1))
        accepted = retcode == int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        return OrderResult(
            accepted=accepted,
            order_id=str(order_id),
            error_code=(None if accepted else str(retcode)),
            error_message=(None if accepted else str(getattr(result, "comment", "") or "cancel rejected")),
            raw={"retcode": retcode},
        )

    # -------------------------------------------------------------- health

    def is_connected(self) -> bool:
        try:
            mt5 = self._ensure()
            ti = mt5.terminal_info()
            return bool(ti is not None and getattr(ti, "connected", False))
        except Exception:  # noqa: BLE001
            self._reset()
            return False

    def shutdown(self) -> None:
        # IMPORTANT: mt5linux exposes ONE shared MetaTrader5 module to every
        # RPyC client, and ``MetaTrader5.shutdown()`` is process-global — calling
        # it here would tear down the terminal IPC for *all* connected services
        # (e.g. stopping the execution-engine would freeze the data-collector).
        # We therefore only drop our local client handle; the terminal stays
        # attached for siblings. The RPyC socket is closed on GC.
        with self._lock:
            self._mt5 = None
            self._initialized = False


__all__ = ["Mt5LinuxConnector"]
