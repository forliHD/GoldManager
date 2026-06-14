"""End-to-end integration: load the sample, run the full data-layer pipeline,
write a mini equity-curve stub (no decision/execution).

This is a paper-trading-free smoke test of the data layer: it wires
ReplayConnector + OHLCBuilder + SpreadMonitor + DataQualityMonitor +
PreTradeSafetyChecker and runs them against the committed 30-day
XAUUSD M1 sample. The test asserts that:

1. The pipeline runs without exceptions.
2. The quality monitor flags a sane fraction of bars (<= 5%).
3. A mini equity-curve JSON is written to logs/test_equity.json.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure the package is importable even when pytest is invoked from
# outside the repo root.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


SAMPLE_PATH = ROOT / "data" / "sample" / "xauusd_m1_sample.parquet"


def test_end_to_end_replay_pipeline(tmp_path: Path) -> None:
    """Run the full data-layer pipeline on the committed XAUUSD sample.

    Skips if the sample file is missing (fresh-clone edge case)."""

    if not SAMPLE_PATH.exists():
        pytest.skip(f"sample dataset not found at {SAMPLE_PATH}")

    from xauusd_bot.common.logging import setup_logging
    from xauusd_bot.connectors.replay import ReplayConnector
    from xauusd_bot.connectors.safety import (
        PreTradeSafetyChecker,
        SafetyAction,
    )
    from xauusd_bot.connectors.schemas import AccountInfo
    from xauusd_bot.data.ohlc_builder import OHLCBuilder
    from xauusd_bot.data.quality_monitor import DataQualityMonitor
    from xauusd_bot.data.spread_monitor import SpreadMonitor

    setup_logging(level="WARNING")
    n_bars = 5000

    # 1. Load the connector
    connector = ReplayConnector(source_path=SAMPLE_PATH, symbol="XAUUSD")
    spec = connector.spec

    # 2. Build the data layer
    builder = OHLCBuilder(symbol="XAUUSD", source_timeframe="M1")
    spread = SpreadMonitor(
        symbol="XAUUSD",
        point=spec.point,
        window=2000,
        warn_points=50.0,
        block_points=120.0,
    )
    quality = DataQualityMonitor(spec=spec)

    # 3. Build the safety checker with stub account/spread sources
    account_state = {"equity": 10000.0, "trade_allowed": True}

    def _account() -> AccountInfo:
        return AccountInfo(
            login="replay",
            broker="replay",
            balance=Decimal("10000"),
            equity=Decimal(str(account_state["equity"])),
            margin=Decimal("0"),
            free_margin=Decimal(str(account_state["equity"])),
            leverage=100,
            server_time=datetime.now(tz=UTC),
            trade_allowed=account_state["trade_allowed"],
        )

    def _spread() -> float:
        return spread.last

    safety = PreTradeSafetyChecker(
        get_account=_account,
        get_spread_points=_spread,
    )

    # 4. Drive bars through the pipeline
    target = min(n_bars, len(connector.bars))
    for i in range(target):
        row = connector.bars.iloc[i]
        bar = connector._row_to_bar(row, "M1")  # noqa: SLF001 - intentional internal API
        builder.on_bar(bar)
        # Synthesize spread for monitoring.
        synthetic_spread = float((bar.high - bar.low) / spec.point) * 0.1 + 30.0
        spread.update_from_points(synthetic_spread)
        quality.update(bar)

    # 5. Advance cursor and verify point-in-time at the end.
    last_bar_time = connector.bars["time"].iloc[target - 1].to_pydatetime()
    connector.advance_time(last_bar_time)
    visible = connector.get_rates("XAUUSD", "M1", count=5)
    assert all(b.time <= last_bar_time for b in visible)

    # 6. Run the safety check — should be PROCEED for a clean sample.
    verdict = safety.check(datetime.now(tz=UTC))
    assert verdict.action in (SafetyAction.PROCEED, SafetyAction.WARN), (
        f"Expected clean sample to PROCEED or WARN; got {verdict.action}: {verdict.reasons}"
    )

    # 7. Sanity: <= 5% of bars flagged.
    n_flagged = (
        quality.report.n_gaps
        + quality.report.n_spikes
        + quality.report.n_ohlc_inconsistent
        + quality.report.n_spec_drift
    )
    flagged_pct = n_flagged / max(quality.report.n_bars, 1)
    assert flagged_pct <= 0.05, (
        f"Quality monitor flagged {n_flagged}/{quality.report.n_bars} bars "
        f"({flagged_pct:.2%}) — exceeds 5% sanity threshold"
    )

    # 8. Write a mini equity-curve stub to logs/test_equity.json (the
    # task spec requires it). The "equity" is just the running mark
    # at each bar close — no real trades.
    equity_curve = [
        {
            "bar_time": str(connector.bars["time"].iloc[0]),
            "equity": 10000.0,
            "comment": "start (no trades in this smoke)",
        },
        {
            "bar_time": str(connector.bars["time"].iloc[target - 1]),
            "equity": float(verdict.equity or 10000.0),
            "comment": "end (no trades in this smoke)",
        },
    ]
    log_path = ROOT / "logs" / "test_equity.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(equity_curve, indent=2))
    assert log_path.exists()
    assert log_path.stat().st_size > 0

    # 9. Sanity-check that the higher-TF bars were built.
    closed = builder.closed_bars_by_tf
    assert closed.get("M1") is not None and len(closed["M1"]) > 0
    # M5 and H1 are derived from M1; expect non-zero.
    assert closed.get("M5") is not None and len(closed["M5"]) > 0


def test_end_to_end_replay_handles_m1_close_cascade(tmp_path: Path) -> None:
    """A second integration test: with the cascade bug fixed, the M1 close
    count must equal the number of distinct M1 buckets we actually feed.
    """

    if not SAMPLE_PATH.exists():
        pytest.skip(f"sample dataset not found at {SAMPLE_PATH}")

    from xauusd_bot.connectors.replay import ReplayConnector
    from xauusd_bot.data.ohlc_builder import OHLCBuilder

    connector = ReplayConnector(source_path=SAMPLE_PATH, symbol="XAUUSD")
    builder = OHLCBuilder(symbol="XAUUSD", source_timeframe="M1")
    n_bars = 200
    target = min(n_bars, len(connector.bars))
    for i in range(target):
        row = connector.bars.iloc[i]
        bar = connector._row_to_bar(row, "M1")  # noqa: SLF001
        builder.on_bar(bar)

    m1 = builder.closed_bars("M1")
    # We fed `target` bars; all of them are "closed" in the OHLCBuilder
    # bookkeeping sense (the source is closed on arrival).
    assert len(m1) == target
    # First and last M1 bars match the source.
    assert m1[0].time == connector.bars["time"].iloc[0].to_pydatetime()
    assert m1[-1].time == connector.bars["time"].iloc[target - 1].to_pydatetime()
