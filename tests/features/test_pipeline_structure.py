"""FeaturePipeline wiring tests — focus on the H1 vs M5 structure split."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.pipeline import FeaturePipeline


def _zigzag_m1(n: int = 600) -> list[Bar]:
    """A slow oscillation (≈3h period) so both M5 and H1 swings form."""

    base = datetime(2026, 6, 22, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    for i in range(n):
        # 180-min period → clear H1 swing highs/lows.
        mid = 4180.0 + 12.0 * math.sin(i / 180.0 * 2 * math.pi)
        o = mid
        c = 4180.0 + 12.0 * math.sin((i + 1) / 180.0 * 2 * math.pi)
        hi = max(o, c) + 0.4
        lo = min(o, c) - 0.4
        bars.append(
            Bar(
                symbol="XAUUSD",
                timeframe="M1",
                time=base + timedelta(minutes=i),
                open=Decimal(str(round(o, 2))),
                high=Decimal(str(round(hi, 2))),
                low=Decimal(str(round(lo, 2))),
                close=Decimal(str(round(c, 2))),
                tick_volume=100,
            )
        )
    return bars


def test_pipeline_emits_h1_and_m5_structure() -> None:
    bars = _zigzag_m1()
    bundle = FeaturePipeline().assemble(bars, bars[-1].time)

    assert bundle.structure is not None
    assert bundle.structure_h1 is not None
    # The H1 engine is configured with fractal_n=2 (to match the fib leg); the
    # M5 engine keeps the default fractal_n=3 — they are genuinely separate engines.
    assert bundle.structure_h1.fractal_n == 2
    assert bundle.structure.fractal_n == 3
    # The H1 engine actually resolved swings on the resampled series, and every
    # H1 swing lands on a whole hour (proves H1 resampling, not M5/M1 minutes).
    assert len(bundle.structure_h1.swings) >= 1
    for s in bundle.structure_h1.swings:
        assert s.time.minute == 0 and s.time.second == 0
