"""execution-engine: closed positions are finalised into the journal."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from xauusd_bot.common.schemas.journal import ExitReasonTag
from xauusd_bot.connectors.schemas import ClosedPositionInfo, OrderSide
from xauusd_bot.execution_engine import _exit_reason, _journal_close, _r_multiple
from xauusd_bot.execution.position_manager import ManagedPosition


def _mp(**over) -> ManagedPosition:
    base = dict(
        ticket="T1",
        side=OrderSide.SELL,
        entry_price=Decimal("4231.23"),
        initial_volume=Decimal("0.22"),
        sl_price=Decimal("4233.40"),
        tp1_price=Decimal("4226.86"),
        tp2_price=Decimal("4222.50"),
        tp3_price=Decimal("4218.00"),
    )
    base.update(over)
    return ManagedPosition(**base)


def test_r_multiple_long_and_short() -> None:
    # Short: entry 4231.23, sl 4233.40 (risk 2.17); exit 4226.86 → +4.37/2.17.
    r = _r_multiple(OrderSide.SELL, Decimal("4231.23"), Decimal("4233.40"), Decimal("4226.86"))
    assert r == pytest.approx(2.014, abs=1e-2)
    # Long mirror.
    r2 = _r_multiple(OrderSide.BUY, Decimal("100"), Decimal("90"), Decimal("120"))
    assert r2 == pytest.approx(2.0, abs=1e-9)
    # Undefined risk.
    assert _r_multiple(OrderSide.BUY, Decimal("100"), Decimal("100"), Decimal("110")) is None


def test_exit_reason_from_broker_code() -> None:
    assert _exit_reason(_mp(), Decimal("4233.40"), reason_code=4) == ExitReasonTag.SL_HIT
    assert _exit_reason(_mp(breakeven_done=True), Decimal("4233.40"), reason_code=4) == ExitReasonTag.TRAILED
    assert _exit_reason(_mp(), Decimal("4226.86"), reason_code=5) == ExitReasonTag.TP1_HIT
    assert _exit_reason(_mp(tp1_taken=True), Decimal("4222.50"), reason_code=5) == ExitReasonTag.TP2_HIT


def test_exit_reason_infers_from_price_when_untagged() -> None:
    # No broker reason → nearest configured level wins.
    assert _exit_reason(_mp(), Decimal("4226.80"), reason_code=None) == ExitReasonTag.TP1_HIT
    # Far from every level → manual.
    assert _exit_reason(_mp(), Decimal("4100.00"), reason_code=None) == ExitReasonTag.MANUAL


@pytest.mark.asyncio
async def test_journal_close_publishes_trade_close_event() -> None:
    published: list = []

    class _Pub:
        async def publish(self, topic, event):
            published.append((topic, event))

    class _Conn:
        def closed_position_info(self, ticket):
            return ClosedPositionInfo(
                ticket=str(ticket),
                exit_price=Decimal("4226.50"),
                pnl_realized=Decimal("103.40"),
                close_time=datetime(2026, 6, 18, 21, 5, tzinfo=UTC),
                reason_code=5,
            )

    settings = SimpleNamespace(symbol="XAUUSD+")
    await _journal_close(_Pub(), _Conn(), _mp(), "T1", 4226.0, settings)

    assert len(published) == 1
    _topic, ev = published[0]
    assert ev.entry_type == "trade_close"
    tc = ev.trade_close
    assert tc.order_id == "T1"
    assert tc.exit_price == Decimal("4226.50")
    assert tc.pnl_realized == Decimal("103.40")
    assert tc.exit_reason == ExitReasonTag.TP1_HIT
    assert tc.r_multiple is not None


@pytest.mark.asyncio
async def test_journal_close_best_effort_without_deal_history() -> None:
    published: list = []

    class _Pub:
        async def publish(self, topic, event):
            published.append(event)

    class _Conn:  # no closed_position_info → fallback path
        pass

    settings = SimpleNamespace(symbol="XAUUSD")
    await _journal_close(_Pub(), _Conn(), _mp(), "T1", 4226.0, settings)

    assert len(published) == 1
    tc = published[0].trade_close
    assert tc.exit_price == Decimal("4226.0")  # fell back to last price
    assert tc.pnl_realized is None             # unknown without deal history


@pytest.mark.asyncio
async def test_journal_close_books_realized_pnl_into_risk() -> None:
    """Closing a position must feed the realized PnL into the risk caps."""
    booked = []

    class _Pub:
        async def publish(self, topic, event): pass

    class _Conn:
        def closed_position_info(self, ticket):
            return ClosedPositionInfo(
                ticket=str(ticket), exit_price=Decimal("4226.50"),
                pnl_realized=Decimal("-34.87"),
                close_time=datetime(2026, 6, 18, 21, 5, tzinfo=UTC), reason_code=4,
            )

    class _Risk:
        def record_pnl(self, pnl, now): booked.append(pnl)

    class _Redis:
        async def set(self, *a, **k): pass

    settings = SimpleNamespace(symbol="XAUUSD+")
    await _journal_close(_Pub(), _Conn(), _mp(), "T1", 4226.0, settings, _Risk(), _Redis())
    assert booked == [Decimal("-34.87")]  # the loss was booked into the risk totals
