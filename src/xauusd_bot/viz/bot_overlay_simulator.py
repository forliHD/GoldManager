"""Python simulator for ``BotOverlay.mq5``.

Re-implements the file-read + schema-decoding logic from
``mql5/BotOverlay.mq5`` in Python so we can test it without an MT5
runtime. The MQL5 indicator reads ``MQL5/Files/overlay_levels.json``,
extracts VWAP / Volume Profile / FVG levels via substring-based
helpers (MQL5 stdlib has no JsonParse for the build we target), and
emits a set of draw operations.

We mirror that logic here and produce a list of
:class:`DrawOp` records that represent what the indicator would draw.
This is the test seam — visual validation still needs a real chart.

Why not just import the MQL5 code? MQL5 is not Python. The simulator
exists to assert that the *data processing* (file parsing + null
handling + style mapping) is correct. The chart rendering itself is
visual and must be checked manually in MetaTrader.

Why a list of DrawOps and not a mock chart? Easier to assert against,
simpler to dump for debugging, and trivially diffable across two
overlay snapshots (which is how OnTimer-style re-render is tested).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)


# Color palette — mirrors the MQL5 #define constants.
COLOR_VWAP_00 = "dodgerblue"
COLOR_VWAP_07 = "orange"
COLOR_VWAP_12 = "magenta"
COLOR_VP_DEV = "gray"
COLOR_VP_LOCK = "white"
COLOR_VP_PREV = "yellow"
COLOR_FVG_BULL = "green"
COLOR_FVG_BEAR = "red"

DrawKind = Literal["hline", "rect", "label"]


@dataclass(frozen=True)
class DrawOp:
    """A single drawing operation that the MQL5 indicator would emit."""

    kind: DrawKind
    name: str
    price: float | None = None
    top: float | None = None
    bottom: float | None = None
    color: str = ""
    style: Literal["solid", "dot"] = "solid"
    filled: bool = False
    back: bool = False
    text: str = ""

    def op_key(self) -> str:
        """Stable identifier for diff-comparing DrawOp lists across two reads."""

        return f"{self.kind}:{self.name}"


@dataclass
class DrawPlan:
    """Result of simulating one ``OnTimer()`` cycle."""

    ops: list[DrawOp] = field(default_factory=list)
    ts: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def hlines(self) -> list[DrawOp]:
        return [op for op in self.ops if op.kind == "hline"]

    @property
    def rects(self) -> list[DrawOp]:
        return [op for op in self.ops if op.kind == "rect"]

    @property
    def labels(self) -> list[DrawOp]:
        return [op for op in self.ops if op.kind == "label"]


# Object names — mirror the MQL5 OBJ_VWAP / VP_PERIODS tables.
VWAP_OBJECT_NAMES = ("vwap_utc00", "vwap_utc07", "vwap_utc12")
VWAP_COLOR_BY_KEY = {"utc00": COLOR_VWAP_00, "utc07": COLOR_VWAP_07, "utc12": COLOR_VWAP_12}
VP_PERIODS = ("weekly", "monthly", "yearly", "prev_week", "prev_month", "prev_year")


def simulate_mql5_read(path: Path) -> DrawPlan:
    """Read ``overlay_levels.json`` and produce the DrawOps the MQL5 indicator would emit.

    Mirrors the MQL5 code path 1:1: VWAPs → Volume Profiles → FVG zones.
    All defensive checks (missing file, corrupt JSON, null fields, missing
    keys, prev_*=null) are honored. The MQL5 chart validation remains
    manual — see AGENTS.md §4g-2.
    """

    plan = DrawPlan()

    # 1. File presence
    if not path.exists():
        plan.errors.append(f"file_missing:{path}")
        log.warning("BotOverlay: %s missing — skipping draw", path)
        return plan

    # 2. JSON parsing — the MQL5 indicator treats unreadable JSON as
    #    a no-op (warn + return). We do the same.
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        plan.errors.append(f"corrupt_json:{exc}")
        plan.warnings.append("BotOverlay: corrupt JSON, skipping draw")
        log.warning("BotOverlay: corrupt JSON in %s — %s", path, exc)
        return plan
    except OSError as exc:
        plan.errors.append(f"io_error:{exc}")
        log.warning("BotOverlay: cannot read %s — %s", path, exc)
        return plan

    if not isinstance(data, dict):
        plan.warnings.append("BotOverlay: top-level not a dict, skipping draw")
        return plan

    plan.ts = data.get("ts") if isinstance(data.get("ts"), str) else None

    # 3. Unknown top-level field tolerance — MQL5 simply ignores keys it
    #    doesn't know about. We do too.
    # (Implicit: we only read the documented sections.)

    # 4. VWAPs — handle nested { "vwap": {...} } (overlay schema) OR
    #    flat top-level "utc00"/"utc07"/"utc12" (defensive fallback).
    vwap_section = data.get("vwap")
    vwap_scope: dict[str, Any]
    if isinstance(vwap_section, dict):
        vwap_scope = vwap_section
    else:
        # Fall back: use top-level keys directly.
        vwap_scope = data

    for key, obj_name in zip(("utc00", "utc07", "utc12"), VWAP_OBJECT_NAMES):
        v = vwap_scope.get(key)
        if v is None:
            continue  # null field — skip
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            plan.warnings.append(f"vwap.{key}: not a number, skipping")
            continue
        fv = float(v)
        if math.isnan(fv) or math.isinf(fv):
            plan.warnings.append(f"vwap.{key}: NaN/Inf, skipping")
            continue
        if fv <= 0:
            # Negative prices are valid in backtests with synthetic data.
            # Only NaN / Inf is filtered (already handled above).
            pass
        plan.ops.append(
            DrawOp(
                kind="hline",
                name=obj_name,
                price=fv,
                color=VWAP_COLOR_BY_KEY[key],
                style="solid",
            )
        )

    # 5. Volume profiles.
    vp_section = data.get("volume_profile")
    if not isinstance(vp_section, dict):
        plan.warnings.append("volume_profile missing or not a dict, skipping")
    else:
        for period in VP_PERIODS:
            block = vp_section.get(period)
            if block is None:
                # null prev_* (AGENTS.md §4b-1) → gracefully skip.
                continue
            if not isinstance(block, dict):
                plan.warnings.append(f"volume_profile.{period}: not a dict, skipping")
                continue

            state = block.get("state", "")
            is_prev = period.startswith("prev_")
            is_dev = state == "developing"

            if is_prev:
                color = COLOR_VP_PREV
            elif is_dev:
                color = COLOR_VP_DEV
            else:
                # No state field → defensive default to "locked".
                color = COLOR_VP_LOCK
            style = "dot" if is_dev else "solid"

            def _opt_num(name: str) -> float | None:
                v = block.get(name)
                if v is None:
                    return None
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    plan.warnings.append(
                        f"volume_profile.{period}.{name}: not a number, skipping"
                    )
                    return None
                fv = float(v)
                if math.isnan(fv) or math.isinf(fv):
                    plan.warnings.append(
                        f"volume_profile.{period}.{name}: NaN/Inf, skipping"
                    )
                    return None
                return fv

            vah = _opt_num("vah")
            vpoc = _opt_num("vpoc")
            val = _opt_num("val")

            if vah is not None:
                plan.ops.append(
                    DrawOp(
                        kind="hline",
                        name=f"vp_{period}_vah",
                        price=vah,
                        color=color,
                        style=style,
                    )
                )
                plan.ops.append(
                    DrawOp(
                        kind="label",
                        name=f"vp_{period}_vah_l",
                        price=vah,
                        color="white",
                        text=f"{period} VAH {vah:.1f}",
                    )
                )
            if vpoc is not None:
                plan.ops.append(
                    DrawOp(
                        kind="hline",
                        name=f"vp_{period}_vpoc",
                        price=vpoc,
                        color=color,
                        style=style,
                    )
                )
                plan.ops.append(
                    DrawOp(
                        kind="label",
                        name=f"vp_{period}_vpoc_l",
                        price=vpoc,
                        color="white",
                        text=f"{period} VPOC {vpoc:.1f}",
                    )
                )
            if val is not None:
                plan.ops.append(
                    DrawOp(
                        kind="hline",
                        name=f"vp_{period}_val",
                        price=val,
                        color=color,
                        style=style,
                    )
                )
                plan.ops.append(
                    DrawOp(
                        kind="label",
                        name=f"vp_{period}_val_l",
                        price=val,
                        color="white",
                        text=f"{period} VAL {val:.1f}",
                    )
                )
            if vah is not None and val is not None and vah > val:
                plan.ops.append(
                    DrawOp(
                        kind="rect",
                        name=f"va_{period}",
                        top=vah,
                        bottom=val,
                        color=color,
                        style=style,
                        filled=True,
                        back=is_dev,
                    )
                )

    # 6. FVG zones.
    fvg_zones = data.get("fvg_zones")
    if isinstance(fvg_zones, list):
        for idx, zone in enumerate(fvg_zones):
            if not isinstance(zone, dict):
                plan.warnings.append(f"fvg_zones[{idx}]: not a dict, skipping")
                continue
            top = zone.get("top")
            bot = zone.get("bottom")
            if not isinstance(top, (int, float)) or isinstance(top, bool):
                plan.warnings.append(f"fvg_zones[{idx}].top invalid, skipping")
                continue
            if not isinstance(bot, (int, float)) or isinstance(bot, bool):
                plan.warnings.append(f"fvg_zones[{idx}].bottom invalid, skipping")
                continue
            ft, fb = float(top), float(bot)
            if math.isnan(ft) or math.isinf(ft) or math.isnan(fb) or math.isinf(fb):
                plan.warnings.append(f"fvg_zones[{idx}]: NaN/Inf, skipping")
                continue
            if ft <= fb:
                plan.warnings.append(f"fvg_zones[{idx}]: top<=bottom, skipping")
                continue
            ztype = zone.get("type", "")
            # Defensive default: missing/unknown type treated as bullish.
            color = COLOR_FVG_BEAR if ztype == "bearish" else COLOR_FVG_BULL
            plan.ops.append(
                DrawOp(
                    kind="rect",
                    name=f"fvg_{idx}",
                    top=ft,
                    bottom=fb,
                    color=color,
                    style="solid",
                    filled=True,
                    back=True,
                )
            )

    return plan


def plan_to_dict(plan: DrawPlan) -> dict[str, Any]:
    """Serialize a DrawPlan to a JSON-friendly dict (for debugging / logs)."""

    return {
        "ts": plan.ts,
        "n_ops": len(plan.ops),
        "n_hlines": len(plan.hlines),
        "n_rects": len(plan.rects),
        "n_labels": len(plan.labels),
        "warnings": plan.warnings,
        "errors": plan.errors,
        "ops": [
            {
                "kind": op.kind,
                "name": op.name,
                "price": op.price,
                "top": op.top,
                "bottom": op.bottom,
                "color": op.color,
                "style": op.style,
                "filled": op.filled,
                "back": op.back,
                "text": op.text,
            }
            for op in plan.ops
        ],
    }