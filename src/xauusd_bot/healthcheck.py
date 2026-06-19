"""Container healthcheck — is this service's heartbeat fresh?

Run as ``python -m xauusd_bot.healthcheck`` from the compose
healthcheck. Reads ``SERVICE_ROLE`` to find the right
``logs/<role>.alive`` file (written by :func:`service_runtime`) and
exits 0 if it was touched within ``HEARTBEAT_MAX_AGE_SECONDS`` (default
60s), else 1. A wedged event loop stops touching the file, so the
container goes unhealthy even though the process is technically alive.
"""

from __future__ import annotations

import os
import sys
import time

from xauusd_bot.common.service import heartbeat_path


def main() -> int:
    role = os.environ.get("SERVICE_ROLE", "")
    if not role:
        print("SERVICE_ROLE not set", file=sys.stderr)
        return 1
    path = heartbeat_path(role)
    if not path.exists():
        print(f"heartbeat file missing: {path}", file=sys.stderr)
        return 1
    max_age = float(os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "60"))
    age = time.time() - path.stat().st_mtime
    if age >= max_age:
        print(f"heartbeat stale: {age:.1f}s >= {max_age:.0f}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
