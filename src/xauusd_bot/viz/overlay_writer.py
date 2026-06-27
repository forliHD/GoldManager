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

# --- Chart FVG relevance horizon -------------------------------------------
# The FVG engine never expires a zone (mitigation is one-directional: a bullish
# demand only dies on a close BELOW it, so when price rallies far above and never
# returns it stays "open" forever). For the CHART that means dozens of stale
# bands stretching a day+ back, far from price ("viel zu weit nach hinten, wo
# nichts ist"). We trim what the chart shows here. This is CHART-ONLY: the
# decision engine reads ``bundle.fvg`` directly, so culling never changes a trade.
_FVG_MAX_AGE_H: dict[str, float] = {"H1": 48.0, "M5": 12.0, "M1": 4.0}
_FVG_MAX_PER_TF = 8
_FVG_MAX_DIST_ATR = 25.0  # drop zones whose nearest edge is > this×ATR from price
_FVG_MAX_DIST_FALLBACK = 60.0  # points, used when ATR is unavailable


def _cull_chart_fvg_zones(
    zones: list[Any],
    *,
    ts: datetime,
    current_price: float | None,
    atr: float | None,
) -> list[Any]:
    """Trim the FVG zones shown on the chart to the relevant, recent ones.

    Drops (1) fully-mitigated zones, (2) zones older than the per-TF age horizon,
    (3) zones whose nearest edge is implausibly far from the current price, then
    keeps the top-``_FVG_MAX_PER_TF`` per timeframe by ``rank_score``. Returns the
    surviving :class:`FVGZone` objects (chart-only — does not touch the decision
    path, which reads ``bundle.fvg``).
    """

    max_dist = (
        _FVG_MAX_DIST_ATR * atr if (atr and atr > 0) else _FVG_MAX_DIST_FALLBACK
    )
    kept_by_tf: dict[str, list[Any]] = {}
    for z in zones:
        if z.status.value == "mitigated":
            continue
        created = z.created_at
        if getattr(created, "tzinfo", None) is None:
            created = created.replace(tzinfo=ts.tzinfo)
        age_h = (ts - created).total_seconds() / 3600.0
        if age_h > _FVG_MAX_AGE_H.get(z.tf, 24.0):
            continue
        if current_price is not None:
            bull = z.type.value == "bullish"
            low = z.extended_bottom if (bull and z.extended_bottom is not None) else z.bottom
            high = z.extended_top if (not bull and z.extended_top is not None) else z.top
            if low > high:
                low, high = high, low
            if current_price > high:
                dist = current_price - high
            elif current_price < low:
                dist = low - current_price
            else:
                dist = 0.0
            if dist > max_dist:
                continue
        kept_by_tf.setdefault(z.tf, []).append(z)

    out: list[Any] = []
    for tf_zones in kept_by_tf.values():
        tf_zones.sort(key=lambda z: z.rank_score, reverse=True)
        out.extend(tf_zones[:_FVG_MAX_PER_TF])
    return out


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
    current_price: float | None = None,
    atr: float | None = None,
) -> dict[str, Any]:
    """Assemble the overlay JSON payload from engine outputs.

    The schema is stable — adding new fields is fine, removing or
    renaming breaks ``BotOverlay.mq5`` and requires a sync release.

    ``current_price``/``atr`` (when provided) drive the chart FVG relevance
    horizon — stale/far zones are trimmed via :func:`_cull_chart_fvg_zones` so
    the chart shows the same compact, near-price boxes a trader expects rather
    than dozens of day-old bands. Chart-only; the decision path is untouched.
    """

    vwap_section: dict[str, float | None] = {}
    for level_key in ("utc00", "utc07", "utc12"):
        v = vwap.levels.get(level_key)
        vwap_section[level_key] = v.value if v is not None else None

    vp_section: dict[str, Any] = {
        "daily": _profile_to_dict(volume_range.daily),
        "weekly": _profile_to_dict(volume_range.weekly),
        "monthly": _profile_to_dict(volume_range.monthly),
        "yearly": _profile_to_dict(volume_range.yearly),
        "prev_day": _profile_to_dict(volume_range.prev_day),
        "prev_week": _profile_to_dict(volume_range.prev_week),
        "prev_month": _profile_to_dict(volume_range.prev_month),
        "prev_year": _profile_to_dict(volume_range.prev_year),
    }

    fvg_zones: list[dict[str, Any]] = []
    for z in _cull_chart_fvg_zones(
        fvg.zones, ts=ts, current_price=current_price, atr=atr
    ):
        fvg_zones.append(
            {
                "tf": z.tf,
                "type": z.type.value,
                "top": z.top,
                "bottom": z.bottom,
                "size_points": z.size_points,
                "created_at": z.created_at.isoformat(),
                "status": z.status.value,
                "rank_score": z.rank_score,
                "extended_bottom": z.extended_bottom,
                "extended_top": z.extended_top,
                "extension_tf": z.extension_tf,
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
