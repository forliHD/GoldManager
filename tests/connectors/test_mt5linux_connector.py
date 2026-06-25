"""Tests for :class:`Mt5LinuxConnector` — the mt5linux RPyC bridge client.

The real mt5linux server (MetaTrader5 under Wine) cannot run in CI. We test
the connector against a ``FakeMt5`` that mirrors the slice of the mt5linux /
MetaTrader5 API the connector relies on (constants, ``copy_rates_from_pos``,
``account_info``, ``symbol_info``, ``order_send``, ``positions_get`` …). The
exact same call surface is separately validated against a live terminal — see
``scripts/`` and the OPERATIONS runbook.

What we cover:
1.  attach-mode: ``initialize()`` is called with no credentials.
2.  ``get_rates`` → ``Bar`` list, oldest→newest, correct OHLC/volume/spread.
3.  ``get_account`` maps company→broker, margin_free→free_margin, computes
    ``current_spread`` in points.
4.  ``get_symbol_spec`` maps the symbol_info object → ``SymbolSpec``.
5.  ``order_send`` builds a TRADE_ACTION_DEAL request and maps RETCODE_DONE
    → ``accepted=True``; a non-DONE retcode → ``accepted=False``.
6.  ``positions_get`` maps MT5 position type 0→BUY / 1→SELL.
7.  ``get_ticks`` → ``Tick`` list using time_msc.
8.  ``is_connected`` reflects terminal_info().connected.
9.  I-1 audit: no ``import MetaTrader5`` in the module.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from xauusd_bot.connectors.mt5linux_connector import Mt5LinuxConnector
from xauusd_bot.connectors.schemas import OrderRequest, OrderSide, OrderType

# MT5 integer constants the connector reads via getattr.
_CONSTS = {
    "TIMEFRAME_M1": 1,
    "TIMEFRAME_M5": 5,
    "TIMEFRAME_H1": 16385,
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_SLTP": 6,
    "TRADE_ACTION_PENDING": 5,
    "TRADE_ACTION_MODIFY": 7,
    "TRADE_ACTION_REMOVE": 8,
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "ORDER_TYPE_BUY_LIMIT": 2,
    "ORDER_TYPE_SELL_LIMIT": 3,
    "ORDER_TYPE_BUY_STOP": 4,
    "ORDER_TYPE_SELL_STOP": 5,
    "ORDER_TIME_GTC": 0,
    "ORDER_FILLING_IOC": 1,
    "TRADE_RETCODE_DONE": 10009,
    "COPY_TICKS_ALL": 3,
}


class FakeMt5:
    """Minimal mt5linux/MetaTrader5 stand-in for connector unit tests."""

    def __init__(self, *, retcode: int = 10009) -> None:
        for k, v in _CONSTS.items():
            setattr(self, k, v)
        self.initialize_calls: list[dict] = []
        self.last_order_request: dict | None = None
        self._retcode = retcode
        self._t0 = int(datetime(2026, 6, 18, 15, 0, tzinfo=UTC).timestamp())

    def initialize(self, **kwargs):
        self.initialize_calls.append(kwargs)
        return True

    def last_error(self):
        return (1, "Success")

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=True)

    def copy_rates_from_pos(self, symbol, tf, start, count):
        # rows behave like numpy structured-array rows: row["field"].
        return [
            {"time": self._t0, "open": 4250.0, "high": 4252.0, "low": 4249.0,
             "close": 4251.0, "tick_volume": 100, "spread": 12, "real_volume": 0},
            {"time": self._t0 + 60, "open": 4251.0, "high": 4253.5, "low": 4250.5,
             "close": 4253.0, "tick_volume": 120, "spread": 13, "real_volume": 0},
        ]

    def copy_ticks_range(self, symbol, frm, to, flags):
        return [
            {"time": self._t0, "time_msc": self._t0 * 1000 + 250, "bid": 4250.10,
             "ask": 4250.25, "last": 0.0, "volume": 0, "flags": 6},
        ]

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=4250.10, ask=4250.25, time=self._t0)

    def symbol_info(self, symbol):
        return SimpleNamespace(
            name=symbol, description="Gold vs US Dollar", point=0.01, digits=2,
            trade_contract_size=100.0, volume_min=0.01, volume_max=100.0,
            volume_step=0.01, currency_base="XAU", currency_profit="USD",
            currency_margin="USD",
        )

    def account_info(self):
        return SimpleNamespace(
            login=25622681, company="Vantage Markets (Pty) Ltd",
            server="VantageMarkets-Demo", name="Joshua Trauth", currency="EUR",
            balance=10000.0, equity=10000.0, margin=0.0, margin_free=10000.0,
            leverage=500, trade_allowed=True,
        )

    def order_send(self, req):
        self.last_order_request = req
        return SimpleNamespace(retcode=self._retcode, order=555, deal=777,
                               volume=req.get("volume", 0), price=req.get("price", 0),
                               comment="Done" if self._retcode == 10009 else "rejected")

    def positions_get(self, **kwargs):
        return [
            SimpleNamespace(ticket=111, symbol="XAUUSD+", type=0, volume=0.10,
                            price_open=4248.0, sl=4240.0, tp=4260.0, time=self._t0,
                            profit=3.0, swap=0.0, comment="long", magic=42),
            SimpleNamespace(ticket=112, symbol="XAUUSD+", type=1, volume=0.05,
                            price_open=4255.0, sl=0.0, tp=0.0, time=self._t0,
                            profit=-1.0, swap=0.0, comment="", magic=42),
        ]

    def orders_get(self, **kwargs):
        return []

    def shutdown(self):
        return True


def _conn(client=None, **kw):
    return Mt5LinuxConnector(symbol="XAUUSD+", client=client or FakeMt5(), **kw)


def test_attach_mode_initializes_without_credentials():
    fake = FakeMt5()
    c = _conn(fake)
    c.get_account()
    assert fake.initialize_calls == [{}]  # no login/password/server passed


def test_initialize_passes_credentials_when_all_set():
    fake = FakeMt5()
    c = _conn(fake, login=25298483, password="secret", server="VantageInternational-Demo")
    c.get_account()
    assert fake.initialize_calls[0] == {
        "login": 25298483, "password": "secret", "server": "VantageInternational-Demo",
    }


def test_get_rates_maps_bars_oldest_to_newest():
    bars = _conn().get_rates("XAUUSD+", "M1", 10)
    assert [b.timeframe for b in bars] == ["M1", "M1"]
    assert bars[0].time < bars[1].time
    assert bars[0].open == Decimal("4250.0") and bars[1].close == Decimal("4253.0")
    assert bars[1].tick_volume == 120
    assert bars[0].spread == Decimal("12")


def test_get_account_maps_fields_and_spread():
    ai = _conn().get_account()
    assert ai.login == 25622681
    assert ai.broker == "Vantage Markets (Pty) Ltd"
    assert ai.currency == "EUR"
    assert ai.free_margin == Decimal("10000.0")
    assert ai.leverage == 500
    # spread = round((4250.25 - 4250.10) / 0.01) = 15 points
    assert ai.current_spread == Decimal("15")


def test_get_symbol_spec_maps_fields():
    spec = _conn().get_symbol_spec("XAUUSD+")
    assert spec.symbol == "XAUUSD+"
    assert spec.point == Decimal("0.01")
    assert spec.digits == 2
    assert spec.trade_contract_size == Decimal("100.0")
    assert spec.volume_min == Decimal("0.01")


def test_order_send_buy_market_accepted():
    fake = FakeMt5(retcode=10009)
    c = _conn(fake)
    req = OrderRequest(symbol="XAUUSD+", side=OrderSide.BUY, type=OrderType.MARKET,
                       volume=Decimal("0.10"), sl=Decimal("4240"), tp=Decimal("4260"))
    res = c.order_send(req)
    assert res.accepted is True
    assert res.order_id == "555"
    # request was built with DEAL action + BUY type + ask price
    assert fake.last_order_request["action"] == _CONSTS["TRADE_ACTION_DEAL"]
    assert fake.last_order_request["type"] == _CONSTS["ORDER_TYPE_BUY"]
    assert fake.last_order_request["price"] == 4250.25  # ask
    assert fake.last_order_request["sl"] == 4240.0


def test_order_send_rejected_retcode():
    res = _conn(FakeMt5(retcode=10006)).order_send(
        OrderRequest(symbol="XAUUSD+", side=OrderSide.SELL, type=OrderType.MARKET,
                     volume=Decimal("0.10"))
    )
    assert res.accepted is False
    assert res.error_code == "10006"


def test_positions_get_maps_side_from_type():
    positions = _conn().positions_get()
    assert positions[0].side == OrderSide.BUY and positions[0].sl == Decimal("4240.0")
    assert positions[1].side == OrderSide.SELL and positions[1].sl is None  # 0.0 → None


def test_order_modify_open_position_uses_sltp_and_preserves_tp():
    """Trailing an open position's SL must use TRADE_ACTION_SLTP + position and
    re-send the existing TP (a missing leg is read as 0 = removed by MT5)."""
    fake = FakeMt5()
    c = _conn(fake)
    # Ticket 111 is an open position (sl=4240, tp=4260). Trail only the SL.
    res = c.order_modify("111", sl=4245.0)
    assert res.accepted is True
    req = fake.last_order_request
    assert req["action"] == _CONSTS["TRADE_ACTION_SLTP"]
    assert req["position"] == 111
    assert "order" not in req
    assert req["sl"] == 4245.0
    assert req["tp"] == 4260.0  # preserved from the live position, NOT wiped


def test_order_modify_position_without_tp_keeps_zero():
    """A position with no TP (ticket 112, sl=0/tp=0) stays TP-less on an SL trail."""
    fake = FakeMt5()
    res = _conn(fake).order_modify("112", sl=4250.0)
    assert res.accepted is True
    req = fake.last_order_request
    assert req["action"] == _CONSTS["TRADE_ACTION_SLTP"]
    assert req["position"] == 112 and req["sl"] == 4250.0 and req["tp"] == 0.0


def test_order_modify_pending_order_uses_modify_action():
    """A ticket that is NOT an open position is a pending order → MODIFY."""
    fake = FakeMt5()
    res = _conn(fake).order_modify("999", price=4200.0, sl=4250.0)
    assert res.accepted is True
    req = fake.last_order_request
    assert req["action"] == _CONSTS["TRADE_ACTION_MODIFY"]
    assert req["order"] == 999
    assert "position" not in req
    assert req["price"] == 4200.0 and req["sl"] == 4250.0


def test_order_modify_refuses_on_empty_positions_snapshot():
    """Review #4: an EMPTY positions_get() (likely a transient bridge read) must
    NOT fall through to the pending-order action on a real position ticket — that
    is the silent SL-trail rejection. Refuse so the manage loop retries."""
    fake = FakeMt5()
    c = _conn(fake)
    c.positions_get = lambda *a, **k: []  # simulate a transient empty read
    res = c.order_modify("111", sl=4245.0)
    assert res.accepted is False
    assert res.error_code == "POSITION_NOT_FOUND"
    assert fake.last_order_request is None  # no (wrong) order_send was issued


def test_get_ticks_uses_time_msc():
    ticks = _conn().get_ticks(
        "XAUUSD+", datetime(2026, 6, 18, 15, 0, tzinfo=UTC),
        datetime(2026, 6, 18, 15, 0, tzinfo=UTC) + timedelta(minutes=2),
    )
    assert len(ticks) == 1
    assert ticks[0].bid == Decimal("4250.10")
    assert ticks[0].last is None  # 0.0 → None
    assert ticks[0].time.microsecond == 250000  # from time_msc


def test_is_connected_reflects_terminal_info():
    assert _conn().is_connected() is True


def test_no_metatrader5_import_in_module():
    """I-1: never import the Windows-only MetaTrader5 *package* directly.

    ``from mt5linux import MetaTrader5`` (the RPyC client class, which merely
    shares the name) is allowed — that is the whole point of the bridge.
    """
    import re

    mod = __import__("xauusd_bot.connectors.mt5linux_connector", fromlist=["x"])
    src = inspect.getsource(mod)
    assert re.search(r"(?m)^\s*import MetaTrader5\b", src) is None
    assert "from MetaTrader5 import" not in src
    assert "from mt5linux import MetaTrader5" in src  # the bridge client


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
