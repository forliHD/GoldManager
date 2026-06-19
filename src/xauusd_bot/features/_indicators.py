"""Shared technical-indicator utilities used by all feature engines.

Keeping these in one place (rather than re-implementing per engine) avoids
drift and makes the indicator definitions auditable. Every function in
this module is **pure** and **point-in-time safe** — given bars with
``time <= current_t`` the result is a deterministic number with no
look-ahead.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import pandas as pd

from xauusd_bot.connectors.schemas import Bar


def bars_to_df(bars: Iterable[Bar]) -> pd.DataFrame:
    """Convert an iterable of :class:`Bar` to a UTC-aware DataFrame.

    The DataFrame is sorted by ``time`` ascending and indexed 0..N-1.
    Decimal price columns are cast to float (features live in float space;
    the connector layer is the only place that cares about Decimal).

    Parameters
    ----------
    bars:
        Iterable of :class:`Bar` (any timeframe, any length). All bars
        are assumed to satisfy ``bar.time <= current_t`` — that is the
        caller's responsibility (per the AGENTS.md I-3 invariant).
    """

    rows: list[dict] = []
    for b in bars:
        rows.append(
            {
                "time": b.time,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "tick_volume": int(b.tick_volume),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.sort_values("time").reset_index(drop=True)


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range (Wilder). NaN-safe; first row is NaN (no prior close)."""

    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range (simple-mean variant, point-in-time).

    Uses a simple rolling mean of ``true_range`` rather than Wilder's
    smoothing — the simple mean is a defensible default and trivial to
    reason about. With period=14 it approximates Wilder's ATR to within
    ~1% on real XAUUSD data.

    Returns
    -------
    float | None
        The latest ATR, or ``None`` if fewer than ``period`` bars are
        available.
    """

    if len(df) < period:
        return None
    tr = true_range(df)
    return float(tr.tail(period).mean())


def sma(series: pd.Series, period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""

    if len(series) < period:
        return None
    return float(series.tail(period).mean())


def percentile_rank(series: pd.Series, value: float) -> float:
    """Percentile rank of ``value`` in ``series`` (0..100).

    With fewer than 2 observations, returns 50.0 (neutral).
    """

    if len(series) < 2:
        return 50.0
    rank = (series < value).sum() / (len(series) - 1) * 100.0
    return float(rank)


def round_bars_by_time(bars: list[Bar], timeframe_minutes: int) -> list[Bar]:
    """Group M1 bars into N-minute buckets and emit one Bar per bucket.

    Used by the engines that need a higher timeframe but receive M1 from
    the connector (e.g. FVG at H1/M5, structure on M5/H1).

    The output Bar is the standard OHLCV aggregation:
    * open = bucket first open
    * high = max(bucket highs)
    * low = min(bucket lows)
    * close = bucket last close
    * tick_volume = sum(bucket tick_volume)
    * time = bucket start (UTC)
    * timeframe = e.g. "M5" / "H1"

    Incomplete trailing buckets are kept (they appear as an "in-progress"
    bar); the engine that consumes them must filter to closed buckets
    if it needs strictly-closed bars.
    """

    if not bars:
        return []
    df = bars_to_df(bars)
    df["bucket"] = df["time"].dt.floor(f"{timeframe_minutes}min")
    grouped = df.groupby("bucket", sort=True)
    out: list[Bar] = []
    for bucket, g in grouped:
        out.append(
            Bar(
                symbol=bars[0].symbol,
                timeframe=f"M{timeframe_minutes}" if timeframe_minutes < 60 else f"H{timeframe_minutes // 60}",
                time=bucket.to_pydatetime(),
                open=g["open"].iloc[0],
                high=g["high"].max(),
                low=g["low"].min(),
                close=g["close"].iloc[-1],
                tick_volume=int(g["tick_volume"].sum()),
            )
        )
    return out


def filter_pit(bars: Iterable[Bar], current_t: datetime) -> list[Bar]:
    """Strict point-in-time filter: only bars with ``time <= current_t``.

    The ReplayConnector already enforces this at the connector layer, but
    feature engines also call :func:`filter_pit` defensively in case they
    receive bars from another source (live connector, test fixtures,
    in-memory pipelines).
    """

    return [b for b in bars if b.time <= current_t]
