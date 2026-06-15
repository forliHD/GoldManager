"""Execution engine (Block 4) — the deterministic "Hands" layer.

Block 4 owns position sizing, stop / take-profit, order management,
and emergency control. It sits between the decision layer (Block 3,
``xauusd_bot.decision``) and the broker connector (``xauusd_bot.connectors``).

Pipeline
--------
1. :class:`~xauusd_bot.decision.qualification.TradeQualificationEngine`
   emits a qualified :class:`~xauusd_bot.common.schemas.decision.TradeQualification`.
2. :class:`RiskManager.approve` → :class:`RiskVerdict` (veto authority).
3. :class:`PositionSizer.size` → :class:`SizingResult` (lot volume).
4. :class:`StopManager.compute_initial` + :class:`TakeProfitManager.compute`
   → :class:`StopsAndTPs` (SL / TP1 / TP2 / TP3).
5. :class:`OrderManager.send` → :class:`OrderEnvelope` (idempotent order).
6. :class:`PendingOrderManager.sweep` periodically → cancel obsolete
   limit / stop orders.
7. :class:`EmergencyStopManager` runs on every iteration — flatten +
   cancel + pause on any spike / disconnect / kill-switch.

Hard constraints (enforced in code + verifier)
----------------------------------------------
* I-1 — no module in ``execution/`` imports ``MetaTrader5``. The
  only allowed import surface is :class:`~xauusd_bot.connectors.base.IMarketConnector`.
* I-4 (inverted for this layer) — the execution layer *does*
  compute position size, SL, TP. The decision layer never does.
* Pre-trade safety — every ``order_send`` is preceded by
  :meth:`PreTradeSafetyChecker.check`. A ``BLOCK`` vetoes the order.
* No LLM calls — everything is deterministic.
"""

from xauusd_bot.execution.emergency import (
    DEFAULT_PAUSE_DURATION,
    DEFAULT_SLIPPAGE_MULTIPLIER,
    DEFAULT_VOLATILITY_MULTIPLIER,
    EmergencyStopManager,
    EmergencyStopState,
    EmergencyTrigger,
)
from xauusd_bot.execution.orders import OrderManager
from xauusd_bot.execution.pending import (
    DEFAULT_CLUSTER_BREAK_ATR,
    DEFAULT_MAX_AGE_BARS,
    PendingOrderManager,
)
from xauusd_bot.execution.risk import (
    REASON_DAILY_LOSS_LIMIT,
    REASON_MAX_OPEN_EXPOSURE,
    REASON_MAX_TRADES_PER_SESSION,
    REASON_NEWS_BLACKOUT,
    REASON_OPPOSITE_POSITION,
    REASON_RISK_BAND_UNKNOWN,
    REASON_WEEKLY_LOSS_LIMIT,
    RISK_PCT_BY_BAND,
    RiskBand,
    RiskManager,
    risk_band_for_entry_type,
    risk_pct_for_band,
)
from xauusd_bot.execution.sizer import PositionSizer
from xauusd_bot.execution.stops import (
    DEFAULT_BE_BONUS_POINTS,
    DEFAULT_INITIAL_SL_ATR,
    DEFAULT_TRAIL_MIN_ATR,
    StopManager,
)
from xauusd_bot.execution.take_profit import (
    DEFAULT_TP1_PCT,
    DEFAULT_TP2_PCT,
    DEFAULT_TP3_PCT,
    TakeProfitManager,
)

__all__ = [
    # Risk
    "REASON_DAILY_LOSS_LIMIT",
    "REASON_MAX_OPEN_EXPOSURE",
    "REASON_MAX_TRADES_PER_SESSION",
    "REASON_NEWS_BLACKOUT",
    "REASON_OPPOSITE_POSITION",
    "REASON_RISK_BAND_UNKNOWN",
    "REASON_WEEKLY_LOSS_LIMIT",
    "RISK_PCT_BY_BAND",
    "RiskBand",
    "RiskManager",
    "risk_band_for_entry_type",
    "risk_pct_for_band",
    # Sizing
    "PositionSizer",
    # Order management
    "OrderManager",
    "PendingOrderManager",
    "DEFAULT_CLUSTER_BREAK_ATR",
    "DEFAULT_MAX_AGE_BARS",
    # Stops / TPs
    "StopManager",
    "TakeProfitManager",
    "DEFAULT_BE_BONUS_POINTS",
    "DEFAULT_INITIAL_SL_ATR",
    "DEFAULT_TRAIL_MIN_ATR",
    "DEFAULT_TP1_PCT",
    "DEFAULT_TP2_PCT",
    "DEFAULT_TP3_PCT",
    # Emergency
    "EmergencyStopManager",
    "EmergencyStopState",
    "EmergencyTrigger",
    "DEFAULT_PAUSE_DURATION",
    "DEFAULT_SLIPPAGE_MULTIPLIER",
    "DEFAULT_VOLATILITY_MULTIPLIER",
]
