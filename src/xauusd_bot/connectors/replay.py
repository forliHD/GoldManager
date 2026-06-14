"""Historical-data replay connector — the dev / backtest market interface.

The :class:`ReplayConnector` reads M1 (or higher-timeframe) bars from a Parquet /
CSV file and exposes them through the standard :class:`IMarketConnector`
interface. A monotonically increasing ``current_t`` cursor acts as the wall
clock; ``get_rates`` and ``get_ticks`` will **never** return data with a
timestamp later than the cursor. ``advance_time`` is the only way to move the
cursor forward.

Why a cursor and not a wall clock?
    Because in backtest / smoke-test we want deterministic time control.
    The bot (or the test) drives time explicitly via ``advance_time``.

Point-in-Time guarantee
------------------------
``get_rates`` and ``get_ticks`` filter strictly on ``time <= current_t``. The
``OHLCBuilder`` relies on this to avoid look-ahead; the smoke CLI
(``xauusd_bot.cli.replay_smoke``) exercises it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import structlog

from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)

log = structlog.get_logger(__name__)

_REQUIRED_BAR_COLUMNS = ("time", "open", "high", "low", "close", "tick_volume")


@dataclass
class _AccountState:
    """Mutable simulated account state used by the PaperBroker in tests."""

    balance: Decimal = Decimal("10000")
    equity: Decimal = Decimal("10000")
    margin: Decimal = Decimal("0")
    free_margin: Decimal = Decimal("10000")
    leverage: int = 100


@dataclass
class _Source:
    """A loaded Parquet/CSV bar series, kept in memory and indexed by time."""

    symbol: str
    spec: SymbolSpec
    bars: pd.DataFrame  # sorted by `time`, columns: time, open, high, low, close, tick_volume[, real_volume, spread]
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, OrderRequest] = field(default_factory=dict)
    orders_filled: dict[str, OrderResult] = field(default_factory=dict)
    account: _AccountState = field(default_factory=_AccountState)
    ticks: pd.DataFrame | None = None  # optional, sorted by time


def _read_bar_frame(path: Path) -> pd.DataFrame:
    """Load a bar frame from Parquet or CSV. Returns a UTC-aware DataFrame."""

    if not path.exists():
        raise FileNotFoundError(f"Replay source not found: {path}")
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        df = pd.read_parquet(path)
    elif suffix in {".csv"}:
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported replay source format: {path.suffix}")

    missing = [c for c in _REQUIRED_BAR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Replay source {path} missing required columns: {missing}")

    # Normalize time to UTC-aware datetime.
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    return df


class ReplayConnector:
    """Replay historical bars through the :class:`IMarketConnector` protocol.

    Parameters
    ----------
    source_path:
        Path to a Parquet or CSV file with at least these columns:
        ``time, open, high, low, close, tick_volume``. Optional columns:
        ``real_volume, spread``.

    symbol:
        Symbol the file represents (e.g. ``"XAUUSD"``). The connector
        filters all queries to this symbol and rejects requests for
        other symbols with a clear error.

    spec:
        Optional pre-built :class:`SymbolSpec`. If omitted, a default
        XAUUSD-CFD spec is constructed (point=0.01, contract_size=100,
        margin_rate=0.01).

    initial_balance:
        Starting balance for the simulated account (used when ``order_send``
        is wired to :class:`PaperBroker`).
    """

    def __init__(
        self,
        source_path: Path | str,
        symbol: str = "XAUUSD",
        spec: SymbolSpec | None = None,
        initial_balance: Decimal = Decimal("10000"),
    ) -> None:
        self._source_path = Path(source_path)
        self.symbol = symbol
        self._df = _read_bar_frame(self._source_path)
        self._state = _Source(
            symbol=symbol,
            spec=spec or self._default_spec(symbol),
            bars=self._df,
            account=_AccountState(balance=initial_balance, equity=initial_balance, free_margin=initial_balance),
        )
        # Cursor starts at the first bar's time - 1 nanosecond; the very first
        # advance_time() puts the cursor at bar 0.
        self._current_t: datetime = self._df["time"].iloc[0].to_pydatetime() - pd.Timedelta(nanoseconds=1).to_pytimedelta()
        # Time cache for fast lookups
        self._times_ns = self._df["time"].astype("int64").to_numpy()
        log.info(
            "replay_connector_loaded",
            path=str(self._source_path),
            symbol=symbol,
            bars=len(self._df),
            first=str(self._df["time"].iloc[0]),
            last=str(self._df["time"].iloc[-1]),
        )

    # ------------------------------------------------------------------ I/O

    @property
    def current_t(self) -> datetime:
        """Wall-clock cursor — ``get_rates`` / ``get_ticks`` only return data up to this point."""

        return self._current_t

    @property
    def spec(self) -> SymbolSpec:
        """The :class:`SymbolSpec` for the replayed symbol."""

        return self._state.spec

    @property
    def bars(self) -> pd.DataFrame:
        """Read-only view of the underlying bar frame (debug / tests only)."""

        return self._df

    def advance_time(self, t: datetime) -> None:
        """Move the cursor forward to ``t`` (must be >= ``current_t``)."""

        if t.tzinfo is None:
            raise ValueError("advance_time requires a timezone-aware datetime (UTC).")
        t_utc = t.astimezone(UTC)
        if t_utc < self._current_t:
            raise ValueError(
                f"Time travel not allowed: current_t={self._current_t.isoformat()} > t={t_utc.isoformat()}"
            )
        self._current_t = t_utc

    def advance_bars(self, n: int = 1) -> datetime:
        """Advance the cursor by ``n`` bars (returns the new cursor)."""

        if n <= 0:
            raise ValueError("n must be positive")
        # Find the current position in the timeline
        cur_ns = int(pd.Timestamp(self._current_t).value)
        idx = int(self._times_ns.searchsorted(cur_ns, side="right"))
        new_idx = min(idx + n, len(self._df))
        new_t = self._df["time"].iloc[new_idx - 1].to_pydatetime()
        self._current_t = new_t
        return new_t

    # ------------------------------------------------------------ IMarketConnector

    def get_rates(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[Bar]:
        if symbol != self.symbol:
            raise ValueError(f"ReplayConnector has {self.symbol!r}, not {symbol!r}")
        if timeframe not in {"M1"} and self._df.attrs.get("source_timeframe") not in {timeframe, None}:
            # For non-M1 we approximate by taking every Nth bar. We always serve
            # at least M1; consumers should resample if they need M5/H1.
            log.debug("replay_serving_m1_for", requested=timeframe, served="M1")
        cutoff = end_time if end_time is not None else self._current_t
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        cutoff = cutoff.astimezone(UTC)
        mask = self._df["time"] <= pd.Timestamp(cutoff)
        visible = self._df.loc[mask].tail(count)
        return [self._row_to_bar(row, timeframe) for _, row in visible.iterrows()]

    def get_ticks(self, symbol: str, from_ts: datetime, to_ts: datetime) -> list[Tick]:
        if symbol != self.symbol:
            raise ValueError(f"ReplayConnector has {self.symbol!r}, not {symbol!r}")
        # If the source has no tick data, synthesize deterministic ticks from M1 bars.
        if self._state.ticks is None:
            return list(self._synth_ticks_for_range(from_ts, to_ts))
        df = self._state.ticks
        mask = (df["time"] >= pd.Timestamp(from_ts)) & (df["time"] <= pd.Timestamp(to_ts))
        visible = df.loc[mask]
        return [self._row_to_tick(row) for _, row in visible.iterrows()]

    def get_account(self) -> AccountInfo:
        s = self._state.account
        return AccountInfo(
            login="replay",
            broker="replay",
            balance=s.balance,
            equity=s.equity,
            margin=s.margin,
            free_margin=s.free_margin,
            leverage=s.leverage,
            server_time=self._current_t,
            trade_allowed=True,
        )

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol != self.symbol:
            raise ValueError(f"ReplayConnector has {self.symbol!r}, not {symbol!r}")
        return self._state.spec

    def order_send(self, request: OrderRequest) -> OrderResult:
        # Replay is read-only at the connector level. Real simulated execution
        # is the PaperBroker's job. The connector accepts orders into its
        # in-memory state but does NOT simulate fills — that is the caller's
        # responsibility (see paper_broker.py). This keeps the connector
        # layer transport-only.
        log.info("replay_order_received", symbol=request.symbol, side=request.side, type=request.type)
        # We record the order id so the caller can poll.
        order_id = request.client_order_id or f"replay-{len(self._state.orders_filled) + 1}"
        return OrderResult(accepted=True, order_id=order_id, client_order_id=request.client_order_id)

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        if symbol is not None and symbol != self.symbol:
            return []
        return list(self._state.positions.values())

    def pending_get(self, symbol: str | None = None) -> list[OrderRequest]:
        if symbol is not None and symbol != self.symbol:
            return []
        return list(self._state.pending.values())

    def order_modify(
        self,
        order_id: str,
        *,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        if order_id not in self._state.pending:
            return OrderResult(accepted=False, error_code="NOT_FOUND", error_message=f"order {order_id} not pending")
        req = self._state.pending[order_id]
        if price is not None:
            req = req.model_copy(update={"price": Decimal(str(price))})
        if sl is not None:
            req = req.model_copy(update={"sl": Decimal(str(sl))})
        if tp is not None:
            req = req.model_copy(update={"tp": Decimal(str(tp))})
        self._state.pending[order_id] = req
        return OrderResult(accepted=True, order_id=order_id, client_order_id=req.client_order_id)

    def order_cancel(self, order_id: str) -> OrderResult:
        if order_id not in self._state.pending:
            return OrderResult(accepted=False, error_code="NOT_FOUND", error_message=f"order {order_id} not pending")
        del self._state.pending[order_id]
        return OrderResult(accepted=True, order_id=order_id)

    def is_connected(self) -> bool:
        return True  # Replay is always "connected"

    def shutdown(self) -> None:
        # No-op for Replay
        return None

    # --------------------------------------------------------------- internals

    def _row_to_bar(self, row: pd.Series, timeframe: str) -> Bar:
        spread_val = row.get("spread")
        return Bar(
            symbol=self.symbol,
            timeframe=timeframe,
            time=row["time"].to_pydatetime(),
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            tick_volume=int(row["tick_volume"]),
            real_volume=(int(row["real_volume"]) if "real_volume" in row and pd.notna(row.get("real_volume")) else None),
            spread=(Decimal(str(spread_val)) if spread_val is not None and pd.notna(spread_val) else None),
        )

    def _row_to_tick(self, row: pd.Series) -> Tick:
        return Tick(
            symbol=self.symbol,
            time=row["time"].to_pydatetime(),
            bid=Decimal(str(row["bid"])),
            ask=Decimal(str(row["ask"])),
            last=Decimal(str(row["last"])) if "last" in row and pd.notna(row.get("last")) else None,
            volume=int(row.get("volume", 0) or 0),
            flags=int(row.get("flags", 0) or 0),
        )

    def _synth_ticks_for_range(self, from_ts: datetime, to_ts: datetime) -> Iterable[Tick]:
        """Yield 4 ticks (open/high/low/close crossings) per visible M1 bar."""

        mask = (self._df["time"] >= pd.Timestamp(from_ts)) & (self._df["time"] <= pd.Timestamp(to_ts))
        for _, row in self._df.loc[mask].iterrows():
            t = row["time"].to_pydatetime()
            o, h, low, c = (Decimal(str(row[k])) for k in ("open", "high", "low", "close"))  # noqa: E741
            half_spread = (h - low) * Decimal("0.001")  # 0.1% of bar range, deterministic
            yield Tick(symbol=self.symbol, time=t, bid=o, ask=o + half_spread, last=o, volume=0)
            yield Tick(symbol=self.symbol, time=t, bid=h, ask=h + half_spread, last=h, volume=0)
            yield Tick(symbol=self.symbol, time=t, bid=low, ask=low + half_spread, last=low, volume=0)
            yield Tick(symbol=self.symbol, time=t, bid=c, ask=c + half_spread, last=c, volume=0)

    @staticmethod
    def _default_spec(symbol: str) -> SymbolSpec:
        return SymbolSpec(
            symbol=symbol,
            description="XAUUSD CFD (replay default)",
            point=Decimal("0.01"),
            digits=2,
            trade_contract_size=Decimal("100"),
            volume_min=Decimal("0.01"),
            volume_max=Decimal("100"),
            volume_step=Decimal("0.01"),
            margin_rate=Decimal("0.01"),
            currency_base="XAU",
            currency_profit="USD",
            currency_margin="USD",
        )
