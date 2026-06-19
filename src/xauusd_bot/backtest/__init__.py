"""Backtest layer (Block 5b) — historical replay + walk-forward validation.

Public API
----------
* :class:`BacktestEngine` — :mod:`xauusd_bot.backtest.engine`
* :class:`WalkForwardEngine` — :mod:`xauusd_bot.backtest.walkforward`
* Slippage / spread models — :mod:`xauusd_bot.backtest.models`
* Result schemas — :mod:`xauusd_bot.common.schemas.backtest`

Architecture invariants
-----------------------
* **I-1** — never imports ``MetaTrader5`` (verified by
  ``tests/backtest/test_invariants.py``).
* **I-3 (PIT)** — only the :class:`ReplayConnector` provides bars;
  no future-leak.
* **I-4 (Brain vs Hands)** — the backtest engine is a *pure
  orchestrator* that calls the same feature / decision / execution
  modules the live stack uses. No backtest-only branches in those
  modules.
* **Determinism** — given the same inputs the engine produces an
  identical :class:`BacktestResult` on every run (no hidden state,
  no RNG).
"""

from xauusd_bot.backtest.engine import BacktestEngine, JournalSink
from xauusd_bot.backtest.models import (
    ChainedSlippage,
    FixedSlippage,
    FixedSpread,
    NewsAwareSpread,
    SlippageModel,
    SpreadModel,
    VolatilitySlippage,
    VolatilitySpread,
)
from xauusd_bot.backtest.walkforward import WalkForwardEngine
from xauusd_bot.common.schemas.backtest import (
    BacktestResult,
    BacktestStats,
    BreakdownEntry,
    WalkForwardResult,
    WalkForwardWindow,
)

__all__ = [
    # Engine
    "BacktestEngine",
    "WalkForwardEngine",
    "JournalSink",
    # Models
    "SlippageModel",
    "SpreadModel",
    "FixedSlippage",
    "FixedSpread",
    "VolatilitySlippage",
    "VolatilitySpread",
    "ChainedSlippage",
    "NewsAwareSpread",
    # Schemas
    "BacktestResult",
    "BacktestStats",
    "BreakdownEntry",
    "WalkForwardResult",
    "WalkForwardWindow",
]
