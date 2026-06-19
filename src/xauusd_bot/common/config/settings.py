"""Re-export of :class:`Settings` from the package ``__init__`` so callers
can use either::

    from xauusd_bot.common.config import Settings
    from xauusd_bot.common.config.settings import Settings

This file exists to match the path called out in ``00_FINAL_PLAN.md``
§10 and the deliverables list. The actual implementation lives in
``xauusd_bot.common.config`` (the package ``__init__``) to avoid the
double-import confusion of submodule-equals-package.
"""

from xauusd_bot.common.config import (
    ConnectorMode,
    NewsProvider,
    ServiceRole,
    Settings,
    load_settings,
)

__all__ = [
    "ConnectorMode",
    "NewsProvider",
    "ServiceRole",
    "Settings",
    "load_settings",
]
