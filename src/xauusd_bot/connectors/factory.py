"""Connector factory — pick Replay vs Live from :class:`Settings`.

The stream-connected services (data-collector, execution-engine) call
:func:`make_connector` instead of hard-coding a connector, so the same
service image runs in dev (``CONNECTOR_MODE=replay``) and prod
(``CONNECTOR_MODE=live``) with only an env var changing.

I-1 (connector isolation): :class:`LiveMT5Connector` is imported lazily,
inside the ``live`` branch, so the replay path never pulls in the RPyC
client. The ``MetaTrader5`` package itself is still confined to the
bridge server — ``live.py`` only speaks RPyC.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from xauusd_bot.common.config import Settings
from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.replay import ReplayConnector

log = structlog.get_logger(__name__)


def make_connector(settings: Settings) -> IMarketConnector:
    """Construct the connector selected by ``settings.connector_mode``.

    * ``replay`` → :class:`ReplayConnector` reading ``settings.replay_source``.
    * ``live`` → :class:`LiveMT5Connector` (RPyC client). Requires
      ``MT5_LOGIN`` / ``MT5_PASSWORD`` / ``MT5_SERVER`` to be set.
    """

    if settings.is_live_connector():
        if not (settings.mt5_login and settings.mt5_password and settings.mt5_server):
            raise RuntimeError(
                "CONNECTOR_MODE=live requires MT5_LOGIN, MT5_PASSWORD and MT5_SERVER to be set."
            )
        from xauusd_bot.connectors.live import LiveMT5Connector

        auth_key = (
            settings.mt5_bridge_auth_key.get_secret_value()
            if settings.mt5_bridge_auth_key is not None
            else None
        )
        log.info(
            "connector_factory_live",
            host=settings.mt5_bridge_host,
            port=settings.mt5_bridge_port,
            symbol=settings.symbol,
        )
        return LiveMT5Connector(
            host=settings.mt5_bridge_host,
            port=settings.mt5_bridge_port,
            login=int(settings.mt5_login),
            password=settings.mt5_password.get_secret_value(),
            server=settings.mt5_server,
            symbol=settings.symbol,
            auth_key=auth_key,
        )

    source = Path(settings.replay_source)
    log.info("connector_factory_replay", source=str(source), symbol=settings.symbol)
    return ReplayConnector(source_path=source, symbol=settings.symbol)


__all__ = ["make_connector"]
