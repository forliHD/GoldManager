"""Overlay Writer — emits the per-bar ``overlay_levels.json`` for BotOverlay.mq5.

The bot's MQL5 chart overlay (Plan §5) needs a compact JSON file with
the current levels to draw: VWAPs, Volume Profile (developing + locked
previous), and FVG zones. This module writes that file atomically
(write-tmp-then-rename) so the MQL5 reader never sees a half-written
file.

File schema (matches Plan §5.1)
-------------------------------
::

    {
      "ts": "2026-06-14T13:00:00Z",
      "vwap": {"utc00": 2370.4, "utc07": 2374.8, "utc12": 2378.2},
      "volume_profile": {
        "weekly":  {"vah": 2368, "vpoc": 2358, "val": 2346, "state": "developing"},
        "monthly": {"vah": 2390, "vpoc": 2372, "val": 2355, "state": "developing"},
        "yearly":  {"vah": 2450, "vpoc": 2380, "val": 2320, "state": "developing"},
        "prev_week":  {"vah": 2360, "vpoc": 2351, "val": 2340},
        "prev_month": {"vah": ...,    "vpoc": ...,    "val": ...},
        "prev_year":  {"vah": ...,    "vpoc": ...,    "val": ...}
      },
      "fvg_zones": [
        {"tf": "H1", "type": "bullish", "top": 2373.0, "bottom": 2371.5}
      ]
    }

``prev_*`` profiles are always the locked (completed) period of the
prior week/month/year — these are the levels traders actually anchor
on for support / resistance.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from xauusd_bot.common.schemas.features import (
    FVGOutput,
    TripleVWAPOutput,
    VolumeProfileOutput,
    VolumeRangeOutput,
)

log = structlog.get_logger(__name__)


def _profile_to_dict(profile: VolumeProfileOutput | None) -> dict[str, Any] | None:
    """Project a single :class:`VolumeProfileOutput` into the overlay schema."""

    if profile is None:
        return None
    out: dict[str, Any] = {
        "vah": profile.vah,
        "vpoc": profile.vpoc,
        "val": profile.val,
        "state": profile.state.value,
    }
    return out


def build_overlay_payload(
    *,
    ts: datetime,
    vwap: TripleVWAPOutput,
    volume_range: VolumeRangeOutput,
    fvg: FVGOutput,
) -> dict[str, Any]:
    """Assemble the overlay JSON payload from engine outputs.

    The schema is stable — adding new fields is fine, removing or
    renaming breaks ``BotOverlay.mq5`` and requires a sync release.
    """

    vwap_section: dict[str, float | None] = {}
    for level_key in ("utc00", "utc07", "utc12"):
        v = vwap.levels.get(level_key)
        vwap_section[level_key] = v.value if v is not None else None

    vp_section: dict[str, Any] = {
        "weekly": _profile_to_dict(volume_range.weekly),
        "monthly": _profile_to_dict(volume_range.monthly),
        "yearly": _profile_to_dict(volume_range.yearly),
        "prev_week": _profile_to_dict(volume_range.prev_week),
        "prev_month": _profile_to_dict(volume_range.prev_month),
        "prev_year": _profile_to_dict(volume_range.prev_year),
    }

    fvg_zones: list[dict[str, Any]] = []
    for z in fvg.zones:
        if z.status.value == "mitigated":
            continue  # dead zones don't show on the chart
        fvg_zones.append(
            {
                "tf": z.tf,
                "type": z.type.value,
                "top": z.top,
                "bottom": z.bottom,
                "size_points": z.size_points,
                "created_at": z.created_at.isoformat(),
                "status": z.status.value,
            }
        )

    return {
        "ts": ts.isoformat(),
        "vwap": vwap_section,
        "volume_profile": vp_section,
        "fvg_zones": fvg_zones,
    }


class OverlayWriter:
    """Atomic JSON writer for the per-bar overlay file.

    Parameters
    ----------
    output_path:
        File to write. Defaults to ``data/overlay/overlay_levels.json``
        (relative to the current working directory, or the path
        configured by ``XAUUSD_OVERLAY_PATH``).
    """

    DEFAULT_PATH = Path("data/overlay/overlay_levels.json")

    def __init__(self, output_path: Path | str | None = None) -> None:
        if output_path is None:
            output_path = os.environ.get("XAUUSD_OVERLAY_PATH", self.DEFAULT_PATH)
        self._path = Path(output_path)

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        *,
        ts: datetime,
        vwap: TripleVWAPOutput,
        volume_range: VolumeRangeOutput,
        fvg: FVGOutput,
    ) -> Path:
        """Atomically write the overlay JSON file.

        Returns the path written. The temp file is created in the same
        directory as the target so the rename is on the same filesystem
        (atomic rename only works on the same FS).
        """

        payload = build_overlay_payload(
            ts=ts,
            vwap=vwap,
            volume_range=volume_range,
            fvg=fvg,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to tmp file in the same dir, then rename.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".overlay-", suffix=".json.tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp_path, self._path)
        except Exception:
            # Clean up the tmp file on error.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
            raise
        log.info("overlay_written", path=str(self._path), ts=ts.isoformat())
        return self._path
