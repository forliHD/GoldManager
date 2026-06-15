"""Journal smoke CLI — Block 5a end-to-end proof-of-life.

Drives a deterministic mini-lifecycle through the full pipeline
(Replay → Features → Decision → Qualification → Risk → Sizing →
Stops → Order → PaperBroker → JournalStore → KPI Aggregations).

The CLI persists a :class:`TradeRecord` for every qualified trade
it finds, a :class:`FeatureSnapshotRecord` for every bar the decision
stack processes, and a :class:`OrderRecord` for every order it
submits. After the loop it runs the pure-function aggregations
(:func:`compute_equity_curve`, :func:`compute_r_distribution`, etc.)
over the in-memory journal and writes the result to
``logs/journal_snapshot.json``.

This is the foundation for Block 5b (BacktestEngine) and Block 5c
(ReviewAgent). The smoke must:

1. exit 0 on success
2. write a plausible ``journal_snapshot.json``
3. honor invariants I-1 (no MetaTrader5 import), I-4 (no derived
   field re-computation), and PIT (snapshot timestamps
   monotonically increasing, trades stamped at decision time).

Run from the repo root::

    python -m xauusd_bot.cli.journal_smoke

Or with custom parameters::

    python -m xauusd_bot.cli.journal_smoke --n-bars 200 --start-bar 2000
        --report logs/journal_snapshot.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

# Make ``xauusd_bot`` importable when the CLI is run without install.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.config import Settings  # noqa: E402
from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.schemas.decision import (  # noqa: E402
    Decision,
    DecisionAction,
    EntryType,
    Score,
    ScoreBand,
    TradeQualification,
)
from xauusd_bot.common.schemas.execution import (  # noqa: E402
    OrderTag,
    StopsAndTPs,
)
from xauusd_bot.common.schemas.features import (  # noqa: E402
    FeatureSnapshotBundle,
    LiquidityEngineOutput,
    LiquidityZone,
)
from xauusd_bot.common.schemas.journal import (  # noqa: E402
    ExitReasonTag,
    FeatureSnapshotRecord,
    OrderRecord,
    OrderStatusTag,
    TradeRecord,
)
from xauusd_bot.connectors.paper_broker import PaperBroker  # noqa: E402
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402
from xauusd_bot.connectors.safety import (  # noqa: E402
    PreTradeSafetyChecker,
    SafetyThresholds,
)
from xauusd_bot.connectors.schemas import (  # noqa: E402
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
)
from xauusd_bot.decision import (  # noqa: E402
    FeatureAggregator,
    RuleBasedFallback,
    ScoringEngine,
    TradeQualificationEngine,
)
from xauusd_bot.execution import (  # noqa: E402
    EmergencyStopManager,
    OrderManager,
    PendingOrderManager,
    PositionSizer,
    RiskManager,
    StopManager,
    TakeProfitManager,
)
from xauusd_bot.features._indicators import atr as compute_atr  # noqa: E402
from xauusd_bot.features._indicators import bars_to_df  # noqa: E402
from xauusd_bot.features.fvg import FVGEngine  # noqa: E402
from xauusd_bot.features.liquidity import LiquidityEngine  # noqa: E402
from xauusd_bot.features.momentum import CandleMomentumEngine  # noqa: E402
from xauusd_bot.features.news import NewsContextEngine, StubNewsProvider  # noqa: E402
from xauusd_bot.features.session import SessionEngine  # noqa: E402
from xauusd_bot.features.structure import MarketStructureEngine  # noqa: E402
from xauusd_bot.features.volume_range import FixedVolumeRangeEngine  # noqa: E402
from xauusd_bot.features.vwap import TripleVWAPEngine  # noqa: E402
from xauusd_bot.journal import (  # noqa: E402
    InMemoryJournalStore,
    compute_equity_curve,
    compute_max_drawdown,
    compute_r_distribution,
    compute_score_band_stats,
    compute_session_stats,
    compute_setup_breakdown,
    compute_sharpe,
    get_journal_store,
)

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = _THIS.parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = _THIS.parents[3] / "logs" / "journal_snapshot.json"


# ----------------------------------------------------------------- helpers


def _build_bundle_for_qualification(
    bars: list, ts: datetime, close: float
) -> FeatureSnapshotBundle:
    """Build a feature bundle whose liquidity has a TP target above the current close.

    Same trick as the execution_smoke CLI: we run the real engines
    for session/vwap/volume_range/fvg/structure/momentum/news but
    hand-craft a single liquidity zone above the current close so
    the TradeQualificationEngine's TP-proximity check passes. This
    is the only way the synthetic sample (which is purely a price
    series) yields a non-zero qualified-trade rate in the smoke.
    """

    session_eng = SessionEngine()
    vwap_eng = TripleVWAPEngine()
    vr_eng = FixedVolumeRangeEngine()
    fvg_eng = FVGEngine()
    structure_eng = MarketStructureEngine()
    momentum_eng = CandleMomentumEngine()
    news_eng = NewsContextEngine(provider=StubNewsProvider())

    session_out = session_eng.compute(bars, ts)
    vwap_out = vwap_eng.compute(bars, ts)
    vr_out = vr_eng.compute(bars, ts)
    fvg_out = fvg_eng.compute(bars, ts)
    structure_out = structure_eng.compute(bars, ts)
    momentum_out = momentum_eng.compute(bars, ts)
    news_out = news_eng.compute(ts)
    atr_val = compute_atr(bars_to_df(bars), period=14)
    atr_safe = atr_val if atr_val and atr_val > 0 else 0.5

    zone = LiquidityZone(
        kind="high",
        price_low=close + atr_safe * 0.5,
        price_high=close + atr_safe * 1.0,
        center=close + atr_safe * 0.75,
        pool_count=1,
        is_sl_trap=False,
    )
    liq_out = LiquidityEngineOutput(
        tp_targets_above=[zone], tp_targets_below=[], sl_protection_zones=[]
    )

    return FeatureSnapshotBundle(
        ts=ts,
        session=session_out,
        vwap=vwap_out,
        volume_range=vr_out,
        fvg=fvg_out,
        structure=structure_out,
        momentum=momentum_out,
        liquidity=liq_out,
        news=news_out,
        atr=atr_val,
    )


def _snapshot_to_dict(bundle: FeatureSnapshotBundle, snapshot_id) -> dict[str, Any]:
    """Flatten a :class:`FeatureSnapshotBundle` into a JSON-safe dict for the journal.

    We pull a *compact* view (not the full nested structure) because
    the journal's ``features`` field is meant to be
    JSON-serializable and not too large. The full bundle is
    reconstructable from the persisted :class:`TradeRecord` and
    Block-2 engine code if needed.
    """

    session_name = bundle.session.current_session.value if bundle.session else None
    structure_trend = bundle.structure.trend if bundle.structure else None
    in_blackout = bundle.news.in_blackout_flag if bundle.news else None
    vwap_center = bundle.vwap.cluster_center if bundle.vwap else None
    return {
        "ts": bundle.ts.isoformat() if bundle.ts else None,
        "session": session_name,
        "atr": bundle.atr,
        "structure_trend": structure_trend,
        "in_blackout": in_blackout,
        "vwap_cluster_center": vwap_center,
        "snapshot_id": str(snapshot_id),
    }


def _simulate_close(
    trade: TradeRecord, future_bars: list, current_t: datetime
) -> tuple[datetime, Decimal, Decimal, Decimal, ExitReasonTag] | None:
    """Walk a few future bars and synthesize a close for the smoke.

    The smoke is not a real backtest — it just needs to give every
    open trade a close so the equity curve has data. We pick the
    first bar that touches the SL or the first TP, with a
    deterministic 5-bar timeout fallback to a "manual" close at
    the last bar's close.

    Returns ``(close_ts, exit_price, pnl, r_multiple, exit_reason)`` or
    None if ``future_bars`` is empty.
    """

    if not future_bars:
        return None

    side_sign = Decimal("1") if trade.side == "long" else Decimal("-1")
    sl = trade.stop_loss
    tps = trade.take_profits or []
    tp1 = tps[0] if tps else None
    for j, bar in enumerate(future_bars):
        # SL hit?
        if side_sign > 0 and bar.low <= sl:
            exit_price = sl
            gross = (exit_price - trade.entry_price) * side_sign
            pnl = gross * trade.volume_lots * Decimal("100")  # XAUUSD: 100 oz/lot
            risk = trade.risk_amount if trade.risk_amount > 0 else Decimal("1")
            r = float(pnl / risk)
            return (bar.time, exit_price, pnl, Decimal(str(r)), ExitReasonTag.SL_HIT)
        if side_sign < 0 and bar.high >= sl:
            exit_price = sl
            gross = (exit_price - trade.entry_price) * side_sign
            pnl = gross * trade.volume_lots * Decimal("100")
            risk = trade.risk_amount if trade.risk_amount > 0 else Decimal("1")
            r = float(pnl / risk)
            return (bar.time, exit_price, pnl, Decimal(str(r)), ExitReasonTag.SL_HIT)
        # TP1 hit?
        if tp1 is not None:
            if side_sign > 0 and bar.high >= tp1:
                exit_price = tp1
                gross = (exit_price - trade.entry_price) * side_sign
                pnl = gross * trade.volume_lots * Decimal("100")
                risk = trade.risk_amount if trade.risk_amount > 0 else Decimal("1")
                r = float(pnl / risk)
                return (bar.time, exit_price, pnl, Decimal(str(r)), ExitReasonTag.TP1_HIT)
            if side_sign < 0 and bar.low <= tp1:
                exit_price = tp1
                gross = (exit_price - trade.entry_price) * side_sign
                pnl = gross * trade.volume_lots * Decimal("100")
                risk = trade.risk_amount if trade.risk_amount > 0 else Decimal("1")
                r = float(pnl / risk)
                return (bar.time, exit_price, pnl, Decimal(str(r)), ExitReasonTag.TP1_HIT)
        # Timeout fallback after 5 bars.
        if j >= 5:
            exit_price = bar.close
            gross = (exit_price - trade.entry_price) * side_sign
            pnl = gross * trade.volume_lots * Decimal("100")
            risk = trade.risk_amount if trade.risk_amount > 0 else Decimal("1")
            r = float(pnl / risk)
            return (bar.time, exit_price, pnl, Decimal(str(r)), ExitReasonTag.MANUAL)
    return None


# ----------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Journal smoke for Block 5a.")
    parser.add_argument("--n-bars", type=int, default=200)
    parser.add_argument("--start-bar", type=int, default=2000)
    parser.add_argument("--symbol", type=str, default="XAUUSD")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--close-window",
        type=int,
        default=10,
        help=(
            "How many bars to walk after a qualified trade to find a SL/TP/timeout close. "
            "Smoke-only; the real BacktestEngine handles this properly in Block 5b."
        ),
    )
    return parser.parse_args(argv)


# ----------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(level="INFO")

    if not args.sample.exists():
        log.error("sample_missing", path=str(args.sample))
        print(f"ERROR: sample dataset not found at {args.sample}.", file=sys.stderr)
        print("Run: python -m tools.generate_sample_data", file=sys.stderr)
        return 2

    log.info("journal_smoke_starting", sample=str(args.sample), n_bars=args.n_bars)
    started = time.perf_counter()

    settings = Settings()
    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)
    spec = connector.spec
    paper = PaperBroker(connector=connector, initial_balance=Decimal("10000"))

    # Engine stack.
    aggregator = FeatureAggregator()
    scoring = ScoringEngine()
    fallback = RuleBasedFallback(settings=settings)
    qualifier = TradeQualificationEngine(settings=settings)

    # Execution layer.
    safety = PreTradeSafetyChecker(
        get_account=lambda: connector.get_account(),
        get_spread_points=lambda: 30.0,
        thresholds=SafetyThresholds(),
        is_connected=lambda: connector.is_connected(),
    )
    order_mgr = OrderManager(connector=connector, safety=safety)
    pending_mgr = PendingOrderManager(connector=connector)
    sizer = PositionSizer()
    stop_mgr = StopManager(spec=spec)
    tp_mgr = TakeProfitManager(spec=spec)
    emergency = EmergencyStopManager(
        settings=settings,
        connector_positions=lambda: paper.open_positions,
        connector_pending=lambda: [],
        flatten_position=lambda pid: OrderResult(accepted=True, order_id=pid),
        cancel_order=lambda oid: OrderResult(accepted=True, order_id=oid),
        state_file=args.report.parent / "emergency_stop_state.json",
    )
    risk_mgr = RiskManager(
        settings=settings,
        get_account=lambda: connector.get_account(),
        get_positions=lambda: paper.open_positions,
        emergency=emergency,
    )

    # Journal. The factory picks the backend; the smoke always
    # inlines InMemoryJournalStore in test env, which is exactly
    # what we want for the smoke.
    store = get_journal_store(settings)
    if not isinstance(store, InMemoryJournalStore):
        # If a Timescale store is configured (non-test env), fall
        # back explicitly to in-memory so the smoke never blocks on
        # an unavailable DB.
        log.warning("journal_smoke_forcing_in_memory_for_determinism")
        store = InMemoryJournalStore()
    assert isinstance(store, InMemoryJournalStore)

    # Walk bars.
    start_bar = max(0, args.start_bar)
    n_target = min(args.n_bars, len(connector.bars) - start_bar)
    if n_target <= 0:
        log.error("not_enough_bars", have=len(connector.bars), requested=n_target)
        return 2

    all_bars: list = []
    for j in range(start_bar + n_target):
        all_bars.append(connector._row_to_bar(connector.bars.iloc[j], "M1"))  # noqa: SLF001

    n_snapshots_written = 0
    n_trades_written = 0
    n_orders_written = 0
    last_snapshot_id = None  # type: ignore[var-annotated]

    # We pre-write a snapshot for every bar the decision stack
    # touches. This is the *full* feature-snapshot stream — not
    # just the qualified ones. Block 5c Review needs the full
    # stream to compute "engine disagreement" over time.
    for k in range(n_target):
        i = start_bar + k
        bar = all_bars[i]
        bars_so_far = all_bars[: i + 1]
        current_t = bar.time
        bundle = _build_bundle_for_qualification(bars_so_far, current_t, float(bar.close))
        agg = aggregator.aggregate(bundle)
        score = scoring.score(agg)
        account = connector.get_account()
        decision = fallback.decide(score, agg, account=account)
        qualification = qualifier.qualify(decision, score, agg, bundle, account=account)

        # Snapshot — write every bar (qualified or not). The
        # journal does NOT re-derive this from later bars.
        snapshot = FeatureSnapshotRecord(
            timestamp=current_t,
            bar_time=current_t,
            symbol=args.symbol,
            timeframe="m1",
            has_data=agg.has_data,
            features=_snapshot_to_dict(bundle, uuid4()),
            source_version="block2-v1",
            engine_name=None,
        )
        last_snapshot_id = snapshot.id
        _await(store.write_feature_snapshot(snapshot))
        n_snapshots_written += 1

        if not qualification.qualified:
            continue

        # We have a qualified trade. Drive the lifecycle.
        trade_qual = qualification
        trade_decision = decision
        trade_score = score
        trade_bundle = bundle
        trade_i = i
        side = OrderSide.BUY if trade_qual.final_action == DecisionAction.ENTER_LONG else OrderSide.SELL
        entry_price = bar.close

        # Risk + stops + size.
        risk_verdict = risk_mgr.approve(trade_qual, now=bar.time)
        if not risk_verdict.approved:
            log.info("journal_smoke_risk_blocked", reason=risk_verdict.blocked_reason)
            continue
        stops = stop_mgr.compute_initial(side, entry_price, trade_bundle, now=bar.time)
        if stops.sl_price is None or stops.sl_price == 0:
            log.info("journal_smoke_no_sl_skip")
            continue
        tp_plan = tp_mgr.compute(
            side, entry_price, stops.sl_price, trade_bundle, now=bar.time
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
        sl_distance = abs(entry_price - stops.sl_price)
        if sl_distance <= 0:
            continue
        sizing = sizer.size(
            risk_amount=risk_verdict.risk_amount,
            sl_distance=sl_distance,
            spec=spec,
            now=bar.time,
        )
        if sizing.volume_lots <= 0:
            continue

        # Order.
        order_env = order_mgr.send(
            OrderRequest(
                symbol=args.symbol,
                side=side,
                type=OrderType.MARKET,
                volume=sizing.volume_lots,
                sl=stops.sl_price,
                tp=stops.tp1_price,
            ),
            setup_id=trade_qual.qualification_id,
            now=bar.time,
        )
        if order_env.state == "rejected":
            log.info("journal_smoke_order_rejected", reason=order_env.error_code)
            continue

        # Persist the open trade.
        fill_price = order_env.avg_fill_price or entry_price
        trade_record = TradeRecord(
            timestamp_open=bar.time,
            side=("long" if side == OrderSide.BUY else "short"),
            entry_price=fill_price,
            stop_loss=stops.sl_price,
            take_profits=[p for p in (stops.tp1_price, stops.tp2_price, stops.tp3_price) if p is not None],
            volume_lots=sizing.volume_lots,
            risk_amount=risk_verdict.risk_amount,
            setup_id=trade_qual.qualification_id,
            strategy_version="block5a-v1",
            engine_source="rule" if order_env.engine_source == OrderTag.RULE_BASED else "ai",
            score=trade_score.total_score,
            subscores=dict(trade_score.subscores),
            band=trade_score.band,
            entry_type=trade_qual.final_entry_type or EntryType.SCOUT,
            feature_snapshot_id=last_snapshot_id,
            order_ids=[order_env.order_id] if order_env.order_id else [],
            fill_price=fill_price,
            slippage_pips=(
                float(order_env.slippage_points) / 10.0
                if order_env.slippage_points is not None
                else None
            ),
            slippage_bps=(
                float((order_env.avg_fill_price - order_env.requested_price) / order_env.requested_price * 10000)
                if order_env.slippage_points is not None
                and order_env.requested_price
                and order_env.avg_fill_price
                else None
            ),
            session=trade_bundle.session.current_session.value,
            atr_at_entry=trade_bundle.atr,
            structure_at_entry=trade_bundle.structure.trend if trade_bundle.structure else "range",
            tags={"source": "journal_smoke"},
        )
        _await(store.write_trade(trade_record))
        n_trades_written += 1

        # Order record.
        order_record = OrderRecord(
            timestamp=bar.time,
            trade_id=trade_record.id,
            client_order_id=order_env.client_order_id,
            symbol=args.symbol,
            side=side,
            type=order_env.type,
            volume=sizing.volume_lots,
            requested_price=order_env.requested_price,
            fill_price=order_env.avg_fill_price,
            slippage_pips=(
                float(order_env.slippage_points) / 10.0
                if order_env.slippage_points is not None
                else None
            ),
            slippage_bps=trade_record.slippage_bps,
            status=(
                OrderStatusTag.FILLED
                if order_env.state == "filled"
                else OrderStatusTag.PENDING
                if order_env.state == "submitted"
                else OrderStatusTag.REJECTED
            ),
            error=(
                order_env.error_message
                if order_env.state == "rejected"
                else None
            ),
            strategy_version=order_env.strategy_version,
        )
        _await(store.write_order(order_record))
        n_orders_written += 1

        risk_mgr.record_trade(now=bar.time)

        # Synthesize a close (smoke-only) within the close window.
        future = all_bars[trade_i + 1 : trade_i + 1 + args.close_window]
        close_info = _simulate_close(trade_record, future, bar.time)
        if close_info is not None:
            ts_close, exit_price, pnl, r_mult, exit_reason = close_info
            _await(
                store.update_trade(
                    trade_record.id,
                    updates={
                        "timestamp_close": ts_close,
                        "exit_price": exit_price,
                        "pnl_realized": pnl,
                        "r_multiple": float(r_mult),
                        "exit_reason": exit_reason,
                    },
                )
            )

    # ----------------------------------------------------------------- KPI aggregation

    all_trades = _await(store.list_trades())
    closed_trades = [t for t in all_trades if t.pnl_realized is not None]
    equity_curve = compute_equity_curve(all_trades)
    r_distribution = compute_r_distribution(all_trades)
    setup_breakdown = compute_setup_breakdown(all_trades)
    session_stats = compute_session_stats(all_trades)
    score_band_stats = compute_score_band_stats(all_trades)
    max_dd_amount, max_dd_peak, max_dd_trough = compute_max_drawdown(equity_curve)
    sharpe = compute_sharpe(equity_curve)

    # Sample 20 points from the equity curve for the JSON (file-size guard).
    if len(equity_curve) <= 20:
        ec_sample = [(t.isoformat(), str(eq)) for t, eq in equity_curve]
    else:
        step = max(1, len(equity_curve) // 20)
        ec_sample = [
            (t.isoformat(), str(eq))
            for idx, (t, eq) in enumerate(equity_curve)
            if idx % step == 0
        ][:20]

    counts = _await(store.count())
    elapsed = round(time.perf_counter() - started, 3)

    report = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": str(args.sample),
        "n_bars_consumed": n_target,
        "start_bar": start_bar,
        "elapsed_seconds": elapsed,
        "report_path": str(args.report),
        "store_kind": "in_memory",
        "counts": counts,
        "n_snapshots_written": n_snapshots_written,
        "n_trades_written": n_trades_written,
        "n_orders_written": n_orders_written,
        "n_trades_closed": len(closed_trades),
        "qualified_count": n_trades_written,
        "equity_curve_sample": ec_sample,
        "r_distribution": r_distribution,
        "setup_breakdown": setup_breakdown,
        "session_stats": session_stats,
        "score_band_stats": score_band_stats,
        "max_drawdown": {
            "amount": str(max_dd_amount),
            "peak_time": max_dd_peak.isoformat() if max_dd_peak else None,
            "trough_time": max_dd_trough.isoformat() if max_dd_trough else None,
        },
        "sharpe": round(sharpe, 4) if math.isfinite(sharpe) else 0.0,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str))
    log.info(
        "journal_smoke_complete",
        n_bars=n_target,
        n_trades=n_trades_written,
        n_snapshots=n_snapshots_written,
        n_orders=n_orders_written,
        elapsed=elapsed,
    )
    print(
        json.dumps(
            {
                "n_bars_consumed": n_target,
                "n_trades_written": n_trades_written,
                "n_snapshots_written": n_snapshots_written,
                "n_orders_written": n_orders_written,
                "n_trades_closed": len(closed_trades),
                "qualified_count": n_trades_written,
                "sharpe": report["sharpe"],
                "max_drawdown": report["max_drawdown"]["amount"],
                "report_path": str(args.report),
            },
            indent=2,
        )
    )
    return 0


# ----------------------------------------------------------------- sync bridge for async store


def _await(coro: Any) -> Any:
    """Run a coroutine to completion in a private event loop.

    The smoke is a *sync* CLI (matches decision_smoke / execution_smoke
    style). The store is async. We bridge by running a one-shot
    event loop. The store itself creates and tears down its own
    asyncio.Lock() lazily, so a fresh loop is fine for the smoke.
    """

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Should not happen in the smoke, but defensively:
            return asyncio.run(coro)
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


if __name__ == "__main__":
    raise SystemExit(main())
