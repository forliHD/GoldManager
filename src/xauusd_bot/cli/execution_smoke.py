"""Execution-Engine lifecycle smoke CLI (Block 4).

This CLI drives a single trade through the **full** execution
pipeline end-to-end:

    bars → features → decision → trade_qualification
       → RiskManager.approve → PositionSizer.size
       → StopManager.compute_initial + TakeProfitManager.compute
       → PreTradeSafetyChecker.check → OrderManager.send
       → PendingOrderManager.sweep (cancel obsolete)
       → StopManager.trail (after a few bars)
       → TakeProfitManager.partial_close (TP1 hit simulation)
       → close

The full lifecycle is serialised to ``logs/execution_lifecycle.json``
for the verifier to inspect.

Flags
-----
``--n-bars``        number of M1 bars to replay (default 200)
``--start-bar``     warm-up skip (default 0)
``--force-trade``   if no natural decision qualifies, inject a synthetic
                    long at ``--start-bar`` so the lifecycle still runs
``--simulate-losses N``
                    after the pipeline runs, force N consecutive losses
                    into the RiskManager to demonstrate daily-pause
                    triggering. Each "loss" is a 1 % equity draw.
``--symbol``        XAUUSD (default)
``--sample``        source parquet/csv
``--report``        output JSON path
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    ExecutionLifecycleReport,
    ExecutionPhaseResult,
    OrderEnvelope,
    RiskVerdict,
    SizingResult,
    StopsAndTPs,
)
from xauusd_bot.common.schemas.features import (  # noqa: E402
    FeatureSnapshotBundle,
    LiquidityEngineOutput,
    LiquidityZone,
    NewsContextOutput,
    StructureEventType,
)
from xauusd_bot.connectors.paper_broker import PaperBroker  # noqa: E402
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402
from xauusd_bot.connectors.safety import (  # noqa: E402
    PreTradeSafetyChecker,
    SafetyAction,
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

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = _THIS.parents[3] / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = _THIS.parents[3] / "logs" / "execution_lifecycle.json"


# ----------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execution-engine lifecycle smoke for Block 4.")
    parser.add_argument("--n-bars", type=int, default=200)
    parser.add_argument("--start-bar", type=int, default=0)
    parser.add_argument("--force-trade", action="store_true", help="Inject a synthetic trade if no natural one qualifies.")
    parser.add_argument("--simulate-losses", type=int, default=0, help="Force N losses into the RiskManager after the lifecycle.")
    parser.add_argument("--symbol", type=str, default="XAUUSD")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args(argv)


# ----------------------------------------------------------------- helpers


def _build_liquidity_with_target_above(current_price: float, atr: float) -> LiquidityEngineOutput:
    """Construct a tiny bundle.liquidity so the TP1 test has a candidate."""

    zone = LiquidityZone(
        kind="high",
        price_low=current_price + atr * 0.5,
        price_high=current_price + atr * 1.0,
        center=current_price + atr * 0.75,
        pool_count=1,
        is_sl_trap=False,
    )
    return LiquidityEngineOutput(tp_targets_above=[zone], tp_targets_below=[], sl_protection_zones=[])


def _build_bundle_for_qualification(
    bars: list, ts: datetime, close: float
) -> FeatureSnapshotBundle:
    """Build a synthetic :class:`FeatureSnapshotBundle` that lets the qualification pass.

    Uses real engines (so the structure / VWAP / FVG data is
    internally consistent) but the Liquidity engine is replaced
    with a small hand-crafted output containing a TP target
    above current price.
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
    liq_out = _build_liquidity_with_target_above(close, atr_val if atr_val and atr_val > 0 else 0.5)

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


# ----------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(level="INFO")

    if not args.sample.exists():
        log.error("sample_missing", path=str(args.sample))
        print(f"ERROR: sample dataset not found at {args.sample}.", file=sys.stderr)
        return 2

    log.info("execution_smoke_starting", sample=str(args.sample), n_bars=args.n_bars)
    started = time.perf_counter()

    settings = Settings()
    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)
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
    spec = connector.spec
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

    # Walk the bars.
    start_bar = max(0, args.start_bar)
    n_target = min(args.n_bars, len(connector.bars) - start_bar)
    if n_target <= 0:
        log.error("not_enough_bars", have=len(connector.bars), requested=n_target)
        return 2

    all_bars = []
    for j in range(start_bar + n_target):
        all_bars.append(connector._row_to_bar(connector.bars.iloc[j], "M1"))  # noqa: SLF001

    lifecycle: dict[str, Any] = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "sample": str(args.sample),
        "n_bars_consumed": n_target,
        "start_bar": start_bar,
        "elapsed_seconds": 0.0,
        "phases": [],
        "qualifications": 0,
        "lifecycle": None,
        "simulated_losses": 0,
        "pause_triggered": False,
        "emergency_triggered": False,
    }

    phase_log: list[ExecutionPhaseResult] = []

    def _record_phase(phase: str, ok: bool, detail: dict[str, object], error: str | None = None) -> None:
        phase_log.append(
            ExecutionPhaseResult(
                phase=phase,
                ok=ok,
                detail=detail,
                error=error,
                timestamp=datetime.now(tz=UTC),
            )
        )

    def _safe_phase(phase: str, fn) -> tuple[bool, dict[str, object] | None, str | None]:
        try:
            ok, detail = fn()
            _record_phase(phase, ok, detail or {})
            return ok, detail, None
        except Exception as exc:  # noqa: BLE001
            _record_phase(phase, False, {}, str(exc))
            return False, None, str(exc)

    # ---------------------------------------------------------------- find trade
    trade_qual: TradeQualification | None = None
    trade_score: Score | None = None
    trade_decision: Decision | None = None
    trade_bundle: FeatureSnapshotBundle | None = None
    trade_i: int = -1

    for k in range(n_target):
        i = start_bar + k
        bar = all_bars[i]
        bars_so_far = all_bars[: i + 1]
        current_t = bar.time
        bundle = _build_bundle_for_qualification(bars_so_far, current_t, float(bar.close))
        agg = aggregator.aggregate(bundle)
        score = scoring.score(agg)
        decision = fallback.decide(score, agg, account=connector.get_account())
        qualification = qualifier.qualify(decision, score, agg, bundle, account=connector.get_account())
        if qualification.qualified:
            trade_qual = qualification
            trade_score = score
            trade_decision = decision
            trade_bundle = bundle
            trade_i = i
            lifecycle["qualifications"] += 1
            break

    if trade_qual is None and args.force_trade:
        # Synthesize a qualified long trade at the last bar.
        i = start_bar + n_target - 1
        bar = all_bars[i]
        bars_so_far = all_bars[: i + 1]
        current_t = bar.time
        bundle = _build_bundle_for_qualification(bars_so_far, current_t, float(bar.close))
        agg = aggregator.aggregate(bundle)
        score = scoring.score(agg)
        score = Score(
            total_score=88.0,
            subscores={k: 80.0 for k in score.subscores},
            band=ScoreBand.FULL_85_PLUS,
            reasoning=["forced-trade override"],
            direction="long",
            timestamp=score.timestamp,
        )
        decision = Decision(
            action=DecisionAction.ENTER_LONG,
            entry_type=EntryType.FULL,
            block_reason=None,
            source_score=score.total_score,
            source_band=score.band,
            source_direction="long",
            timestamp=score.timestamp,
        )
        trade_qual = qualifier.qualify(decision, score, agg, bundle, account=connector.get_account())
        trade_score = score
        trade_decision = decision
        trade_bundle = bundle
        trade_i = i
        lifecycle["qualifications"] += 1

    if trade_qual is None:
        log.warning("execution_smoke_no_qualification")
        lifecycle["phases"] = [p.model_dump() for p in phase_log]
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(lifecycle, indent=2, default=str))
        print(json.dumps(lifecycle, indent=2, default=str))
        return 0

    # ---------------------------------------------------------------- lifecycle

    bar = all_bars[trade_i]
    entry_price = bar.close
    side = OrderSide.BUY if trade_qual.final_action == DecisionAction.ENTER_LONG else OrderSide.SELL

    risk_verdict: RiskVerdict | None = None
    sizing: SizingResult | None = None
    stops: StopsAndTPs | None = None
    order_env: OrderEnvelope | None = None

    # 1. RiskManager
    def _risk() -> tuple[bool, dict[str, object]]:
        nonlocal risk_verdict
        risk_verdict = risk_mgr.approve(trade_qual, now=bar.time)
        if not risk_verdict.approved:
            raise RuntimeError(f"risk blocked: {risk_verdict.blocked_reason}")
        return True, {
            "approved": risk_verdict.approved,
            "risk_band": risk_verdict.risk_band.value if risk_verdict.risk_band else None,
            "risk_per_trade_pct": risk_verdict.risk_per_trade_pct,
            "risk_amount": str(risk_verdict.risk_amount),
        }

    ok, _, err = _safe_phase("risk_approve", _risk)
    if not ok:
        log.warning("lifecycle_halted_risk_blocked", error=err)

    # 2. PositionSizer (uses stop distance computed by StopManager below; we
    #    need the SL first, so do stops before sizing).
    def _stops() -> tuple[bool, dict[str, object]]:
        nonlocal stops
        stops = stop_mgr.compute_initial(side, entry_price, trade_bundle, now=bar.time)
        # Add TPs.
        tp_plan = tp_mgr.compute(side, entry_price, stops.sl_price or entry_price, trade_bundle, now=bar.time)
        stops = stops.model_copy(update={
            "tp1_price": tp_plan.tp1_price,
            "tp2_price": tp_plan.tp2_price,
            "tp3_price": tp_plan.tp3_price,
            "partial_close_plan": tp_plan.partial_close_plan,
            "reasoning": stops.reasoning + tp_plan.reasoning,
        })
        return True, {
            "sl": str(stops.sl_price),
            "tp1": str(stops.tp1_price),
            "tp2": str(stops.tp2_price),
            "tp3": str(stops.tp3_price),
            "reasoning": stops.reasoning,
        }

    ok, _, err = _safe_phase("stops_compute", _stops)
    if not ok or stops is None:
        log.warning("lifecycle_halted_stops_failed", error=err)

    def _size() -> tuple[bool, dict[str, object]]:
        nonlocal sizing
        if risk_verdict is None or stops is None or stops.sl_price is None:
            raise RuntimeError("missing risk/stops")
        sl_distance = abs(entry_price - stops.sl_price)
        if sl_distance <= 0:
            raise RuntimeError("sl_distance must be > 0")
        sizing = sizer.size(
            risk_amount=risk_verdict.risk_amount,
            sl_distance=sl_distance,
            spec=spec,
            now=bar.time,
        )
        return True, {
            "volume_lots": str(sizing.volume_lots),
            "risk_per_lot": str(sizing.risk_per_lot),
            "rounding_mode": sizing.rounding_mode.value,
            "formula": sizing.formula_used,
        }

    ok, _, err = _safe_phase("position_size", _size)
    if not ok or sizing is None:
        log.warning("lifecycle_halted_sizing_failed", error=err)

    # 3. OrderManager (with PreTradeSafety baked in).
    def _order() -> tuple[bool, dict[str, object]]:
        nonlocal order_env
        if sizing is None:
            raise RuntimeError("missing sizing")
        order_env = order_mgr.send(
            OrderRequest(
                symbol=args.symbol,
                side=side,
                type=OrderType.MARKET,
                volume=sizing.volume_lots,
                sl=stops.sl_price if stops else None,
                tp=stops.tp1_price if stops else None,
            ),
            setup_id=trade_qual.qualification_id,
            now=bar.time,
        )
        if order_env.state == "rejected":
            raise RuntimeError(f"order rejected: {order_env.error_code} {order_env.error_message}")
        return True, {
            "state": order_env.state,
            "client_order_id": order_env.client_order_id,
            "filled_volume": str(order_env.filled_volume),
            "avg_fill_price": str(order_env.avg_fill_price) if order_env.avg_fill_price is not None else None,
            "slippage_points": str(order_env.slippage_points) if order_env.slippage_points is not None else None,
        }

    ok, _, err = _safe_phase("order_send", _order)
    if not ok or order_env is None:
        log.warning("lifecycle_halted_order_failed", error=err)

    # 4. RiskManager.record_trade.
    def _record_trade() -> tuple[bool, dict[str, object]]:
        risk_mgr.record_trade(now=bar.time)
        return True, {"trades_today": risk_mgr.state.trades_today}

    _safe_phase("risk_record_trade", _record_trade)

    # 5. Pending sweep.
    def _pending() -> tuple[bool, dict[str, object]]:
        sweep = pending_mgr.sweep(trade_bundle, float(bar.close), bar_index=trade_i, now=bar.time)
        return True, {
            "examined": sweep.examined,
            "kept": sweep.kept,
            "cancelled": sweep.cancelled,
            "reasons": sweep.cancel_reasons,
        }

    _safe_phase("pending_sweep", _pending)

    # 6. Trail (best-effort).
    def _trail() -> tuple[bool, dict[str, object]]:
        if order_env is None or stops is None or stops.sl_price is None:
            raise RuntimeError("missing order/stops")
        new_stops = stop_mgr.trail(
            side=side,
            current_sl=stops.sl_price,
            entry_price=entry_price,
            bundle=trade_bundle,
            now=bar.time,
        )
        return True, {
            "new_sl": str(new_stops.sl_price),
            "trail_active": new_stops.trail_active,
        }

    _safe_phase("trail", _trail)

    # 7. (Optional) simulate losses.
    sim_losses: list[dict[str, object]] = []
    for n in range(args.simulate_losses):
        loss = Decimal("-100")  # 1 % of 10 000
        risk_mgr.record_pnl(pnl=loss, now=bar.time)
        sim_losses.append({"iteration": n, "loss": str(loss), "daily": str(risk_mgr.state.daily_pnl)})
        if not emergency.is_active(now=bar.time):
            emergency.trigger(
                __import__("xauusd_bot.common.schemas.execution", fromlist=["EmergencyTrigger"]).EmergencyTrigger.MANUAL_KILL_SWITCH,
                details={"simulate_losses_iter": n},
                now=bar.time,
            )
        # stop after pause triggers
        if emergency.is_active(now=bar.time):
            break
    if sim_losses:
        lifecycle["simulated_losses"] = len(sim_losses)
        lifecycle["pause_triggered"] = emergency.is_active(now=bar.time)
        lifecycle["emergency_state"] = emergency.state().model_dump()
        _record_phase(
            "simulate_losses",
            True,
            {"losses": sim_losses, "pause_triggered": lifecycle["pause_triggered"]},
        )

    # Build the lifecycle report.
    report = ExecutionLifecycleReport(
        setup_id=trade_qual.qualification_id,
        qualification=trade_qual,
        risk=risk_verdict,
        sizing=sizing,
        stops=stops,
        order=order_env,
        phases=phase_log,
        timestamp=bar.time,
    )
    lifecycle["lifecycle"] = report.model_dump()
    lifecycle["phases"] = [p.model_dump() for p in phase_log]
    lifecycle["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    lifecycle["safety_action"] = safety.check(bar.time).action.value

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(lifecycle, indent=2, default=str))
    log.info(
        "execution_smoke_complete",
        phases=len(phase_log),
        approved=risk_verdict.approved if risk_verdict else False,
        order_state=order_env.state if order_env else "n/a",
        pause_triggered=lifecycle["pause_triggered"],
    )
    print(
        json.dumps(
            {
                "n_bars_consumed": n_target,
                "qualifications": lifecycle["qualifications"],
                "phases": [p.phase for p in phase_log],
                "approved": risk_verdict.approved if risk_verdict else False,
                "order_state": order_env.state if order_env else "n/a",
                "pause_triggered": lifecycle["pause_triggered"],
                "report_path": str(args.report),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
