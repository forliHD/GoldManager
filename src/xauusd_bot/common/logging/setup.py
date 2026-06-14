"""Structlog setup — JSON output, correlation contextvars, level filter.

Usage
-----
::

    from xauusd_bot.common.logging import setup_logging, bind_correlation, get_logger

    setup_logging(level="INFO")
    log = get_logger(__name__)
    log.info("starting_up", service="data-collector")

    with bind_correlation(setup_id="setup-2026-06-14", trade_id=None):
        log.info("processing_bar", symbol="XAUUSD")
        # both events now carry setup_id / trade_id in the JSON output
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def setup_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog + stdlib logging for JSON output.

    Idempotent: calling twice just re-binds handlers.

    Notes
    -----
    We use ``cache_logger_on_first_use=False`` so each call to
    :func:`structlog.get_logger` re-resolves the file handle. This makes
    the setup robust under pytest, which captures & closes ``sys.stdout``
    between tests; the cached logger would otherwise point at a closed
    file and explode with ``ValueError: I/O operation on closed file``.
    """

    log_level = _LOG_LEVELS.get(level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ]

    # Route structlog through the stdlib logging module. This means the
    # final emit goes through a logging.Handler, which re-resolves the
    # stream lazily. Robust under pytest, which captures & closes
    # ``sys.stdout`` between tests.
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    # Wipe any existing handlers so we don't double-emit (esp. under
    # repeated calls to ``setup_logging``).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(log_level)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _get_log_stream():
    """Resolve a writable log stream. Honors ``$LOG_STREAM`` if set (used in tests)."""

    import os

    target = os.environ.get("XAUUSD_LOG_STREAM", "").strip()
    if target:
        try:
            return open(target, "a", encoding="utf-8")
        except OSError:
            pass
    return sys.stdout


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger (use ``__name__`` as ``name``)."""

    return structlog.get_logger(name) if name else structlog.get_logger()


@contextmanager
def bind_correlation(**kv: object) -> Iterator[None]:
    """Bind context variables for the duration of the block.

    Example: ``with bind_correlation(setup_id="..."): ...`` — every log
    emitted inside the block carries ``setup_id``.
    """

    bind_contextvars(**kv)
    try:
        yield
    finally:
        unbind_contextvars(*kv.keys())


def clear_correlation() -> None:
    """Clear all bound context variables (e.g. between requests in a service)."""

    clear_contextvars()
