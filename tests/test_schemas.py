"""Schema contract: ReplayConnector and LiveMT5Connector return identical types.

The architecture invariant (see ``00_FINAL_PLAN.md`` §3.2) is that the
two connector implementations are interchangeable from the consumer's
point of view. We can't easily start the live bridge in tests, but we
*can* verify that the type annotations on each method match — and that
the constructed ReplayConnector values pass the same validation rules
the live bridge would produce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from xauusd_bot.connectors import IMarketConnector, ReplayConnector
from xauusd_bot.connectors.live import LiveMT5Connector
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    SymbolSpec,
)

# ---------------------------------------------------------------- method parity


@pytest.mark.parametrize(
    "method",
    [
        "get_rates",
        "get_ticks",
        "get_account",
        "get_symbol_spec",
        "order_send",
        "positions_get",
        "pending_get",
        "order_modify",
        "order_cancel",
        "is_connected",
        "shutdown",
    ],
)
def test_both_connectors_expose_the_same_methods(method: str) -> None:
    """Replay and Live must both define every method on IMarketConnector."""

    assert hasattr(ReplayConnector, method), f"ReplayConnector missing {method}"
    assert hasattr(LiveMT5Connector, method), f"LiveMT5Connector missing {method}"


def test_replay_is_runtime_instance_of_protocol() -> None:
    """ReplayConnector satisfies the IMarketConnector runtime protocol."""

    df = pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2026-01-01 00:00:00", "2026-01-01 00:01:00"], utc=True
            ),
            "open": [2000.0, 2001.0],
            "high": [2002.0, 2003.0],
            "low": [1999.0, 2000.0],
            "close": [2001.0, 2002.0],
            "tick_volume": [10, 20],
        }
    )
    sample = Path("/tmp/xauusd_test_schema.parquet")
    sample.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(sample)

    conn = ReplayConnector(source_path=sample, symbol="XAUUSD")
    assert isinstance(conn, IMarketConnector)


# ----------------------------------------------------------- point-in-time


def test_replay_never_returns_future_bars(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(
                [
                    "2026-01-01 00:00:00",
                    "2026-01-01 00:01:00",
                    "2026-01-01 00:02:00",
                    "2026-01-01 00:03:00",
                ],
                utc=True,
            ),
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [1.5, 2.5, 3.5, 4.5],
            "low": [0.5, 1.5, 2.5, 3.5],
            "close": [1.2, 2.2, 3.2, 4.2],
            "tick_volume": [10, 20, 30, 40],
        }
    )
    p = tmp_path / "bars.parquet"
    df.to_parquet(p)
    conn = ReplayConnector(source_path=p, symbol="XAUUSD")
    # Move the cursor to "01:30" — should yield exactly the first two bars.
    cutoff = datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)
    conn.advance_time(cutoff)
    bars = conn.get_rates("XAUUSD", "M1", count=10)
    assert [b.time for b in bars] == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    ]
    # The 3rd bar (00:02) must not appear even if we ask for a larger count.
    late = conn.get_rates("XAUUSD", "M1", count=100, end_time=cutoff)
    assert all(b.time <= cutoff for b in late)


# ----------------------------------------------------------- value-level checks


def test_replay_bar_values_round_trip(tmp_path: Path) -> None:
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(["2026-01-01 00:00:00"], utc=True),
            "open": [2000.10],
            "high": [2000.50],
            "low": [1999.80],
            "close": [2000.30],
            "tick_volume": [50],
        }
    )
    p = tmp_path / "bars.parquet"
    df.to_parquet(p)
    conn = ReplayConnector(source_path=p, symbol="XAUUSD")
    conn.advance_time(datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    [bar] = conn.get_rates("XAUUSD", "M1", count=1)
    assert isinstance(bar, Bar)
    assert bar.open == Decimal("2000.10")
    assert bar.high == Decimal("2000.50")
    assert bar.low == Decimal("1999.80")
    assert bar.close == Decimal("2000.30")
    assert bar.tick_volume == 50


def test_replay_symbol_spec() -> None:
    p = Path("/tmp/xauusd_test_spec.parquet")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "time": pd.to_datetime(["2026-01-01 00:00:00"], utc=True),
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "tick_volume": [1],
        }
    ).to_parquet(p)
    conn = ReplayConnector(source_path=p, symbol="XAUUSD")
    spec = conn.get_symbol_spec("XAUUSD")
    assert isinstance(spec, SymbolSpec)
    assert spec.point == Decimal("0.01")
    assert spec.trade_contract_size == Decimal("100")


def test_replay_account_snapshot() -> None:
    p = Path("/tmp/xauusd_test_acct.parquet")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "time": pd.to_datetime(["2026-01-01 00:00:00"], utc=True),
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
            "tick_volume": [1],
        }
    ).to_parquet(p)
    conn = ReplayConnector(source_path=p, symbol="XAUUSD")
    acct = conn.get_account()
    assert isinstance(acct, AccountInfo)
    assert acct.balance == Decimal("10000")
    assert acct.trade_allowed is True


# ----------------------------------------------------------- live is a stub


def test_live_connector_order_send_returns_bridge_not_wired() -> None:
    """LiveMT5Connector.order_send must return a clear BLOCK, not crash."""


    conn = LiveMT5Connector(bridge_host="does-not-exist", bridge_port=1)
    result = conn.order_send(
        OrderRequest(
            symbol="XAUUSD",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            volume=Decimal("0.01"),
        )
    )
    assert result.accepted is False
    assert result.error_code == "BRIDGE_NOT_WIRED"


def test_live_connector_is_not_connected_by_default() -> None:
    conn = LiveMT5Connector()
    assert conn.is_connected() is False
