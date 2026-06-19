"""Structured logging — JSON, correlated over setup/trade ids.

The bot's logs are the primary debugging tool when a service crashes
mid-trade. We want:

* **JSON output** for machine ingestion.
* **Correlation IDs** that flow across services (setup_id, trade_id).
* **Level filtering** via env (``LOG_LEVEL``).
* **Sane defaults** — caller only needs ``setup_logging(level=...)``
  once at process start.
"""

from xauusd_bot.common.logging.setup import (
    bind_correlation,
    clear_correlation,
    get_logger,
    setup_logging,
)

__all__ = [
    "bind_correlation",
    "clear_correlation",
    "get_logger",
    "setup_logging",
]
