"""Connector layer: IMarketConnector protocol + Replay / Live / Paper / Safety.

The connector is the **only** module that knows how to talk to a broker or
to historical data. Everything above (features, decision, execution) speaks
exclusively to the connector through the protocol defined in
:mod:`xauusd_bot.connectors.base`.

Hard rule (see ``00_FINAL_PLAN.md`` §1 Δ5 + §3.2): the ``MetaTrader5`` package
must only be imported inside :mod:`xauusd_bot.connectors.live`. Replay and
Paper must run on macOS without any Windows-only dependency.
"""

from xauusd_bot.connectors.base import (
    IMarketConnector,
)
from xauusd_bot.connectors.paper_broker import PaperBroker
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.connectors.safety import (
    PreTradeSafetyChecker,
    SafetyVerdict,
)

# LiveMT5Connector is intentionally NOT exported at module top level because
# importing it pulls in the Windows-only `MetaTrader5` package. Import it
# explicitly inside code paths that require the live bridge (which only runs
# on the Ubuntu prod stack).
__all__ = [
    "IMarketConnector",
    "PaperBroker",
    "PreTradeSafetyChecker",
    "ReplayConnector",
    "SafetyVerdict",
]
