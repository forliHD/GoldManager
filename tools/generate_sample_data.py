"""Deterministic XAUUSD M1 sample-data generator.

Generates 30 days of M1 bars with:

* A geometric-Brownian-motion core in the 2350–2400 range
  (realistic for the 2024–2025 XAUUSD regime).
* Tick-volume in a plausible range (40–400 ticks/min), with a
  bursty session pattern (more ticks during London/NY overlap).
* A handful of **injected** gaps and spikes so the data-quality
  monitor has something to flag in tests.
* Deterministic: ``seed=42`` → identical bytes on every run.

Output: ``data/sample/xauusd_m1_sample.parquet``.
"""

from __future__ import annotations

import argparse
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------- knobs

SEED = 42
SYMBOL = "XAUUSD"
START_PRICE = 2375.00
DRIFT_PER_MIN = 0.0       # mean-zero returns, no drift
VOL_PER_MIN = 0.00015     # ~0.015% per minute, ~0.2% per day realized vol
TICK_VOL_BASE = 120
TICK_VOL_JITTER = 80
DAYS = 30
BARS_PER_DAY = 24 * 60  # 1440

# Number of injected gaps / spikes for data-quality tests.
INJECT_GAPS = 4
INJECT_SPIKES = 2

# Spike magnitude — kept moderate so realistic quality-monitor flags fire
# without wrecking the price range.
SPIKE_MULT = 3.0

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "sample" / "xauusd_m1_sample.parquet"


def _session_intensity(ts: datetime) -> float:
    """0..1 multiplier; higher during London/NY overlap."""

    hour = ts.hour
    # Crude bands (UTC): Asia 0-7 thin, London 7-16 medium, NY 13-21 high, overlap 13-16 highest
    if 13 <= hour <= 16:
        return 1.0
    if 7 <= hour < 13:
        return 0.6
    if 17 <= hour <= 20:
        return 0.7
    return 0.3  # Asia / after-hours


def generate(seed: int = SEED, days: int = DAYS, inject_gaps: int = INJECT_GAPS, inject_spikes: int = INJECT_SPIKES) -> pd.DataFrame:
    rng = random.Random(seed)
    end = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    start = end - timedelta(days=days)
    total = days * BARS_PER_DAY

    # Pre-pick gap positions and spike positions (deterministic via RNG).
    gap_indices = set(rng.sample(range(1, total - 1), k=min(inject_gaps * 2, total // 10)))
    spike_indices = set(rng.sample(range(1, total - 1), k=min(inject_spikes * 2, total // 10)))

    rows: list[dict] = []
    price = START_PRICE
    cur = start
    for i in range(total):
        # Skip gap: leave a 5-minute hole.
        if i in gap_indices and (i + 1) in gap_indices:
            cur += timedelta(minutes=1)
            continue
        intensity = _session_intensity(cur)
        sigma = VOL_PER_MIN * (0.5 + intensity)
        # Log-return ~ N(drift, sigma) — drift is per-minute log-return, very small.
        ret = rng.gauss(DRIFT_PER_MIN, sigma)
        # Apply as a multiplicative return (geometric Brownian motion).
        open_p = price
        close_p = open_p * (1 + ret)
        # Clamp to a plausible band so a sequence of bad luck doesn't
        # drag the price into absurd territory. ±0.3% per minute keeps
        # the cumulative range realistic for XAUUSD.
        close_p = max(close_p, open_p * 0.997)
        close_p = min(close_p, open_p * 1.003)
        # Bar range ~ sigma * open_p * random multiplier
        bar_range = abs(rng.gauss(0.0, sigma * open_p)) * 0.5
        # Spike injection
        if i in spike_indices:
            bar_range *= SPIKE_MULT
        high_p = max(open_p, close_p) + bar_range / 2
        low_p = min(open_p, close_p) - bar_range / 2
        tick_vol = int(TICK_VOL_BASE * intensity + rng.uniform(-TICK_VOL_JITTER, TICK_VOL_JITTER))
        tick_vol = max(10, tick_vol)
        rows.append(
            {
                "time": cur,
                "open": round(open_p, 2),
                "high": round(high_p, 2),
                "low": round(low_p, 2),
                "close": round(close_p, 2),
                "tick_volume": tick_vol,
            }
        )
        price = close_p
        cur += timedelta(minutes=1)

    df = pd.DataFrame(rows)
    return df  # noqa: RET504 — explicit for ruff clarity


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a deterministic XAUUSD M1 sample dataset.")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="Output parquet path.")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--days", type=int, default=DAYS)
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    df = generate(seed=args.seed, days=args.days)
    df.to_parquet(args.out, index=False)
    print(
        f"wrote {len(df):,} M1 bars to {args.out}  "
        f"({df['time'].iloc[0]} → {df['time'].iloc[-1]})  "
        f"price range [{df['low'].min():.2f}, {df['high'].max():.2f}]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
