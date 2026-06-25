"""Regression: the post-loop flush must NOT price forced exits in the future.

When ``max_bars`` truncates the decision loop, the engine used to close any
still-open position at ``all_bars[end_idx].close`` — the *nominal* window end,
which can be days past the last bar the loop actually walked (and SL-checked).
A short/long force-closed there could book a "loss" far bigger than its own
stop (an impossible loss the broker's SL would have capped at ~1R), which is
exactly what manufactured the −32R outlier in the LLM-in-loop validation
(engine −39R vs the trustworthy exit-replay −4.83R on the SAME tapes).

The fix anchors the flush at the LAST PROCESSED bar (``prev_bar``), matching
the exit-replay (which closes leftover positions at the tape's last bar). This
test pins that: a position open at the truncation point, with price ramping
100 pts adverse AFTER the cutoff, must close at the cutoff price (~0R), never
at the far-future price (~−10R).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

from xauusd_bot.backtest import BacktestEngine, FixedSlippage, FixedSpread
from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import EntryType, ScoreBand
from xauusd_bot.common.schemas.journal import TradeRecord
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.journal import InMemoryJournalStore

FLUSH_PARQUET = Path("/tmp/xauusd_flush_lookahead.parquet")


@pytest.fixture(scope="module", autouse=True)
def _build_flush_parquet() -> None:
    """70 M1 bars: flat ~2000 through the (truncated) processing window, then a
    100-pt ramp DOWN afterward — the 'future' the old flush wrongly priced at.
    """

    t0 = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(70):
        if i <= 12:
            c = 2000.0  # flat through warmup (0..9) + processed window (10..12)
        else:
            # bars 13..69 ramp from 2000 down to ~1900 (the post-cutoff "future")
            c = 2000.0 - (i - 12) * (100.0 / 57.0)
        rows.append(
            {
                "time": t0 + timedelta(minutes=i),
                "open": c,
                "high": c + 0.5,
                "low": c - 0.5,
                "close": c,
                "tick_volume": 100,
            }
        )
    pd.DataFrame(rows).to_parquet(FLUSH_PARQUET)


def test_truncated_flush_closes_at_last_processed_bar() -> None:
    journal = InMemoryJournalStore()
    engine = BacktestEngine(
        connector=ReplayConnector(source_path=FLUSH_PARQUET, symbol="XAUUSD"),
        journal=journal,
        settings=Settings(),  # type: ignore[call-arg]
        slippage_model=FixedSlippage(Decimal("0.50")),
        spread_model=FixedSpread(Decimal("0.30")),
        context_window_bars=100,
    )

    # Force every decision bar to "qualify" so our injector runs; the injector
    # opens exactly one long at the first processed bar (entry 2000, SL 1990,
    # TP 2100 — none of which the flat processed bars hit).
    engine._qualifier.qualify = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda *a, **k: SimpleNamespace(qualified=True)
    )

    opened = {"done": False}

    def _fake_open(*, bar, bundle, decision, score, qualification, snapshot_id, open_positions):  # noqa: ANN001, ANN202
        if opened["done"]:
            return
        opened["done"] = True
        from xauusd_bot.connectors.paper_broker import _OpenPosition

        entry = bar.close
        sl = entry - Decimal("10")
        vol = Decimal("0.10")
        risk = Decimal("100")
        tr = TradeRecord(
            timestamp_open=bar.time, side="long", entry_price=entry, stop_loss=sl,
            volume_lots=vol, risk_amount=risk, setup_id=uuid4(), score=70.0,
            band=ScoreBand.PREPARE_65_74, entry_type=EntryType.SCOUT, fill_price=entry,
        )
        tid = engine._run_async(engine._journal.write_trade(tr))  # noqa: SLF001
        pid = "flushpos"
        engine._paper._positions[pid] = _OpenPosition(  # noqa: SLF001
            spec=engine._spec, side=OrderSide.BUY, volume=vol, open_price=entry,  # noqa: SLF001
            open_time=bar.time, sl=sl, tp=entry + Decimal("100"), magic=0,
            comment="flush-test", position_id=pid, client_order_id="c-flush",
        )
        open_positions[pid] = {
            "trade_id": tid, "side": OrderSide.BUY, "entry_price": entry, "sl": sl,
            "tps": [entry + Decimal("100")], "tp1": None, "tp2": None, "tp3": None,
            "volume": vol, "initial_volume": vol, "risk_amount": risk,
            "initial_risk": Decimal("10"), "entry_time": bar.time, "zone_id": None,
            "tp1_taken": False, "tp2_taken": False, "armed": False,
            "realized_pnl": Decimal("0"), "peak": entry,
        }

    engine._try_open_trade = _fake_open  # type: ignore[assignment, method-assign]  # noqa: SLF001

    result = engine.run(
        start_date=datetime(2026, 4, 1, 0, 10, tzinfo=UTC),
        end_date=datetime(2026, 4, 1, 1, 9, tzinfo=UTC),  # bar 69 (~1900) — the trap
        warmup_bars=10,
        max_bars=3,  # processes bars 10,11,12 then truncates
    )
    assert result.n_bars_processed == 3

    assert len(journal._trades) == 1  # noqa: SLF001
    closed = next(iter(journal._trades.values()))  # noqa: SLF001
    # The single trade must close at the LAST PROCESSED bar (~2000), not the
    # far-future ramp bottom (~1900). Old bug: exit 1900 → r≈-10R (a loss 10×
    # the 1R stop, which the SL would have capped). Fixed: exit ~2000 → r≈0.
    assert closed.exit_price is not None
    assert closed.exit_price > Decimal("1990"), (
        f"forced exit priced in the future: {closed.exit_price} (flush lookahead regressed)"
    )
    assert closed.r_multiple is not None and closed.r_multiple > -1.5, (
        f"impossible loss {closed.r_multiple:.2f}R — bigger than the 1R stop"
    )
    assert closed.exit_reason is not None and closed.exit_reason.value == "manual"
