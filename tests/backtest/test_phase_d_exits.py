"""Phase D in the backtest — partial closes + runner produce a blended multi-R."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from xauusd_bot.backtest import BacktestEngine, FixedSlippage, FixedSpread
from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import EntryType, ScoreBand
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.common.schemas.journal import TradeRecord
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.schemas import Bar, OrderSide
from xauusd_bot.journal import InMemoryJournalStore

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample" / "xauusd_m1_sample.parquet"
SHORT = Path(__file__).resolve().parents[2] / "data" / "sample" / "_short_phd.parquet"


@pytest.fixture(scope="module", autouse=True)
def _short():
    if not SHORT.exists():
        pd.read_parquet(SAMPLE).iloc[:120].to_parquet(SHORT)


def _bar(ts, o, h, lo, c):
    return Bar(symbol="XAUUSD", time=ts, timeframe="M1",
               open=Decimal(o), high=Decimal(h), low=Decimal(lo), close=Decimal(c),
               tick_volume=100, spread=10, real_volume=0)


def test_runner_blended_r_across_partials():
    """TP1(1R)+TP2(2R)+runner-to-TP3(3R) at 30/30/40 → blended ~2.1R."""
    journal = InMemoryJournalStore()
    eng = BacktestEngine(
        connector=ReplayConnector(source_path=SHORT, symbol="XAUUSD"),
        journal=journal,
        settings=Settings(),  # type: ignore[call-arg]
        slippage_model=FixedSlippage(Decimal("0.50")),
        spread_model=FixedSpread(Decimal("0.30")),
        context_window_bars=100,
    )
    # A real journal trade so _close_position's update_trade succeeds.
    t0 = datetime(2026, 4, 15, 13, 0, tzinfo=UTC)  # Wednesday, mid-window (no weekend flat)
    tr = TradeRecord(
        timestamp_open=t0, side="long", entry_price=Decimal("2000"),
        stop_loss=Decimal("1990"), volume_lots=Decimal("1.0"), risk_amount=Decimal("1000"),
        setup_id=uuid4(), score=70.0, band=ScoreBand.PREPARE_65_74,
        entry_type=EntryType.SCOUT, fill_price=Decimal("2000"),
    )
    eng._run_async(journal.write_trade(tr))  # same loop the engine uses for update_trade

    state = {
        "trade_id": tr.id, "side": OrderSide.BUY, "entry_price": Decimal("2000"),
        "sl": Decimal("1990"), "tps": [Decimal("2010"), Decimal("2020"), Decimal("2030")],
        "tp1": Decimal("2010"), "tp2": Decimal("2020"), "tp3": Decimal("2030"),
        "volume": Decimal("1.0"), "initial_volume": Decimal("1.0"),
        "risk_amount": Decimal("1000"), "initial_risk": Decimal("10"),
        "entry_time": t0, "zone_id": None,
        "tp1_taken": False, "tp2_taken": False, "armed": False, "realized_pnl": Decimal("0"),
        "peak": Decimal("2000"),
    }
    open_positions = {"p1": state}
    # No structure in the bundle → the trail/runner stay quiet so the TP tiers
    # are tested in isolation.
    bundle = FeatureSnapshotBundle(ts=t0, atr=2.0, broker_offset_minutes=0.0)

    bal0 = eng._paper._account.balance  # noqa: SLF001 — before any leg settles

    # Bar 1: touches TP1 only (high 2012). Bar 2: TP2 (high 2022). Bar 3: TP3 (high 2032).
    eng._walk_open_positions(bar=_bar(t0, "2000", "2012", "2001", "2011"),
                             open_positions=open_positions, bundle=bundle, in_news_blackout=False)
    assert state["tp1_taken"] and state["armed"] and state["volume"] == Decimal("0.70")
    eng._walk_open_positions(bar=_bar(t0, "2011", "2022", "2010", "2021"),
                             open_positions=open_positions, bundle=bundle, in_news_blackout=False)
    assert state["tp2_taken"] and state["volume"] == Decimal("0.40")
    eng._walk_open_positions(bar=_bar(t0, "2021", "2032", "2020", "2031"),
                             open_positions=open_positions, bundle=bundle, in_news_blackout=False)
    assert "p1" not in open_positions  # runner closed at TP3

    closed = journal._trades[tr.id]  # noqa: SLF001
    # 0.3×1R + 0.3×2R + 0.4×3R = 2.1R
    assert closed.r_multiple == pytest.approx(2.1, abs=0.05)
    assert closed.exit_reason.value == "tp3_hit"
    # Review #5: the account balance must move by the TOTAL realized pnl exactly
    # once. TP1 0.3@+10 = +300, TP2 0.3@+20 = +600, runner 0.4@+30 = +1200 → +2100.
    # The old code re-added the partials in _close_position → +3000 (double-count).
    assert eng._paper._account.balance - bal0 == Decimal("2100")  # noqa: SLF001
