"""Docker entrypoint dispatcher — picks the right service module by SERVICE_ROLE.

The shared service Dockerfile installs the package and sets
``ENTRYPOINT ["python", "-m", "xauusd_bot.docker_entrypoint"]``.
This module reads ``SERVICE_ROLE`` and dispatches to one of:

* ``data-collector``  → data-collector service module (block 1: replay smoke)
* ``feature-engine``  → feature-engine service module (block 2)
* ``decision-engine`` → decision-engine service module (block 4)
* ``execution-engine`` → execution-engine service module (block 4)
* ``review``          → review / backtest CLI (block 6)

Until later blocks ship their service modules, the dispatcher
*intentionally* logs a clear "not yet implemented" message and exits
non-zero so the container restart loop is visible during bring-up.
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
    ServiceRole.DATA_COLLECTOR: ("xauusd_bot.cli.replay_smoke", "main"),
    # Future blocks will fill these in:
    # ServiceRole.FEATURE_ENGINE: ("xauusd_bot.feature_engine", "main"),
    # ServiceRole.DECISION_ENGINE: ("xauusd_bot.decision_engine", "main"),
    # ServiceRole.EXECUTION_ENGINE: ("xauusd_bot.execution_engine", "main"),
    # ServiceRole.REVIEW: ("xauusd_bot.review_engine", "main"),
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
