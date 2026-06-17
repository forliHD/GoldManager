"""Docker entrypoint dispatcher — picks the right service module by SERVICE_ROLE.

The shared service Dockerfile installs the package and sets
``ENTRYPOINT ["python", "-m", "xauusd_bot.docker_entrypoint"]``.
This module reads ``SERVICE_ROLE`` and dispatches to one of:

* ``data-collector``   → :mod:`xauusd_bot.data_collector`
* ``feature-engine``   → :mod:`xauusd_bot.feature_engine`
* ``decision-engine``  → :mod:`xauusd_bot.decision_engine`
* ``execution-engine`` → :mod:`xauusd_bot.execution_engine`
* ``journal-writer``   → :mod:`xauusd_bot.journal_writer`

A role with no entry in ``_DISPATCH`` (e.g. ``review``, which runs as an
on-demand CLI rather than a streaming daemon) logs a clear "not yet
implemented" message and exits non-zero so the container restart loop is
visible during bring-up.
"""

from __future__ import annotations

import sys

import structlog

from xauusd_bot.common.config import ServiceRole, load_settings
from xauusd_bot.common.logging import setup_logging

log = structlog.get_logger(__name__)


# Map of role → (module, attribute) for the entry point. Missing entries
# fall back to the smoke CLI for now (block 1 ships only data-collector).
_DISPATCH: dict[ServiceRole, tuple[str, str]] = {
    ServiceRole.DATA_COLLECTOR: ("xauusd_bot.data_collector", "main"),
    ServiceRole.FEATURE_ENGINE: ("xauusd_bot.feature_engine", "main"),
    ServiceRole.DECISION_ENGINE: ("xauusd_bot.decision_engine", "main"),
    ServiceRole.EXECUTION_ENGINE: ("xauusd_bot.execution_engine", "main"),
    ServiceRole.JOURNAL_WRITER: ("xauusd_bot.journal_writer", "main"),
    # REVIEW stays an on-demand CLI (backtest/review run on request, not
    # as a streaming daemon) — intentionally not dispatched here.
}


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    log.info("docker_entrypoint_starting", role=settings.service_role.value, env=settings.environment)

    if settings.service_role not in _DISPATCH:
        log.error(
            "service_role_not_implemented",
            role=settings.service_role.value,
            note="this role lands in a later build block — see 00_FINAL_PLAN.md §9",
        )
        return 78  # EX_CONFIG

    module_name, attr = _DISPATCH[settings.service_role]
    import importlib

    mod = importlib.import_module(module_name)
    fn = getattr(mod, attr, None)
    if fn is None:
        log.error("entrypoint_missing", module=module_name, attr=attr)
        return 70  # EX_SOFTWARE
    log.info("entrypoint_dispatched", module=module_name, attr=attr)
    result = fn()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
