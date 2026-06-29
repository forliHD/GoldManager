"""Deferred (limit) entry in the BacktestEngine.

When the entry-zone gate blocks a chase it arms a resting limit; a later bar
that trades into the zone must fill it at the limit (delegating to
``_try_open_trade`` with ``skip_gate=True``), a bar that stays out of zone must
hold it, and a stale intent must expire at its deadline. These exercise the
``_check_pending_fill`` wiring directly (``_try_open_trade`` is spied) so they
are fully deterministic — no LLM, no price-path assumptions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests._execution_factories import make_bar
from xauusd_bot.backtest.engine import BacktestEngine, PendingEntry
from xauusd_bot.backtest.models import FixedSlippage, FixedSpread
from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.journal import InMemoryJournalStore

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "data" / "sample" / "xauusd_m1_sample.parquet"
_T = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


@pytest.fixture
def engine() -> BacktestEngine:
    conn = ReplayConnector(source_path=SAMPLE, symbol="XAUUSD")
    return BacktestEngine(
        connector=conn,
        journal=InMemoryJournalStore(),
        settings=Settings(),  # type: ignore[call-arg]
        slippage_model=FixedSlippage(Decimal("0.50")),
        spread_model=FixedSpread(Decimal("0.30")),
        context_window_bars=200,
    )


def _arm(engine: BacktestEngine, *, side: OrderSide, limit: str, deadline_min: int = 60) -> None:
    engine._pending = PendingEntry(  # noqa: SLF001
        side=side,
        limit=Decimal(limit),
        decision=object(),
        score=object(),
        qualification=object(),  # type: ignore[arg-type]
        deadline=_T + timedelta(minutes=deadline_min),
        zone=(99.0, 100.0),
    )


def _spy_open(engine: BacktestEngine) -> list[dict]:
    calls: list[dict] = []
    engine._try_open_trade = lambda **kw: calls.append(kw)  # type: ignore[assignment]  # noqa: SLF001
    return calls


def _fill(engine: BacktestEngine, bar) -> None:  # noqa: ANN001
    engine._check_pending_fill(  # noqa: SLF001
        bar=bar,
        bundle=FeatureSnapshotBundle(ts=bar.time, atr=0.5),
        snapshot_id=1,
        open_positions={},
    )


def test_deferred_long_fills_at_limit_on_dip(engine: BacktestEngine) -> None:
    _arm(engine, side=OrderSide.BUY, limit="100.0")
    calls = _spy_open(engine)
    bar = make_bar(time=_T + timedelta(minutes=5), low=Decimal("99.5"), high=Decimal("101.0"), close=Decimal("100.8"))
    _fill(engine, bar)
    assert len(calls) == 1
    assert calls[0]["entry_price_override"] == Decimal("100.0")  # fills at the limit, NOT bar.close 100.8
    assert calls[0]["skip_gate"] is True
    assert engine._pending is None  # noqa: SLF001


def test_deferred_long_holds_when_price_stays_above(engine: BacktestEngine) -> None:
    _arm(engine, side=OrderSide.BUY, limit="100.0")
    calls = _spy_open(engine)
    bar = make_bar(time=_T + timedelta(minutes=5), low=Decimal("100.5"), high=Decimal("101.2"), close=Decimal("101.0"))
    _fill(engine, bar)
    assert calls == []
    assert engine._pending is not None  # still armed  # noqa: SLF001


def test_deferred_entry_expires_past_deadline(engine: BacktestEngine) -> None:
    _arm(engine, side=OrderSide.BUY, limit="100.0", deadline_min=60)
    calls = _spy_open(engine)
    # 61 min after arm → past the deadline; even though the bar dips to the
    # limit, the stale intent must expire instead of filling.
    bar = make_bar(time=_T + timedelta(minutes=61), low=Decimal("99.0"), high=Decimal("101.0"), close=Decimal("100.0"))
    _fill(engine, bar)
    assert calls == []
    assert engine._pending is None  # noqa: SLF001


def test_deferred_short_fills_when_price_rises_to_limit(engine: BacktestEngine) -> None:
    _arm(engine, side=OrderSide.SELL, limit="100.0")
    calls = _spy_open(engine)
    bar = make_bar(time=_T + timedelta(minutes=5), low=Decimal("99.0"), high=Decimal("100.5"), close=Decimal("99.5"))
    _fill(engine, bar)
    assert len(calls) == 1
    assert calls[0]["entry_price_override"] == Decimal("100.0")
    assert engine._pending is None  # noqa: SLF001
