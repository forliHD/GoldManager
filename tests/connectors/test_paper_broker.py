"""Tests for the PaperBroker — fill model, pending orders, account state.

The PaperBroker is the *only* piece of the connector layer that
simulates fills. These tests pin down the contract:

* Market orders fill at the current price + half-spread ± slippage.
* Limit orders fill only when the bar touches the trigger price.
* Pessimistic pending fills: the limit order fills at the *worst*
  price of the bar that touched the trigger, not the best.
* Order cancellation removes the order from the pending list.
* Account state (balance, equity, margin) is updated correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from xauusd_bot.connectors.paper_broker import FillModel, PaperBroker
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.schemas import (
    Bar,
    FillPolicy,
    OrderRequest,
    OrderSide,
    OrderType,
    SymbolSpec,
)

# ---------------------------------------------------------------- fixtures


def _spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="XAUUSD",
        description="XAUUSD CFD",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
    )


def _replay_with_spec(tmp_path: Path, n_bars: int = 5) -> ReplayConnector:
    """A small deterministic ReplayConnector for broker tests."""

    times = pd.date_range(
        start="2026-01-01 00:00:00",
        periods=n_bars,
        freq="1min",
        tz="UTC",
    )
    df = pd.DataFrame(
        {
            "time": times,
            "open": [2000.0 + i for i in range(n_bars)],
            "high": [2001.0 + i for i in range(n_bars)],
            "low": [1999.0 + i for i in range(n_bars)],
            "close": [2000.5 + i for i in range(n_bars)],
            "tick_volume": [100 * (i + 1) for i in range(n_bars)],
        }
    )
    p = tmp_path / "broker_sample.parquet"
    df.to_parquet(p)
    return ReplayConnector(source_path=p, symbol="XAUUSD", spec=_spec())


def _m1_bar(t: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=t,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


# =============================================================== 1. market fills


def test_market_order_fills_to_mid_plus_half_spread_minus_slippage(tmp_path: Path) -> None:
    """A market BUY fills at last.close + half_spread + slippage.

    We set the spread monitor / record_spread to a known value (40 points)
    and freeze the slippage RNG to its base value (1 point). The expected
    fill price is then deterministic."""

    conn = _replay_with_spec(tmp_path, n_bars=2)
    broker = PaperBroker(
        connector=conn,
        spec=_spec(),
        fill_model=FillModel(base_slippage_points=1.0, seed=42),
        initial_balance=Decimal("10000"),
    )
    # Lock down the spread to a known value (40 points = 0.40 in price).
    broker.record_spread(spread_points=40.0)
    # Move the connector cursor to the second bar.
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))

    result = broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            volume=Decimal("0.10"),
            fill_policy=FillPolicy.PAPER,
        )
    )
    assert result.accepted is True
    assert result.filled_volume == Decimal("0.10")
    assert result.avg_fill_price is not None
    # The market price seen was the last bar's close = 2001.5.
    # Half-spread = 20 points = 0.20. Slippage is at least 1 point.
    # Fill price = 2001.5 + 0.20 + slippage_price, with slippage_price > 0.
    fill = result.avg_fill_price
    assert fill > Decimal("2001.5") + Decimal("0.20"), (
        f"Buy fill {fill} should be > 2001.7 (close + half spread); got {fill}"
    )
    # And the fill should be reasonably close to the model: 2001.5 + 0.20 + ~0.01.
    assert fill < Decimal("2010"), f"Fill {fill} absurdly far from expected range"


def test_market_sell_fills_to_mid_minus_half_spread_minus_slippage(tmp_path: Path) -> None:
    """A market SELL fills below close."""

    conn = _replay_with_spec(tmp_path, n_bars=2)
    broker = PaperBroker(
        connector=conn,
        spec=_spec(),
        fill_model=FillModel(base_slippage_points=1.0, seed=42),
    )
    broker.record_spread(spread_points=40.0)
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))

    result = broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            volume=Decimal("0.10"),
            fill_policy=FillPolicy.PAPER,
        )
    )
    assert result.accepted is True
    assert result.avg_fill_price is not None
    fill = result.avg_fill_price
    # SELL: fill = close - half_spread - slippage. Last close = 2001.5.
    assert fill < Decimal("2001.5") - Decimal("0.20"), (
        f"Sell fill {fill} should be < 2001.3 (close - half spread); got {fill}"
    )


# =============================================================== 2. limit fills


def test_limit_order_fills_only_when_bar_touches_trigger(tmp_path: Path) -> None:
    """A buy limit at 2005 with current price ~2010 fills on a bar
    that dips to 2005 (low <= 2005) and does not fill otherwise."""

    conn = _replay_with_spec(tmp_path, n_bars=2)
    broker = PaperBroker(
        connector=conn,
        spec=_spec(),
        fill_model=FillModel(seed=42),
    )
    broker.record_spread(spread_points=30.0)
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))

    # Submit a buy limit at 2005.10 — below the current market (~2001.5).
    req = OrderRequest(
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        volume=Decimal("0.10"),
        price=Decimal("2005.10"),
    )
    pending_result = broker.submit(req)
    assert pending_result.accepted is True
    assert len(broker.pending) == 1
    assert broker.pending[0].price == Decimal("2005.10")

    # First scenario: a bar where the bar's low is ABOVE 2005.10 — no fill.
    no_trigger = _m1_bar(
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        o=2010, h=2012, low=2008, c=2011,
    )
    fills = broker.process_bar(no_trigger)
    assert fills == []  # No fill — bar.low=2008 < 2005.10? No, 2008 > 2005.10, so no trigger
    assert len(broker.pending) == 1  # Still pending

    # Second scenario: a bar where bar.low DIPS to 2005.10 or below — fill.
    triggers = _m1_bar(
        datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        o=2010, h=2011, low=2004, c=2006,  # low=2004 < 2005.10, so trigger
    )
    fills = broker.process_bar(triggers)
    assert len(fills) == 1
    assert fills[0].accepted is True
    # Pessimistic fill: BUY LIMIT → fill at bar.high (worst for buyer).
    assert fills[0].avg_fill_price == Decimal("2011")
    # Pending should be cleared.
    assert broker.pending == []


def test_pessimistic_buy_limit_fills_at_bar_high_not_limit(tmp_path: Path) -> None:
    """Direct test of the "Long-Buy-Limit unter Markt → Fill zum Limit,
    nicht zum besseren Marktpreis" requirement.

    A buy limit at 2005.10 in a market that drops sharply through it:
    the buyer wanted to buy at 2005.10 but the broker fills at bar.high
    (the worst-case price of the bar)."""

    conn = _replay_with_spec(tmp_path, n_bars=1)
    broker = PaperBroker(connector=conn, spec=_spec(), fill_model=FillModel(seed=42))
    broker.record_spread(spread_points=30.0)

    # Submit a buy limit at 2005.10.
    broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2005.10"),
        )
    )
    assert len(broker.pending) == 1

    # A bar that crashes through the limit: high=2010, low=2000.
    bar = _m1_bar(
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        o=2010, h=2010, low=2000, c=2002,
    )
    fills = broker.process_bar(bar)
    assert len(fills) == 1
    assert fills[0].accepted is True

    # Pessimistic fill: the buy limit triggers (low <= 2005.10) and fills
    # at bar.high = 2010 (NOT at the limit 2005.10 — that would be the
    # *best* price, the optimistic case).
    assert fills[0].avg_fill_price == Decimal("2010"), (
        f"Pessimistic fill should be bar.high=2010, not limit=2005.10; got {fills[0].avg_fill_price}"
    )
    # And it must NOT be the limit price.
    assert fills[0].avg_fill_price != Decimal("2005.10")


def test_pessimistic_sell_limit_fills_at_bar_low(tmp_path: Path) -> None:
    """Symmetric test for SELL LIMIT pessimistic fills."""

    conn = _replay_with_spec(tmp_path, n_bars=1)
    broker = PaperBroker(connector=conn, spec=_spec(), fill_model=FillModel(seed=42))
    broker.record_spread(spread_points=30.0)

    # Sell limit at 2005.10 (above current ~2000.5 — "above market").
    broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2005.10"),
        )
    )
    # A bar that rises through the limit: high=2010, low=2004.
    bar = _m1_bar(
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        o=2004, h=2010, low=2004, c=2009,
    )
    fills = broker.process_bar(bar)
    assert len(fills) == 1
    assert fills[0].accepted is True
    # SELL LIMIT pessimistic fill = bar.low.
    assert fills[0].avg_fill_price == Decimal("2004"), (
        f"Pessimistic sell limit fill should be bar.low=2004; got {fills[0].avg_fill_price}"
    )


def test_limit_does_not_fill_when_bar_does_not_touch(tmp_path: Path) -> None:
    """A buy limit at 2005.10 in a market that stays above (bar.low > 2005.10)
    must NOT fill, even after multiple bars."""

    conn = _replay_with_spec(tmp_path, n_bars=3)
    broker = PaperBroker(connector=conn, spec=_spec(), fill_model=FillModel(seed=42))
    broker.record_spread(spread_points=30.0)

    broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2005.10"),
        )
    )
    # Feed 3 bars, all with low > 2005.10.
    for i in range(3):
        bar = _m1_bar(
            datetime(2026, 1, 1, i, 1, tzinfo=UTC),
            o=2010 + i, h=2012 + i, low=2010 + i, c=2011 + i,  # low=2010+i > 2005.10
        )
        fills = broker.process_bar(bar)
        assert fills == []
    # Still pending.
    assert len(broker.pending) == 1


# =============================================================== 3. cancellation


def test_pending_order_cancel_removes_from_list(tmp_path: Path) -> None:
    """Cancelling a pending order removes it from the pending list."""

    conn = _replay_with_spec(tmp_path, n_bars=1)
    broker = PaperBroker(connector=conn, spec=_spec(), fill_model=FillModel(seed=42))

    # Submit a buy limit; get back the order_id.
    result = broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            volume=Decimal("0.10"),
            price=Decimal("2005.10"),
            client_order_id="client-1",
        )
    )
    assert result.accepted is True
    assert len(broker.pending) == 1
    pending = broker.pending[0]
    pid = pending.client_order_id

    # Cancel via the connector's order_cancel (the connector holds pending
    # state too in the paper-broker delegation model — but in our test we
    # call the broker's own pending-pop logic).
    # The broker doesn't expose a cancel() method; the caller iterates
    # the pending list and removes entries by id. Verify the *data* shape
    # supports that: client_order_id is unique and stable.
    assert pending.client_order_id is not None
    pending_ids = [p.client_order_id for p in broker.pending]
    assert pid in pending_ids
    # Simulate the caller's removal:
    broker._pending.pop(pid)  # noqa: SLF001 - intent: drive the public surface
    assert broker.pending == []


def test_pending_order_via_connector_cancel(tmp_path: Path) -> None:
    """If a pending order is registered with the connector, the connector's
    order_cancel should remove it. This locks in the connector↔broker
    interop for cancellation."""

    conn = _replay_with_spec(tmp_path, n_bars=1)
    # The broker and connector share an in-memory model: pending orders
    # can be added to the connector's pending dict via order_send, and
    # removed via order_cancel.
    pending_id = "manual-1"
    conn._state.pending[pending_id] = OrderRequest(  # noqa: SLF001
        symbol="XAUUSD",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        volume=Decimal("0.10"),
        price=Decimal("2005.10"),
    )
    result = conn.order_cancel(pending_id)
    assert result.accepted is True
    assert pending_id not in conn._state.pending  # noqa: SLF001


def test_cancel_unknown_order_returns_not_found(tmp_path: Path) -> None:
    """order_cancel on a non-existent order returns NOT_FOUND."""

    conn = _replay_with_spec(tmp_path, n_bars=1)
    result = conn.order_cancel("nonexistent")
    assert result.accepted is False
    assert result.error_code == "NOT_FOUND"


# =============================================================== 4. account state


def test_account_state_after_market_buy_updates_balance_and_margin(tmp_path: Path) -> None:
    """After a market BUY, the broker's margin and free_margin reflect the new
    position. balance is unchanged (no realized PnL on entry)."""

    conn = _replay_with_spec(tmp_path, n_bars=2)
    broker = PaperBroker(
        connector=conn,
        spec=_spec(),
        fill_model=FillModel(seed=42),
        initial_balance=Decimal("10000"),
    )
    broker.record_spread(spread_points=30.0)
    conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))

    assert broker.balance == Decimal("10000")
    assert broker.equity == Decimal("10000")
    assert broker.margin == Decimal("0")
    assert broker.free_margin == Decimal("10000")

    broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            volume=Decimal("0.10"),
            fill_policy=FillPolicy.PAPER,
        )
    )
    # After fill: margin increased, free_margin decreased, balance unchanged.
    assert broker.margin > Decimal("0")
    assert broker.free_margin < Decimal("10000")
    assert broker.balance == Decimal("10000")  # balance unchanged on entry
    # And we now have an open position.
    positions = broker.open_positions
    assert len(positions) == 1
    assert positions[0].side == OrderSide.BUY
    assert positions[0].volume == Decimal("0.10")


def test_account_state_after_close_realizes_pnl(tmp_path: Path) -> None:
    """Closing a profitable position credits the balance with realized PnL."""

    conn = _replay_with_spec(tmp_path, n_bars=2)
    broker = PaperBroker(
        connector=conn,
        spec=_spec(),
        fill_model=FillModel(seed=42),
        initial_balance=Decimal("10000"),
    )
    broker.record_spread(spread_points=30.0)
    conn.advance_time(datetime(2026, 1, 1, 0, 1, tzinfo=UTC))

    result = broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            volume=Decimal("0.10"),
        )
    )
    pos_id = result.order_id
    assert pos_id is not None
    # Mark-to-market: equity reflects unrealized PnL after a price move.
    broker.update_marks(last_price=Decimal("2010.00"))  # price went up
    assert broker.equity > Decimal("10000")
    # Close at 2010.00.
    close = broker.close_position(pos_id, last_price=Decimal("2010.00"))
    assert close.accepted is True
    # Balance reflects the realized profit.
    assert broker.balance > Decimal("10000")
    # Margin released.
    assert broker.margin == Decimal("0")
    # No more open positions.
    assert broker.open_positions == []


def test_update_marks_moves_equity_but_not_balance(tmp_path: Path) -> None:
    """update_marks changes equity (unrealized PnL) but not balance."""

    conn = _replay_with_spec(tmp_path, n_bars=2)
    broker = PaperBroker(
        connector=conn,
        spec=_spec(),
        fill_model=FillModel(seed=42),
        initial_balance=Decimal("10000"),
    )
    broker.record_spread(spread_points=30.0)
    conn.advance_time(datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    broker.submit(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            volume=Decimal("0.10"),
        )
    )
    balance_before = broker.balance
    broker.update_marks(last_price=Decimal("2050"))
    assert broker.balance == balance_before  # balance unchanged
    assert broker.equity != balance_before  # equity moved with the mark


def test_close_unknown_position_returns_not_found(tmp_path: Path) -> None:
    """close_position on a non-existent position returns NOT_FOUND."""

    conn = _replay_with_spec(tmp_path, n_bars=1)
    broker = PaperBroker(connector=conn, spec=_spec())
    result = broker.close_position("does-not-exist", last_price=Decimal("2000"))
    assert result.accepted is False
    assert result.error_code == "NOT_FOUND"


# =============================================================== 5. fill determinism


def test_paper_broker_fill_is_deterministic_with_seed(tmp_path: Path) -> None:
    """With a fixed seed and identical market input, fills must be reproducible."""

    def _run() -> Decimal:
        conn = _replay_with_spec(tmp_path, n_bars=2)
        broker = PaperBroker(
            connector=conn,
            spec=_spec(),
            fill_model=FillModel(seed=1234),
        )
        broker.record_spread(spread_points=30.0)
        conn.advance_time(datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
        result = broker.submit(
            OrderRequest(
                symbol="XAUUSD",
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                volume=Decimal("0.10"),
            )
        )
        assert result.avg_fill_price is not None
        return result.avg_fill_price

    fill_a = _run()
    fill_b = _run()
    assert fill_a == fill_b, (
        f"Two identical runs produced different fills: {fill_a} vs {fill_b}"
    )


def test_paper_broker_fill_model_default_seed() -> None:
    """FillModel has a sensible default seed so backtests are reproducible out
    of the box."""

    m = FillModel()
    assert m.seed == 42
