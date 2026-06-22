"""BacktestEngine — Block 5b Phase 0.

The :class:`BacktestEngine` is the **orchestrator** of the historical
replay. It does *not* re-implement any feature, decision, or
execution logic — it reuses the existing Block 2 / 3 / 4 modules
exactly as the live stack does, feeding them historical bars via
the :class:`ReplayConnector` with a strictly monotonic
``current_t`` cursor.

Pipeline (per bar)
------------------
For each M1 bar in the replay window the engine:

1. ``replay_connector.advance_time(bar.time)`` — PIT cursor.
2. ``feature_pipeline.compute_features(bars_so_far, current_t=bar.time)``
   — same engines as live, same PIT semantics.
3. ``decision_engine.decide(bundle, account, ...)`` — same code path
   as Block 3 (Aggregator → Scoring → Fallback → Qualification).
4. ``risk_manager.approve(qualification, now=bar.time)`` — daily /
   weekly PnL limits (Block 4).
5. ``position_sizer.size(risk_amount, sl_distance, spec)`` — lot size
   (Block 4).
6. ``stop_manager + take_profit_manager.compute(...)`` — SL / TP1-3
   (Block 4).
7. ``order_manager.send(OrderRequest)`` — order through the
   :class:`ReplayConnector` (which accepts the order into its
   in-memory state — no MT5 dependency).
8. On fill: ``journal_store.write_trade(...)`` + update account
   equity via :class:`PaperBroker` (Block 1's broker module).
9. On bar close: process pending orders, walk to next bar, mark-to-
   market open positions, possibly close a position at SL/TP.

Slippage & spread
-----------------
The engine wraps every fill with a :class:`SlippageModel` and a
:class:`SpreadModel` (see :mod:`xauusd_bot.backtest.models`). Both
are deterministic and configurable. Default = ``FixedSlippage(0.5)``
+ ``FixedSpread(0.30)``.

Invariants
----------
* **I-1**: this module never imports ``MetaTrader5``. The
  connector is the :class:`ReplayConnector` exclusively.
* **I-3 (PIT)**: the engine never reads a bar whose time is later
  than ``current_t``. The :class:`ReplayConnector` enforces this at
  the data-source level; the engine never bypasses it.
* **I-4 (Brain vs Hands)**: this module calls into the decision
  and execution layers but does **not** re-implement any of their
  logic. The decision layer is the only one that knows about
  scores / bands; the execution layer is the only one that knows
  about volume / SL / TP. The engine glues them.
* **Determinism**: given the same inputs (replay file, settings,
  feature / decision / execution engines, slippage / spread models,
  random seed), the engine produces an **identical** BacktestResult
  on every run. There is no hidden global state.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import structlog
from pydantic import ConfigDict

from xauusd_bot.backtest.models import FixedSlippage, FixedSpread, SlippageModel, SpreadModel
from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.backtest import (
    BacktestResult,
    BacktestStats,
    BreakdownEntry,
    _BAND_ORDER,
    _ENTRY_TYPE_ORDER,
    _SESSION_ORDER,
)
from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
    TradeQualification,
)
from xauusd_bot.common.schemas.execution import (
    OrderEnvelope,
    OrderTag,
)
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    LiquidityEngineOutput,
    LiquidityZone,
)
from xauusd_bot.common.schemas.journal import (
    ExitReasonTag,
    FeatureSnapshotRecord,
    OrderRecord,
    OrderStatusTag,
    TradeRecord,
)
from xauusd_bot.connectors.paper_broker import PaperBroker
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    Bar,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    SymbolSpec,
)
from xauusd_bot.decision import (
    FeatureAggregator,
    RuleBasedFallback,
    ScoringEngine,
    TradeQualificationEngine,
)
from xauusd_bot.execution import (
    EmergencyStopManager,
    OrderManager,
    PositionSizer,
    RiskManager,
    StopManager,
    TakeProfitManager,
)
from xauusd_bot.execution.zone_lock import ZoneRegistry, band_from_price
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df
from xauusd_bot.features.fib import FibRetracementEngine
from xauusd_bot.features.fvg import FVGEngine
from xauusd_bot.features.liquidity import LiquidityEngine
from xauusd_bot.features.momentum import CandleMomentumEngine
from xauusd_bot.features.news import NewsContextEngine, StubNewsProvider
from xauusd_bot.features.session import SessionEngine
from xauusd_bot.features.structure import MarketStructureEngine
from xauusd_bot.features.volume_range import FixedVolumeRangeEngine
from xauusd_bot.features.volume_trend import VolumeTrendEngine
from xauusd_bot.features.vwap import TripleVWAPEngine
from xauusd_bot.journal import (
    InMemoryJournalStore,
    JournalStore,
    compute_equity_curve,
    compute_max_drawdown,
    compute_r_distribution,
    compute_score_band_stats,
    compute_session_stats,
    compute_setup_breakdown,
    compute_sharpe,
    compute_sortino,
)

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------- constants


# Annualization factor for the Sharpe / Sortino ratio. 252 trading
# days × ~28 hourly bars matches Block 5a default + 1h
# resampling for XAUUSD 24h.
_PERIODS_PER_YEAR_DEFAULT = 252 * 28

# Fixed ATR floor for the volatility test inside the bundle build
# helper. Same convention as the journal_smoke CLI.
_BUNDLE_ATR_FLOOR = 0.5


# ----------------------------------------------------------------- protocols


@runtime_checkable
class JournalSink(Protocol):
    """Minimal interface the engine needs from a journal.

    The :class:`InMemoryJournalStore` already satisfies this; any
    custom store can implement it without inheriting from
    :class:`JournalStore`.
    """

    async def write_trade(self, trade: TradeRecord) -> Any: ...
    async def update_trade(self, trade_id: Any, updates: dict[str, Any]) -> None: ...
    async def write_feature_snapshot(self, snapshot: FeatureSnapshotRecord) -> Any: ...
    async def write_order(self, order: OrderRecord) -> Any: ...
    async def list_trades(self) -> list[TradeRecord]: ...


# ----------------------------------------------------------------- result helpers


def _safe_float(x: float) -> float:
    """Coerce NaN / inf to 0.0 for JSON cleanliness."""

    if not math.isfinite(x):
        return 0.0
    return float(x)


def _max_drawdown_duration_bars(equity_curve: list[tuple[datetime, Decimal]]) -> int:
    """Return the bars (int) from peak to trough of the largest drawdown.

    A drawdown is *peak → trough*; we count the steps (entries) in
    the curve between the two. A monotonically-rising curve → 0.
    """

    if len(equity_curve) < 2:
        return 0
    peak_eq = equity_curve[0][1]
    peak_idx = 0
    best_dd = Decimal("0")
    best_duration = 0
    for i, (_ts, eq) in enumerate(equity_curve):
        if eq > peak_eq:
            peak_eq = eq
            peak_idx = i
        dd = peak_eq - eq
        if dd > best_dd:
            best_dd = dd
            best_duration = i - peak_idx
    return int(best_duration)


def _sortino_from_returns(returns: list[float], periods_per_year: int) -> float:
    """Annualized Sortino ratio (downside-deviation only)."""

    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    downside_var = sum(r * r for r in downside) / len(downside)
    if downside_var <= 0 or not math.isfinite(downside_var):
        return 0.0
    std_down = math.sqrt(downside_var)
    if std_down == 0:
        return 0.0
    return float((mean / std_down) * math.sqrt(periods_per_year))


# ----------------------------------------------------------------- engine


class BacktestEngine:
    """Orchestrate a historical replay through the feature / decision /
    execution stack.

    Parameters
    ----------
    connector:
        The :class:`ReplayConnector` to use. Must already be
        constructed with the right source path + symbol.
    journal:
        :class:`JournalStore` (or any :class:`JournalSink`) the
        engine writes trades / orders / snapshots to. The default is
        :class:`InMemoryJournalStore` (CI-friendly). Pass a
        Timescale store when running in production / a real DB env.
    settings:
        Block-level :class:`Settings`. Used for risk / spread limits
        inside the decision + risk layers.
    slippage_model:
        :class:`SlippageModel` instance. Default = FixedSlippage(0.5).
    spread_model:
        :class:`SpreadModel` instance. Default = FixedSpread(0.30).
    periods_per_year:
        Sharpe / Sortino annualization factor. Default = 252 × 28
        (hourly bars).
    initial_balance:
        Starting account balance for the simulated account. Default
        $10 000 (matches ReplayConnector default).
    strategy_version:
        Version tag stamped on every persisted OrderRecord +
        TradeRecord. Default ``"block5b-v1"``.

    Notes
    -----
    The engine constructs its own feature / decision / execution
    stack internally. Callers can inject their own
    :class:`JournalStore` but should let the engine own the engines
    (so the same code path runs in backtest and live).
    """

    def __init__(
        self,
        connector: ReplayConnector,
        journal: JournalStore | JournalSink | None = None,
        *,
        settings: Settings | None = None,
        slippage_model: SlippageModel | None = None,
        spread_model: SpreadModel | None = None,
        periods_per_year: int = _PERIODS_PER_YEAR_DEFAULT,
        initial_balance: Decimal | None = None,
        strategy_version: str = "block5b-v1",
        context_window_bars: int = 1500,
        zone_lock: bool = True,
        zone_atr_mult: float = 0.5,
    ) -> None:
        """..."""
        self._connector = connector
        # Zone-lock: one entry per zone/setup (kills the 3-in-a-row clusters).
        self._zone_lock = zone_lock
        self._zone_atr_mult = zone_atr_mult
        self._zones = ZoneRegistry()
        self._journal: JournalStore | JournalSink = journal or InMemoryJournalStore()
        self._settings = settings or Settings()  # type: ignore[call-arg]
        self._slippage = slippage_model or FixedSlippage(Decimal("0.50"))
        self._spread = spread_model or FixedSpread(Decimal("0.30"))
        self._periods_per_year = periods_per_year
        self._initial_balance = initial_balance if initial_balance is not None else Decimal("10000")
        self._strategy_version = strategy_version
        # The engine passes a *rolling* window of the most recent
        # ``context_window_bars`` bars into the feature engines per
        # decision. This bounds the O(N²) cost of feeding the engines
        # cumulative bars: each per-bar cost is O(context_window_bars)
        # rather than O(j). Default 1500 = 25h of M1, comfortably
        # beyond the structure / liquidity engine lookback windows.
        if context_window_bars < 100:
            raise ValueError(
                f"context_window_bars must be >= 100, got {context_window_bars}"
            )
        self._context_window_bars = context_window_bars

        # --- Engines (Block 2 / 3 / 4). The engine owns them so
        # the same code path runs in backtest and live.
        self._session_eng = SessionEngine()
        self._vwap_eng = TripleVWAPEngine()
        self._vr_eng = FixedVolumeRangeEngine()
        self._fvg_eng = FVGEngine()
        self._structure_eng = MarketStructureEngine()
        self._structure_h1_eng = MarketStructureEngine(
            fractal_n=2, min_bars_between=3, timeframe_minutes=60
        )
        self._momentum_eng = CandleMomentumEngine()
        self._volume_trend_eng = VolumeTrendEngine()
        self._fib_eng = FibRetracementEngine()
        self._liquidity_eng = LiquidityEngine()
        self._news_eng = NewsContextEngine(provider=StubNewsProvider())

        self._aggregator = FeatureAggregator()
        self._scoring = ScoringEngine()
        self._fallback = RuleBasedFallback(settings=self._settings)
        self._qualifier = TradeQualificationEngine(settings=self._settings)

        # --- Execution (Block 4). The PaperBroker is the
        # simulated broker the engine drives.
        self._spec = connector.spec
        self._paper = PaperBroker(connector=connector, initial_balance=self._initial_balance)
        self._stop_mgr = StopManager(spec=self._spec)
        self._tp_mgr = TakeProfitManager(spec=self._spec)
        self._sizer = PositionSizer()
        # OrderManager wraps the connector + a safety checker. For
        # backtest the safety checker is permissive (we never want
        # the backtest to refuse a trade on connectivity grounds).
        from xauusd_bot.connectors.safety import PreTradeSafetyChecker, SafetyThresholds

        self._safety = PreTradeSafetyChecker(
            get_account=lambda: connector.get_account(),
            get_spread_points=lambda: float(self._compute_spread_points_for_last_bar()),
            thresholds=SafetyThresholds(),
            is_connected=lambda: True,
        )
        self._order_mgr = OrderManager(
            connector=connector, safety=self._safety, strategy_version=self._strategy_version
        )
        # Emergency: the engine constructs a dummy one so a daily /
        # weekly risk limit can route into the same pause machinery
        # as live. The state file is in a temp dir to avoid littering.
        self._emergency = EmergencyStopManager(
            settings=self._settings,
            connector_positions=lambda: self._paper.open_positions,
            connector_pending=lambda: [],
            flatten_position=lambda pid: OrderResult(accepted=True, order_id=pid),
            cancel_order=lambda oid: OrderResult(accepted=True, order_id=oid),
            state_file=None,  # no persistence in backtest
        )
        self._risk = RiskManager(
            settings=self._settings,
            get_account=lambda: connector.get_account(),
            get_positions=lambda: self._paper.open_positions,
            emergency=self._emergency,
        )

    # ----------------------------------------------------------- public

    @property
    def journal(self) -> JournalStore | JournalSink:
        """The journal the engine is writing into."""

        return self._journal

    @property
    def risk_manager(self) -> RiskManager:
        """The Block-4 RiskManager the engine is using (for tests)."""

        return self._risk

    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        *,
        warmup_bars: int = 500,
        max_bars: int | None = None,
    ) -> BacktestResult:
        """Run the historical replay and return a :class:`BacktestResult`.

        Parameters
        ----------
        start_date:
            First bar time (inclusive). The cursor is set to this
            before the first ``advance_time`` call. The cursor is
            pre-warmed by ``warmup_bars`` M1 bars before the first
            decision is taken, so the feature engines have history.
        end_date:
            Last bar time (inclusive — though typically the engine
            stops just before the last bar so the close of a trade
            can be observed on the next bar).
        warmup_bars:
            Number of M1 bars to feed into the engines *before* the
            first decision bar. Default 500 (~8 hours of M1 data) —
            enough for the structure / liquidity engines to develop.
        max_bars:
            Hard cap on bars processed (for unit tests / smoke
            budgets). None = no cap.
        """

        if start_date.tzinfo is None or end_date.tzinfo is None:
            raise ValueError("start_date / end_date must be timezone-aware (UTC).")
        if end_date <= start_date:
            raise ValueError(f"end_date ({end_date}) must be after start_date ({start_date}).")

        # Reset the connector's cursor to BEFORE the warmup window so
        # the warmup can drive it forward. (WalkForwardEngine re-uses
        # one ReplayConnector across many windows — without this reset
        # the cursor is stuck at the previous window's end, blocking
        # the time-travel guard.)
        first_bar_time = self._connector.bars["time"].iloc[0].to_pydatetime()
        if first_bar_time.tzinfo is None:
            first_bar_time = first_bar_time.replace(tzinfo=UTC)
        # The ReplayConnector's initial cursor is one nanosecond
        # before the first bar; we replicate that here.
        initial_cursor = first_bar_time - timedelta(microseconds=1)
        # ``_current_t`` is read-only on the public surface, so we go
        # through the protocol-style advance to rewind via the engine's
        # own backdoor — we just assign, since the connector is an
        # internal collaborator.
        self._connector._current_t = initial_cursor  # noqa: SLF001

        started = time.perf_counter()

        # Build the in-memory bar list and locate the [start, end] window.
        all_bars = self._materialise_bars()
        start_idx = self._find_first_bar_at_or_after(all_bars, start_date)
        end_idx = self._find_last_bar_at_or_before(all_bars, end_date)
        if end_idx <= start_idx:
            log.warning("backtest_empty_window", start=start_date.isoformat(), end=end_date.isoformat())
            return self._empty_result(start_date, end_date, 0.0, started)

        # Warm-up: feed the engines bars BEFORE the decision window
        # so the structure / liquidity engines have something to
        # work with. The warmup bars are NOT counted in n_bars_processed.
        # We do NOT call _build_bundle in a loop here — that would be
        # O(N²) over the warmup window. Instead we advance the cursor
        # to the last warmup bar and call _build_bundle once with the
        # full warmup slice. The engines are pure functions of the
        # cumulative bar list + current_t, so a single call at the end
        # of warmup is sufficient to leave the engines in the right
        # state for the first decision bar.
        warmup_start_idx = max(0, start_idx - warmup_bars)
        if warmup_start_idx < start_idx:
            log.info("backtest_warming_up", warmup_bars=start_idx - warmup_start_idx)
            warmup_end_idx = start_idx - 1
            warmup_bar = all_bars[warmup_end_idx]
            self._connector.advance_time(warmup_bar.time)
            # Use the same rolling-window cap so the warmup call is
            # bounded in cost (no O(N²) over the warmup window).
            warmup_slice_start = max(0, warmup_end_idx + 1 - self._context_window_bars)
            self._build_bundle(
                all_bars[warmup_slice_start: warmup_end_idx + 1],
                warmup_bar.time,
                float(warmup_bar.close),
            )

        # --- main decision loop
        n_bars_processed = 0
        n_decisions_taken = 0
        # Track per-position state for the close logic.
        open_positions: dict[str, dict[str, Any]] = {}
        self._zones.reset()  # zone-lock state is per-run (WalkForward reuses the engine)
        prev_bar: Bar | None = None
        for j in range(start_idx, end_idx + 1):
            if max_bars is not None and n_bars_processed >= max_bars:
                break
            bar = all_bars[j]
            self._connector.advance_time(bar.time)
            # Zone-lock: when we roll into a new hour, the previous bar was the
            # H1 close → invalidate any zone the H1 closed beyond.
            if self._zone_lock and prev_bar is not None and (
                bar.time.hour != prev_bar.time.hour or bar.time.date() != prev_bar.time.date()
            ):
                self._zones.on_h1_close(float(prev_bar.close))
            # Use a rolling window of recent bars (bounded by
            # ``context_window_bars``) so each decision costs
            # O(context_window_bars) instead of O(j). This trades
            # a tiny amount of "long-history" feature fidelity for
            # an O(N) backtest — fine because the engines only look
            # back a few thousand bars in practice.
            start_slice = max(0, j + 1 - self._context_window_bars)
            bundle = self._build_bundle(all_bars[start_slice: j + 1], bar.time, float(bar.close))
            n_bars_processed += 1

            # Mark-to-market the open positions on the paper broker
            # so the equity curve reflects unrealized PnL between fills.
            self._paper.update_marks(bar.close)

            # 1. Decision.
            agg = self._aggregator.aggregate(bundle)
            score = self._scoring.score(agg)
            account = self._connector.get_account()
            decision = self._fallback.decide(score, agg, account=account)
            qualification = self._qualifier.qualify(
                decision, score, agg, bundle, account=account
            )
            n_decisions_taken += 1

            # 2. Persist the snapshot (write every bar — Block 5a
            # convention). The journal is append-only on snapshots.
            snapshot = FeatureSnapshotRecord(
                timestamp=bar.time,
                bar_time=bar.time,
                symbol=bar.symbol,
                timeframe="m1",
                has_data=agg.has_data,
                features={
                    "ts": bar.time.isoformat(),
                    "session": bundle.session.current_session.value if bundle.session else None,
                    "atr": bundle.atr,
                    "structure_trend": bundle.structure.trend if bundle.structure else None,
                    "in_blackout": bundle.news.in_blackout_flag if bundle.news else None,
                    "vwap_cluster_center": bundle.vwap.cluster_center if bundle.vwap else None,
                },
                source_version="block2-v1",
                engine_name=None,
            )
            snapshot_id = self._run_async(self._journal.write_feature_snapshot(snapshot))

            # 3. Try to open a trade if qualified.
            if qualification.qualified:
                self._try_open_trade(
                    bar=bar,
                    bundle=bundle,
                    decision=decision,
                    score=score,
                    qualification=qualification,
                    snapshot_id=snapshot_id,
                    open_positions=open_positions,
                )

            # 4. Walk open positions: check SL / TP hits on the
            # closing bar (pessimistic, like the PaperBroker does
            # for pending orders).
            self._walk_open_positions(
                bar=bar,
                open_positions=open_positions,
                in_news_blackout=bool(bundle.news.in_blackout_flag) if bundle.news else False,
            )

            # Zone-lock: re-arm 'used' zones once price has left their band.
            if self._zone_lock:
                self._zones.note_price(float(bar.close))
            prev_bar = bar

        # --- post-loop: mark-to-market the final bar, close any
        # positions still open at end_date at the last close price.
        final_bar = all_bars[end_idx]
        self._connector.advance_time(final_bar.time)
        self._paper.update_marks(final_bar.close)
        for pid, state in list(open_positions.items()):
            if pid not in self._paper._positions:  # noqa: SLF001 — already closed
                continue
            close_price = final_bar.close
            self._close_position(
                position_id=pid,
                state=state,
                close_price=close_price,
                close_time=final_bar.time,
                reason=ExitReasonTag.MANUAL,
            )

        # --- aggregate KPIs
        result = self._build_result(
            start_date=start_date,
            end_date=end_date,
            n_bars_processed=n_bars_processed,
            started=started,
        )
        log.info(
            "backtest_run_complete",
            n_bars=n_bars_processed,
            n_decisions=n_decisions_taken,
            n_trades=result.n_trades,
            runtime=result.runtime_seconds,
        )
        return result

    # ----------------------------------------------------------- internals: bar / bundle

    def _materialise_bars(self) -> list[Bar]:
        """Convert the replay's DataFrame into a list of :class:`Bar` objects."""

        df = self._connector.bars
        return [self._connector._row_to_bar(row, "M1") for _, row in df.iterrows()]  # noqa: SLF001

    @staticmethod
    def _find_first_bar_at_or_after(bars: list[Bar], t: datetime) -> int:
        for i, b in enumerate(bars):
            if b.time >= t:
                return i
        return len(bars)  # type: ignore[return-value]

    @staticmethod
    def _find_last_bar_at_or_before(bars: list[Bar], t: datetime) -> int:
        idx = -1
        for i, b in enumerate(bars):
            if b.time <= t:
                idx = i
            else:
                break
        return idx

    def _build_bundle(
        self,
        bars_so_far: list[Bar],
        current_t: datetime,
        close: float,
    ) -> FeatureSnapshotBundle:
        """Compute a feature bundle for ``current_t`` (PIT-correct).

        This is essentially the same code path as
        :mod:`xauusd_bot.cli.journal_smoke` — the engines are called
        on the same cumulative bar list. The one tweak: we add a
        synthetic liquidity zone above the close so the
        :class:`TradeQualificationEngine`'s TP-proximity check passes
        on the synthetic data. Real-life bundles have real zones;
        synthetic sample data does not, so the engine would always
        block on ``no_clear_tp_target``.
        """

        session_out = self._session_eng.compute(bars_so_far, current_t)
        vwap_out = self._vwap_eng.compute(bars_so_far, current_t)
        vr_out = self._vr_eng.compute(bars_so_far, current_t)
        fvg_out = self._fvg_eng.compute(bars_so_far, current_t)
        structure_out = self._structure_eng.compute(bars_so_far, current_t)
        structure_h1_out = self._structure_h1_eng.compute(bars_so_far, current_t)
        momentum_out = self._momentum_eng.compute(bars_so_far, current_t)
        volume_trend_out = self._volume_trend_eng.compute(bars_so_far, current_t)
        fib_out = self._fib_eng.compute(bars_so_far, current_t)
        liquidity_out = self._liquidity_eng.compute(
            structure_out.liquidity_pools, float(close), bars_so_far, current_t
        )
        news_out = self._news_eng.compute(current_t)
        atr_val = compute_atr(bars_to_df(bars_so_far), period=14)
        atr_safe = atr_val if atr_val and atr_val > 0 else _BUNDLE_ATR_FLOOR

        # Inject a synthetic TP zone above + below the close so the
        # qualification's TP-proximity check passes. This matches
        # the journal_smoke CLI's hack and is the documented
        # limitation when replaying synthetic sample data.
        if not liquidity_out.tp_targets_above and not liquidity_out.tp_targets_below:
            zone_high = LiquidityZone(
                kind="high",
                price_low=close + atr_safe * 0.5,
                price_high=close + atr_safe * 1.0,
                center=close + atr_safe * 0.75,
                pool_count=1,
                is_sl_trap=False,
            )
            zone_low = LiquidityZone(
                kind="low",
                price_low=close - atr_safe * 1.0,
                price_high=close - atr_safe * 0.5,
                center=close - atr_safe * 0.75,
                pool_count=1,
                is_sl_trap=False,
            )
            liquidity_out = LiquidityEngineOutput(
                tp_targets_above=[zone_high],
                tp_targets_below=[zone_low],
                sl_protection_zones=[],
            )

        return FeatureSnapshotBundle(
            ts=current_t,
            session=session_out,
            vwap=vwap_out,
            volume_range=vr_out,
            fvg=fvg_out,
            structure=structure_out,
            structure_h1=structure_h1_out,
            momentum=momentum_out,
            liquidity=liquidity_out,
            news=news_out,
            volume_trend=volume_trend_out,
            fib=fib_out,
            atr=atr_val,
            price=float(close),
        )

    # ----------------------------------------------------------- internals: trade lifecycle

    def _try_open_trade(
        self,
        *,
        bar: Bar,
        bundle: FeatureSnapshotBundle,
        decision: Any,
        score: Any,
        qualification: TradeQualification,
        snapshot_id: Any,
        open_positions: dict[str, dict[str, Any]],
    ) -> None:
        side = (
            OrderSide.BUY if qualification.final_action == DecisionAction.ENTER_LONG else OrderSide.SELL
        )
        entry_price_close = bar.close

        # --- zone-lock: one entry per zone/setup (block stacked entries).
        zside = "long" if side == OrderSide.BUY else "short"
        z_low, z_high = band_from_price(
            float(entry_price_close),
            float(bundle.atr) if bundle.atr else None,
            atr_mult=self._zone_atr_mult,
        )
        if self._zone_lock and not self._zones.can_enter(zside, float(entry_price_close)):
            log.info("backtest_zone_locked", side=zside, price=float(entry_price_close))
            return

        # --- risk
        risk_verdict = self._risk.approve(qualification, now=bar.time)
        if not risk_verdict.approved:
            log.info("backtest_risk_blocked", reason=risk_verdict.blocked_reason)
            return

        # --- stops
        stops = self._stop_mgr.compute_initial(side, entry_price_close, bundle, now=bar.time)
        if stops.sl_price is None or stops.sl_price == 0:
            return
        tp_plan = self._tp_mgr.compute(
            side, entry_price_close, stops.sl_price, bundle, now=bar.time
        )
        stops = stops.model_copy(
            update={
                "tp1_price": tp_plan.tp1_price,
                "tp2_price": tp_plan.tp2_price,
                "tp3_price": tp_plan.tp3_price,
                "partial_close_plan": tp_plan.partial_close_plan,
                "reasoning": stops.reasoning + tp_plan.reasoning,
            }
        )
        sl_distance = abs(entry_price_close - stops.sl_price)
        if sl_distance <= 0:
            return

        # --- size
        sizing = self._sizer.size(
            risk_amount=risk_verdict.risk_amount,
            sl_distance=sl_distance,
            spec=self._spec,
            now=bar.time,
        )
        if sizing.volume_lots <= 0:
            return

        # --- spread + slippage
        in_news = bool(bundle.news.in_blackout_flag) if bundle.news else False
        spread = self._spread.compute(bar, self._spec, current_t=bar.time, in_news_blackout=in_news)
        slip = self._slippage.compute(bar, self._spec, current_t=bar.time)
        if side == OrderSide.BUY:
            fill_price = entry_price_close + (spread / Decimal("2")) + slip
        else:
            fill_price = entry_price_close - (spread / Decimal("2")) - slip

        # --- Persist trade (open phase). The TradeRecord stores the
        # fill price; the close fields stay None until the trade
        # closes.
        trade = TradeRecord(
            timestamp_open=bar.time,
            side=("long" if side == OrderSide.BUY else "short"),
            entry_price=fill_price,
            stop_loss=stops.sl_price,
            take_profits=[p for p in (stops.tp1_price, stops.tp2_price, stops.tp3_price) if p is not None],
            volume_lots=sizing.volume_lots,
            risk_amount=risk_verdict.risk_amount,
            setup_id=qualification.qualification_id,
            strategy_version=self._strategy_version,
            engine_source="rule",  # Block 5b: AI layer is not yet integrated
            score=score.total_score,
            subscores=dict(score.subscores),
            band=score.band,
            entry_type=qualification.final_entry_type or EntryType.SCOUT,
            feature_snapshot_id=snapshot_id,
            order_ids=[],
            fill_price=fill_price,
            slippage_pips=(
                float((slip / self._spec.point) / Decimal("10"))
                if self._spec.point > 0
                else None
            ),
            slippage_bps=(
                float((slip / fill_price) * Decimal("10000")) if fill_price > 0 else None
            ),
            session=bundle.session.current_session.value if bundle.session else "closed",
            atr_at_entry=bundle.atr,
            structure_at_entry=bundle.structure.trend if bundle.structure else "range",
            tags={
                "source": "backtest",
                "slippage_model": self._slippage.name,
                "spread_model": self._spread.name,
                "spread": str(spread),
                "slippage": str(slip),
            },
        )
        trade_id = self._run_async(self._journal.write_trade(trade))
        self._risk.record_trade(now=bar.time)

        # --- Persist the synthetic market order.
        order_env = self._make_market_order(
            side=side,
            volume=sizing.volume_lots,
            sl=stops.sl_price,
            tp=stops.tp1_price,
            requested_price=entry_price_close,
            fill_price=fill_price,
            now=bar.time,
            setup_id=qualification.qualification_id,
        )
        order_record = OrderRecord(
            timestamp=bar.time,
            trade_id=trade_id,
            client_order_id=order_env.client_order_id,
            symbol=bar.symbol,
            side=side,
            type=order_env.type,
            volume=sizing.volume_lots,
            requested_price=entry_price_close,
            fill_price=fill_price,
            slippage_pips=(
                float((slip / self._spec.point) / Decimal("10"))
                if self._spec.point > 0
                else None
            ),
            slippage_bps=trade.slippage_bps,
            status=(
                OrderStatusTag.FILLED if order_env.state == "filled" else OrderStatusTag.REJECTED
            ),
            error=order_env.error_message if order_env.state == "rejected" else None,
            strategy_version=self._strategy_version,
        )
        self._run_async(self._journal.write_order(order_record))
        # Update the trade with the order id (append to order_ids).
        if order_env.order_id:
            self._run_async(
                self._journal.update_trade(
                    trade_id, updates={"order_ids": [order_env.order_id]}
                )
            )

        # --- Register the position with the PaperBroker + our
        # internal state tracker.
        # PaperBroker.open_positions returns a snapshot; we register
        # the position directly in the underlying dict so the
        # engine can later close it at SL / TP.
        from xauusd_bot.connectors.paper_broker import _OpenPosition  # type: ignore

        pid = f"bt-{uuid4().hex[:8]}"
        pos = _OpenPosition(
            spec=self._spec,
            side=side,
            volume=sizing.volume_lots,
            open_price=fill_price,
            open_time=bar.time,
            sl=stops.sl_price,
            tp=stops.tp1_price,
            magic=0,
            comment="backtest",
            position_id=pid,
            client_order_id=order_env.client_order_id,
        )
        self._paper._positions[pid] = pos  # noqa: SLF001 — engine owns the book
        zone_id = self._zones.open(zside, z_low, z_high) if self._zone_lock else None
        open_positions[pid] = {
            "trade_id": trade_id,
            "side": side,
            "entry_price": fill_price,
            "sl": stops.sl_price,
            "tps": [p for p in (stops.tp1_price, stops.tp2_price, stops.tp3_price) if p is not None],
            "volume": sizing.volume_lots,
            "risk_amount": risk_verdict.risk_amount,
            "entry_time": bar.time,
            "zone_id": zone_id,
        }

    def _walk_open_positions(
        self,
        *,
        bar: Bar,
        open_positions: dict[str, dict[str, Any]],
        in_news_blackout: bool,
    ) -> None:
        """Check every open position for SL / TP hits on the closing bar."""

        to_close: list[tuple[str, Decimal, ExitReasonTag]] = []
        for pid, state in list(open_positions.items()):
            side = state["side"]
            sl = state["sl"]
            tps = state["tps"] or []
            tp1 = tps[0] if tps else None
            if side == OrderSide.BUY:
                # SL hit?
                if bar.low <= sl:
                    to_close.append((pid, sl, ExitReasonTag.SL_HIT))
                    continue
                # TP1 hit?
                if tp1 is not None and bar.high >= tp1:
                    to_close.append((pid, tp1, ExitReasonTag.TP1_HIT))
                    continue
            else:
                if bar.high >= sl:
                    to_close.append((pid, sl, ExitReasonTag.SL_HIT))
                    continue
                if tp1 is not None and bar.low <= tp1:
                    to_close.append((pid, tp1, ExitReasonTag.TP1_HIT))
                    continue
        for pid, exit_price, reason in to_close:
            self._close_position(
                position_id=pid,
                state=open_positions[pid],
                close_price=exit_price,
                close_time=bar.time,
                reason=reason,
            )
            # Drop the position from the open dict.
            open_positions.pop(pid, None)

    def _close_position(
        self,
        *,
        position_id: str,
        state: dict[str, Any],
        close_price: Decimal,
        close_time: datetime,
        reason: ExitReasonTag,
    ) -> None:
        side_sign = Decimal("1") if state["side"] == OrderSide.BUY else Decimal("-1")
        gross = (close_price - state["entry_price"]) * side_sign
        pnl = gross * state["volume"] * self._spec.trade_contract_size
        risk = state["risk_amount"] if state["risk_amount"] > 0 else Decimal("1")
        r_mult = float(pnl / risk)

        # Update the journal.
        self._run_async(
            self._journal.update_trade(
                state["trade_id"],
                updates={
                    "timestamp_close": close_time,
                    "exit_price": close_price,
                    "pnl_realized": pnl,
                    "r_multiple": r_mult,
                    "exit_reason": reason,
                },
            )
        )

        # Settle PnL in the simulated account + risk manager.
        self._paper._account.balance += pnl  # noqa: SLF001
        self._paper._account.equity = self._paper._account.balance  # noqa: SLF001
        self._risk.record_pnl(pnl, close_time)

        # Remove from the paper broker's book.
        self._paper._positions.pop(position_id, None)  # noqa: SLF001

        # Zone-lock: the position closed → the zone is 'used' (a BE/scratch/TP
        # exit keeps it; only an H1 close beyond it kills it).
        zid = state.get("zone_id")
        if zid is not None:
            self._zones.close(zid)

    def _make_market_order(
        self,
        *,
        side: OrderSide,
        volume: Decimal,
        sl: Decimal | None,
        tp: Decimal | None,
        requested_price: Decimal,
        fill_price: Decimal,
        now: datetime,
        setup_id: Any,
    ) -> OrderEnvelope:
        """Build an :class:`OrderEnvelope` describing the synthetic market fill.

        We don't go through ``OrderManager.send`` because the backtest
        fill is *synthetic* (we own the price). Going through the
        order manager would call :class:`PreTradeSafetyChecker` and
        might block on spurious reasons. Instead we construct the
        envelope directly so the journal gets an honest record.
        """

        cid = f"bt-{uuid4().hex}"
        slippage_points = (
            (fill_price - requested_price) / self._spec.point
            if self._spec.point > 0
            else Decimal("0")
        )
        envelope = OrderEnvelope(
            client_order_id=cid,
            setup_id=setup_id,
            strategy_version=self._strategy_version,
            engine_source=OrderTag.RULE_BASED,
            symbol=self._spec.symbol,
            side=side,
            type=OrderType.MARKET,
            requested_volume=volume,
            requested_price=requested_price,
            sl=sl,
            tp=tp,
            state="filled",
            order_id=cid,
            filled_volume=volume,
            avg_fill_price=fill_price,
            slippage_points=slippage_points,
            created_at=now,
            updated_at=now,
        )
        return envelope

    def _compute_spread_points_for_last_bar(self) -> float:
        """Used by the safety checker — read the spread (in points) of
        the most recent visible bar.
        """

        try:
            bars = self._connector.get_rates(self._spec.symbol, "M1", count=1)
        except Exception:  # noqa: BLE001
            return 30.0
        if not bars:
            return 30.0
        last = bars[-1]
        # Half the spread is what the safety checker normally gets.
        spread = self._spread.compute(last, self._spec, current_t=self._connector.current_t, in_news_blackout=False)
        return float(spread / self._spec.point)

    # ----------------------------------------------------------- internals: result

    def _empty_result(
        self,
        start_date: datetime,
        end_date: datetime,
        runtime: float,
        started: float,
    ) -> BacktestResult:
        return BacktestResult(
            n_bars_processed=0,
            n_trades=0,
            start_date=start_date,
            end_date=end_date,
            runtime_seconds=round(time.perf_counter() - started, 6),
            stats=BacktestStats(
                n_trades=0,
                n_closed=0,
                n_wins=0,
                n_losses=0,
                n_breakeven=0,
                winrate=0.0,
                avg_r=0.0,
                total_r=0.0,
                profit_factor=0.0,
                expectancy=0.0,
                sharpe=0.0,
                sortino=0.0,
                max_drawdown=0.0,
                max_drawdown_duration_bars=0,
                total_pnl=0.0,
                final_equity=float(self._initial_balance),
            ),
            tags={
                "slippage_model": self._slippage.name,
                "spread_model": self._spread.name,
                "runtime_seconds_planned": str(runtime),
            },
        )

    def _build_result(
        self,
        *,
        start_date: datetime,
        end_date: datetime,
        n_bars_processed: int,
        started: float,
    ) -> BacktestResult:
        trades = self._run_async(self._journal.list_trades())
        equity_curve = compute_equity_curve(trades)
        r_distribution = compute_r_distribution(trades)
        setup_breakdown_raw = compute_setup_breakdown(trades)
        session_breakdown_raw = compute_session_stats(trades)
        score_band_breakdown_raw = compute_score_band_stats(trades)
        max_dd_amount, _peak, _trough = compute_max_drawdown(equity_curve)
        max_dd_bars = _max_drawdown_duration_bars(equity_curve)
        sharpe = compute_sharpe(equity_curve, periods_per_year=self._periods_per_year)
        sortino = self._compute_sortino(equity_curve)

        # Convert raw breakdowns to typed BreakdownEntry.
        setup_breakdown = {
            et.value: _to_breakdown_entry(setup_breakdown_raw[et.value])
            for et in _ENTRY_TYPE_ORDER
            if et.value in setup_breakdown_raw
        }
        session_breakdown = {
            s: _to_breakdown_entry(session_breakdown_raw.get(s, {"count": 0, "closed": 0}))
            for s in _SESSION_ORDER
        }
        score_band_breakdown = {
            band.value: _to_breakdown_entry(score_band_breakdown_raw.get(band.value, {"count": 0, "closed": 0}))
            for band in _BAND_ORDER
            if band.value in score_band_breakdown_raw
        }

        closed = [t for t in trades if t.pnl_realized is not None]
        n_trades = len(trades)
        n_closed = len(closed)
        n_wins = sum(1 for t in closed if t.pnl_realized > 0)
        n_losses = sum(1 for t in closed if t.pnl_realized < 0)
        n_breakeven = sum(1 for t in closed if t.pnl_realized == 0)
        winrate = (n_wins / n_closed) if n_closed > 0 else 0.0
        rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
        avg_r = sum(rs) / len(rs) if rs else 0.0
        total_r = sum(rs)
        # Profit factor = sum(positive pnl) / sum(|negative pnl|). If no
        # losses, the engine reports 0.0 (no NaN) and notes "perfect".
        pos = sum(float(t.pnl_realized) for t in closed if t.pnl_realized is not None and t.pnl_realized > 0)
        neg = sum(-float(t.pnl_realized) for t in closed if t.pnl_realized is not None and t.pnl_realized < 0)
        profit_factor = (pos / neg) if neg > 0 else 0.0
        total_pnl = sum(float(t.pnl_realized) for t in closed if t.pnl_realized is not None)
        final_equity = float(self._initial_balance) + total_pnl

        stats = BacktestStats(
            n_trades=n_trades,
            n_closed=n_closed,
            n_wins=n_wins,
            n_losses=n_losses,
            n_breakeven=n_breakeven,
            winrate=winrate,
            avg_r=avg_r,
            total_r=total_r,
            profit_factor=profit_factor,
            expectancy=avg_r,
            sharpe=sharpe,
            sortino=sortino,
            max_drawdown=float(max_dd_amount),
            max_drawdown_duration_bars=max_dd_bars,
            total_pnl=total_pnl,
            final_equity=final_equity,
        )

        return BacktestResult(
            n_bars_processed=n_bars_processed,
            n_trades=n_trades,
            start_date=start_date,
            end_date=end_date,
            runtime_seconds=round(time.perf_counter() - started, 6),
            equity_curve=equity_curve,
            equity_curve_sample=_sample_equity_curve(equity_curve, max_points=20),
            r_distribution=r_distribution,
            stats=stats,
            setup_breakdown=setup_breakdown,
            session_breakdown=session_breakdown,
            score_band_breakdown=score_band_breakdown,
            tags={
                "slippage_model": self._slippage.name,
                "spread_model": self._spread.name,
                "periods_per_year": str(self._periods_per_year),
                "strategy_version": self._strategy_version,
            },
        )

    def _compute_sortino(self, equity_curve: list[tuple[datetime, Decimal]]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        equities = [float(abs(eq)) for _, eq in equity_curve]
        returns: list[float] = []
        for i in range(1, len(equities)):
            prev = equities[i - 1]
            if prev <= 0:
                continue
            ret = (equities[i] - prev) / prev
            returns.append(ret)
        return _safe_float(_sortino_from_returns(returns, self._periods_per_year))

    # ----------------------------------------------------------- internals: async bridge

    def _run_async(self, coro: Any) -> Any:
        """Run a coroutine to completion in a private event loop.

        The BacktestEngine is a *sync* orchestrator (it matches the
        CLI smoke pattern). The journal is async. We bridge by
        running a one-shot event loop. The journal itself creates
        and tears down its own asyncio.Lock() lazily, so a fresh
        loop is fine.
        """

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return asyncio.run(coro)
        except RuntimeError:
            return asyncio.run(coro)
        return loop.run_until_complete(coro)


# ----------------------------------------------------------------- helpers


def _to_breakdown_entry(raw: dict[str, Any]) -> BreakdownEntry:
    """Convert a raw ``compute_*_stats`` dict to a typed :class:`BreakdownEntry`."""

    return BreakdownEntry(
        count=int(raw.get("count", 0)),
        closed=int(raw.get("closed", 0)),
        wins=int(raw.get("wins", 0)),
        losses=int(raw.get("losses", 0)),
        breakeven=int(raw.get("breakeven", 0)),
        winrate=float(raw.get("winrate", 0.0)),
        avg_r=float(raw.get("avg_r", 0.0)),
        total_r=float(raw.get("total_r", 0.0)),
        total_pnl=float(raw.get("total_pnl", 0.0)),
    )


def _sample_equity_curve(
    equity_curve: list[tuple[datetime, Decimal]], max_points: int = 20
) -> list[tuple[datetime, Decimal]]:
    """Evenly sample at most ``max_points`` points from the equity curve."""

    if not equity_curve or max_points <= 0:
        return []
    if len(equity_curve) <= max_points:
        return list(equity_curve)
    step = max(1, len(equity_curve) // max_points)
    sampled = equity_curve[::step]
    if len(sampled) > max_points:
        sampled = sampled[:max_points]
    return sampled


# ----------------------------------------------------------------- re-exports


__all__ = [
    "BacktestEngine",
    "BreakdownEntry",
    "JournalSink",
]
