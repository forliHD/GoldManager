"""Execution pipeline factory — turn a qualified decision into an order.

Extracted from the ``journal_smoke`` lifecycle so the execution-engine
*service* reuses the proven risk → stops → TP → size → order chain
instead of duplicating it. Given a qualified :class:`TradeQualification`
and a reference entry price, :meth:`ExecutionPipeline.process` runs the
full pre-trade gauntlet and submits the order, returning the resulting
:class:`TradeRecord` / :class:`OrderRecord` for the caller to publish on
the journal stream.

Scope boundary (documented, intentional)
----------------------------------------
This pipeline handles **trade entry**. Managing an *open* position over
subsequent bars — trailing the stop, taking partial profit at TP1/TP2,
emergency-flattening on the daily/weekly loss cap — is a stateful,
market-data-driven loop that belongs to a later increment of the
execution-engine (it needs a position store + a market_ticks
subscription). The building blocks (StopManager, TakeProfitManager,
EmergencyStopManager) are wired here and ready; the per-bar management
loop is not yet driven. See AGENTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    Decision,
    DecisionAction,
    EntryType,
    Score,
    TradeQualification,
)
from xauusd_bot.common.schemas.execution import OrderTag
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.common.schemas.journal import (
    OrderRecord,
    OrderStatusTag,
    TradeRecord,
)
from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.safety import PreTradeSafetyChecker, SafetyThresholds
from xauusd_bot.connectors.schemas import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
)
from xauusd_bot.execution import (
    EmergencyStopManager,
    OrderManager,
    PositionSizer,
    RiskManager,
    StopManager,
    TakeProfitManager,
)
from xauusd_bot.execution.position_manager import ManagedPosition
from xauusd_bot.execution.take_profit import DEFAULT_TP1_PCT, DEFAULT_TP2_PCT

log = structlog.get_logger(__name__)


@dataclass
class ExecutionOutcome:
    """Result of processing one qualified decision."""

    submitted: bool
    blocked_reason: str | None = None
    trade: TradeRecord | None = None
    order: OrderRecord | None = None
    # Per-position management plan (TP1/2/3 + trailing state) the
    # execution-engine persists and drives forward on subsequent bars.
    managed: ManagedPosition | None = None


class ExecutionPipeline:
    """Risk + sizing + order submission for one connector."""

    def __init__(self, settings: Settings, connector: IMarketConnector) -> None:
        self.settings = settings
        self.connector = connector
        self.symbol = settings.symbol
        self.spec = connector.get_symbol_spec(self.symbol)

        def _spread_points() -> float:
            acc = connector.get_account()
            return float(acc.current_spread) if acc.current_spread is not None else 30.0

        self.safety = PreTradeSafetyChecker(
            get_account=connector.get_account,
            get_spread_points=_spread_points,
            thresholds=SafetyThresholds(),
            is_connected=connector.is_connected,
        )
        self.order_mgr = OrderManager(connector=connector, safety=self.safety)
        self.sizer = PositionSizer()
        self.stop_mgr = StopManager(spec=self.spec)
        self.tp_mgr = TakeProfitManager(spec=self.spec)
        self.emergency = EmergencyStopManager(
            settings=settings,
            connector_positions=lambda: connector.positions_get(self.symbol),
            connector_pending=lambda: connector.pending_get(self.symbol),
            flatten_position=self._flatten_position,
            cancel_order=lambda oid: connector.order_cancel(oid),
        )
        self.risk_mgr = RiskManager(
            settings=settings,
            get_account=connector.get_account,
            get_positions=lambda: connector.positions_get(self.symbol),
            emergency=self.emergency,
        )

    def _flatten_position(self, position_id: str) -> OrderResult:
        # Live flattening (submit the closing order) is part of the
        # not-yet-driven position-management loop; for now we log and
        # report acceptance so the emergency manager's bookkeeping is
        # consistent. See the module docstring scope boundary.
        log.warning("execution_flatten_not_driven", position_id=position_id)
        return OrderResult(accepted=True, order_id=position_id)

    def process(
        self,
        decision: Decision,
        score: Score,
        qualification: TradeQualification,
        bundle: FeatureSnapshotBundle,
        *,
        ref_price: Decimal,
        now: datetime,
    ) -> ExecutionOutcome:
        """Run the entry gauntlet for one qualified decision."""

        if not qualification.qualified:
            return ExecutionOutcome(submitted=False, blocked_reason="not_qualified")

        side = (
            OrderSide.BUY
            if qualification.final_action == DecisionAction.ENTER_LONG
            else OrderSide.SELL
        )
        entry_price = Decimal(ref_price)

        # --- Risk approval (daily/weekly caps, open-position + per-session limits).
        risk_verdict = self.risk_mgr.approve(qualification, now=now)
        if not risk_verdict.approved:
            return ExecutionOutcome(submitted=False, blocked_reason=risk_verdict.blocked_reason)

        # --- Stops + take-profits (the "hands" compute SL/TP, never the LLM).
        stops = self.stop_mgr.compute_initial(side, entry_price, bundle, now=now)
        if stops.sl_price is None or stops.sl_price == 0:
            return ExecutionOutcome(submitted=False, blocked_reason="no_stop_loss")
        tp_plan = self.tp_mgr.compute(side, entry_price, stops.sl_price, bundle, now=now)
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
            return ExecutionOutcome(submitted=False, blocked_reason="zero_sl_distance")

        # --- Position size from the approved risk amount.
        sizing = self.sizer.size(
            risk_amount=risk_verdict.risk_amount,
            sl_distance=sl_distance,
            spec=self.spec,
            now=now,
        )
        if sizing.volume_lots <= 0:
            return ExecutionOutcome(submitted=False, blocked_reason="zero_volume")

        # --- Submit the order.
        order_env = self.order_mgr.send(
            OrderRequest(
                symbol=self.symbol,
                side=side,
                type=OrderType.MARKET,
                volume=sizing.volume_lots,
                sl=stops.sl_price,
                tp=stops.tp1_price,
            ),
            setup_id=qualification.qualification_id,
            now=now,
        )
        if order_env.state == "rejected":
            return ExecutionOutcome(
                submitted=False, blocked_reason=f"order_rejected:{order_env.error_code}"
            )

        self.risk_mgr.record_trade(now=now)
        fill_price = order_env.avg_fill_price or entry_price

        trade = TradeRecord(
            timestamp_open=now,
            side=("long" if side == OrderSide.BUY else "short"),
            entry_price=fill_price,
            stop_loss=stops.sl_price,
            take_profits=[
                p for p in (stops.tp1_price, stops.tp2_price, stops.tp3_price) if p is not None
            ],
            volume_lots=sizing.volume_lots,
            risk_amount=risk_verdict.risk_amount,
            setup_id=qualification.qualification_id,
            strategy_version="service-v1",
            engine_source="rule" if order_env.engine_source == OrderTag.RULE_BASED else "ai",
            score=score.total_score,
            subscores=dict(score.subscores),
            band=score.band,
            entry_type=qualification.final_entry_type or EntryType.SCOUT,
            order_ids=[order_env.order_id] if order_env.order_id else [],
            fill_price=fill_price,
            slippage_pips=(
                float(order_env.slippage_points) / 10.0
                if order_env.slippage_points is not None
                else None
            ),
            session=bundle.session.current_session.value if bundle.session else None,
            atr_at_entry=bundle.atr,
            structure_at_entry=(bundle.structure.trend if bundle.structure else "range"),
            tags={"source": "execution-engine"},
        )

        order = OrderRecord(
            timestamp=now,
            trade_id=trade.id,
            client_order_id=order_env.client_order_id,
            symbol=self.symbol,
            side=side,
            type=order_env.type,
            volume=sizing.volume_lots,
            requested_price=order_env.requested_price,
            fill_price=order_env.avg_fill_price,
            slippage_pips=trade.slippage_pips,
            status=(
                OrderStatusTag.FILLED
                if order_env.state == "filled"
                else OrderStatusTag.PENDING
                if order_env.state == "submitted"
                else OrderStatusTag.REJECTED
            ),
            error=(order_env.error_message if order_env.state == "rejected" else None),
            strategy_version=order_env.strategy_version,
        )
        log.info(
            "execution_order_submitted",
            side=side.value,
            volume=str(sizing.volume_lots),
            entry=str(fill_price),
            sl=str(stops.sl_price),
        )
        managed = None
        if order_env.order_id:
            managed = ManagedPosition(
                ticket=str(order_env.order_id),
                side=side,
                entry_price=fill_price,
                initial_volume=sizing.volume_lots,
                sl_price=stops.sl_price,
                tp1_price=stops.tp1_price,
                tp2_price=stops.tp2_price,
                tp3_price=stops.tp3_price,
                tp1_pct=DEFAULT_TP1_PCT,
                tp2_pct=DEFAULT_TP2_PCT,
            )
        return ExecutionOutcome(submitted=True, trade=trade, order=order, managed=managed)


__all__ = ["ExecutionOutcome", "ExecutionPipeline"]
