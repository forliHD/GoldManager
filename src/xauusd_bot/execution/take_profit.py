"""TakeProfitManager — multi-tier TP and runner behaviour (Block 4 Phase 3).

The :class:`TakeProfitManager` builds the *where are the TPs?* answer
for an open position. It uses a three-tier model:

* **TP1** — the first available liquidity pool or 1R (one times the
  risk amount). Closes 30 % of the position.
* **TP2** — the next M5 / H1 zone (M5 OB, FVG, or H1 zone). Closes
  30 %.
* **TP3 / Runner** — the nearest higher-timeframe volume profile
  level (Weekly VAH, VPOC, or VAL). Closes the remaining 40 % as a
  runner.

Runner behaviour
----------------
The runner is closed when the price **rejects** the HTF level
(wick through + close back, or BOS against the runner direction).
The runner stays alive while the price **accepts** the level
(2+ consecutive M5 closes on the right side of the level).

The runner is *not* evaluated here — the executor's main loop calls
:meth:`should_close_runner` per bar to make the decision. This module
just builds the TP prices and the partial-close plan.

Partial-close fractions
-----------------------
30 % / 30 % / 40 % is the default (canonical, from
05_execution_risk.md). Configurable per :class:`Settings` (out of
scope for Block 4 — the defaults are hard-coded here for now).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.schemas.execution import StopsAndTPs
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.schemas import OrderSide, SymbolSpec

log = structlog.get_logger(__name__)


# Default partial-close fractions.
DEFAULT_TP1_PCT = 30.0
DEFAULT_TP2_PCT = 30.0
DEFAULT_TP3_PCT = 40.0

# Sanity bound on an LLM-supplied TP R-multiple (Phase C). A wild value (e.g.
# 500) is ignored and the deterministic target is used instead.
_MAX_TP_RR = 20.0


# ----------------------------------------------------------------- helpers


def _round(price: Decimal, spec: SymbolSpec) -> Decimal:
    q = Decimal(10) ** -spec.digits
    return price.quantize(q)


def _nearest_zone_above(
    zones: Iterable[object], current_price: float
) -> float | None:
    """Return the closest zone price ABOVE ``current_price`` (or None)."""

    best: float | None = None
    for z in zones:
        center = float(getattr(z, "center", z))  # type: ignore[arg-type]
        if center > current_price and (best is None or center < best):
            best = center
    return best


def _nearest_zone_below(
    zones: Iterable[object], current_price: float
) -> float | None:
    """Return the closest zone price BELOW ``current_price`` (or None)."""

    best: float | None = None
    for z in zones:
        center = float(getattr(z, "center", z))  # type: ignore[arg-type]
        if center < current_price and (best is None or center > best):
            best = center
    return best


def _htf_level(
    bundle: FeatureSnapshotBundle,
    side: OrderSide,
    current_price: float,
    *,
    beyond_price: float | None = None,
) -> tuple[float | None, str]:
    """Return the nearest HTF volume-profile level for the runner.

    Priority order (per profile, then across profiles):

    1. **Weekly** profile: prefer VAH (long) / VAL (short); fall back to VPOC.
    2. **Monthly** profile: same.
    3. **Yearly** profile: same.

    Within the chosen primary level, pick the **nearest** price on the
    correct side of ``beyond_price`` (the threshold the runner target must sit
    past — e.g. TP2 — so TP3 is never nearer than TP2). Defaults to
    ``current_price`` when not given.

    Returns
    -------
    (price, label) or (None, '').
    """

    if bundle.volume_range is None:
        return (None, "")
    vr = bundle.volume_range
    # The runner level must sit BEYOND this threshold in the trade direction.
    threshold = current_price if beyond_price is None else float(beyond_price)

    # Iterate profiles in priority order (weekly → monthly → yearly).
    # The first profile that has a usable level wins; if it has
    # multiple candidates (VAH + VPOC) on the right side, take the
    # nearest one. This is "the most relevant profile is the
    # shortest-timeframe profile that has data".
    for profile in (vr.weekly, vr.monthly, vr.yearly):
        primary_label = f"{profile.name.value}_vah" if side == OrderSide.BUY else f"{profile.name.value}_val"
        primary = profile.vah if side == OrderSide.BUY else profile.val
        candidates: list[tuple[float, str]] = []
        if primary is not None:
            on_right = (side == OrderSide.BUY and primary > threshold) or (
                side == OrderSide.SELL and primary < threshold
            )
            if on_right:
                candidates.append((primary, primary_label))
        if profile.vpoc is not None:
            on_right_vpoc = (side == OrderSide.BUY and profile.vpoc > threshold) or (
                side == OrderSide.SELL and profile.vpoc < threshold
            )
            if on_right_vpoc:
                candidates.append((profile.vpoc, f"{profile.name.value}_vpoc"))
        if not candidates:
            continue
        # Pick the nearest on the right side.
        if side == OrderSide.BUY:
            candidates.sort(key=lambda c: c[0])  # smallest first
        else:
            candidates.sort(key=lambda c: -c[0])  # largest first
        return candidates[0]
    return (None, "")


# ----------------------------------------------------------------- manager


class TakeProfitManager:
    """Build the three-tier TP plan + decide on runner continuation."""

    def __init__(
        self,
        spec: SymbolSpec,
        *,
        tp1_pct: float = DEFAULT_TP1_PCT,
        tp2_pct: float = DEFAULT_TP2_PCT,
        tp3_pct: float = DEFAULT_TP3_PCT,
    ) -> None:
        assert abs(tp1_pct + tp2_pct + tp3_pct - 100.0) < 1e-6, "TP percentages must sum to 100"
        self._spec = spec
        self._tp1_pct = tp1_pct
        self._tp2_pct = tp2_pct
        self._tp3_pct = tp3_pct

    @property
    def spec(self) -> SymbolSpec:
        return self._spec

    # -------------------------------------------------------------- compute

    def compute(
        self,
        side: OrderSide,
        entry_price: Decimal,
        sl_price: Decimal,
        bundle: FeatureSnapshotBundle,
        *,
        now: datetime | None = None,
        tp1_rr: float | None = None,
        tp2_rr: float | None = None,
    ) -> StopsAndTPs:
        """Build the TP1 / TP2 / TP3 plan + the partial-close schedule.

        SL price is used to compute the 1R distance. ``tp1_rr`` / ``tp2_rr``
        (Phase C) are the LLM's R-multiple targets: when provided (and sane,
        ``0 < rr <= _MAX_TP_RR``) the matching TP is placed at
        ``entry ± rr × 1R`` instead of the deterministic liquidity/FVG search.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        reasoning: list[str] = []
        sl_distance = abs(entry_price - sl_price)
        one_r = entry_price + sl_distance if side == OrderSide.BUY else entry_price - sl_distance

        def _rr_to_price(rr: float) -> Decimal:
            dist = sl_distance * Decimal(str(rr))
            return _round(entry_price + dist if side == OrderSide.BUY else entry_price - dist, self._spec)

        ai_tp1 = tp1_rr if (tp1_rr is not None and 0 < tp1_rr <= _MAX_TP_RR) else None
        ai_tp2 = tp2_rr if (tp2_rr is not None and 0 < tp2_rr <= _MAX_TP_RR) else None
        # Guard against an inverted AI pair (tp1_rr > tp2_rr): TP1 must be the
        # NEARER target so the partial-close + break-even sequence fires in order
        # (else TP2 banks first and break-even only arms at the further TP1).
        if ai_tp1 is not None and ai_tp2 is not None and ai_tp1 > ai_tp2:
            ai_tp1, ai_tp2 = ai_tp2, ai_tp1
            reasoning.append("AI tp1_rr>tp2_rr → swapped so TP1 is nearer")

        # --- TP1: LLM R-target → nearest liquidity zone → 1R fallback.
        tp1: Decimal | None = None
        tp1_label = ""
        if ai_tp1 is not None:
            tp1 = _rr_to_price(ai_tp1)
            tp1_label = f"ai_{ai_tp1:g}R"
        elif bundle.liquidity is not None:
            zones = (
                bundle.liquidity.tp_targets_above
                if side == OrderSide.BUY
                else bundle.liquidity.tp_targets_below
            )
            nearest = _nearest_zone_above(zones, float(entry_price)) if side == OrderSide.BUY else _nearest_zone_below(zones, float(entry_price))
            if nearest is not None:
                tp1 = _round(Decimal(str(nearest)), self._spec)
                tp1_label = "liquidity_zone"
            else:
                tp1 = _round(one_r, self._spec)
                tp1_label = "1R"
        else:
            tp1 = _round(one_r, self._spec)
            tp1_label = "1R"
        reasoning.append(f"TP1 = {tp1} ({tp1_label}, {self._tp1_pct:.0f}%)")

        # --- TP2: LLM R-target → next FVG / OB (we use the M5 FVG list) → 2R.
        tp2: Decimal | None = None
        tp2_label = ""
        if ai_tp2 is not None:
            tp2 = _rr_to_price(ai_tp2)
            tp2_label = f"ai_{ai_tp2:g}R"
        elif bundle.fvg is not None:
            for zone in bundle.fvg.zones:
                if side == OrderSide.BUY and zone.type.value == "bullish":
                    # Bullish FVG top is a magnet for continuation.
                    candidate = _round(Decimal(str(zone.top)), self._spec)
                    if candidate > entry_price and (tp2 is None or candidate < tp2):
                        tp2 = candidate
                        tp2_label = f"M5_bull_fvg_{zone.tf}"
                        break
                if side == OrderSide.SELL and zone.type.value == "bearish":
                    candidate = _round(Decimal(str(zone.bottom)), self._spec)
                    if candidate < entry_price and (tp2 is None or candidate > tp2):
                        tp2 = candidate
                        tp2_label = f"M5_bear_fvg_{zone.tf}"
                        break
        if tp2 is None:
            # Fall back to 2R.
            two_r = entry_price + (sl_distance * 2) if side == OrderSide.BUY else entry_price - (sl_distance * 2)
            tp2 = _round(two_r, self._spec)
            tp2_label = "2R"
        reasoning.append(f"TP2 = {tp2} ({tp2_label}, {self._tp2_pct:.0f}%)")

        # --- TP3 / Runner: HTF volume level, but ALWAYS the furthest target.
        # The runner level must sit BEYOND TP2 — otherwise the nearest HTF level
        # can land between entry and TP1 (a short whose weekly VAL is 0.5pt below
        # entry → TP3≈entry), and the broker backstop (= furthest target) then
        # attaches a near-entry TP that price hits instantly. Pass TP2 as the
        # floor so only a level past it qualifies; else fall back to 3R.
        three_r = entry_price + (sl_distance * 3) if side == OrderSide.BUY else entry_price - (sl_distance * 3)
        htf_price, htf_label = _htf_level(bundle, side, float(entry_price), beyond_price=float(tp2))
        if htf_price is not None:
            tp3 = _round(Decimal(str(htf_price)), self._spec)
            reasoning.append(f"TP3 / runner = {tp3} ({htf_label} beyond TP2, {self._tp3_pct:.0f}%)")
        else:
            tp3 = _round(three_r, self._spec)
            reasoning.append(f"TP3 / runner = {tp3} (3R fallback, {self._tp3_pct:.0f}%)")
        # Defensive clamp: never let TP3 sit at/inside TP2 (e.g. a far-FVG TP2
        # beyond 3R). Push it one R past TP2 so the tier order always holds.
        if (side == OrderSide.BUY and tp3 <= tp2) or (side == OrderSide.SELL and tp3 >= tp2):
            bumped = tp2 + sl_distance if side == OrderSide.BUY else tp2 - sl_distance
            tp3 = _round(bumped, self._spec)
            reasoning.append(f"TP3 clamped one R beyond TP2 → {tp3}")

        plan = [
            {"level": "tp1", "price": str(tp1), "pct": self._tp1_pct / 100.0},
            {"level": "tp2", "price": str(tp2), "pct": self._tp2_pct / 100.0},
            {"level": "tp3", "price": str(tp3), "pct": self._tp3_pct / 100.0},
        ]
        return StopsAndTPs(
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            trail_active=False,
            partial_close_plan=plan,
            reasoning=reasoning,
            timestamp=ts,
        )

    # ---------------------------------------------------------------- runner

    def should_close_runner(
        self,
        side: OrderSide,
        tp3_price: Decimal,
        current_close: Decimal,
        bundle: FeatureSnapshotBundle,
    ) -> tuple[bool, str]:
        """Decide whether the runner should be closed on the current bar.

        Returns
        -------
        (should_close, reason)
            ``should_close`` is True if a rejection pattern is detected
            (wick through the level + close back, or BOS against the
            runner direction).
        """

        if bundle.structure is None:
            return False, "no_structure_data"

        last = bundle.structure.last_bos or bundle.structure.last_choch
        if last is None:
            return False, "no_recent_structure_event"

        from xauusd_bot.common.schemas.features import StructureEventType

        if side == OrderSide.BUY:
            # Rejection of a long runner at a VAH-style resistance:
            # BOS_down / CHOCH_down right at the TP3 level.
            against = last.type in (
                StructureEventType.BOS_DOWN,
                StructureEventType.CHOCH_DOWN,
            )
            if against and float(current_close) < float(tp3_price):
                return True, "structure_rejection_against_runner"
            # Acceptance: 2+ closes above the TP3 (the runner keeps running).
            # We can't cheaply count closes here without the bar list; the
            # caller can use the executor's position monitor to make that
            # decision. We just return False for now.
            return False, "runner_continues"
        # Short runner
        against = last.type in (
            StructureEventType.BOS_UP,
            StructureEventType.CHOCH_UP,
        )
        if against and float(current_close) > float(tp3_price):
            return True, "structure_rejection_against_runner"
        return False, "runner_continues"


__all__ = [
    "DEFAULT_TP1_PCT",
    "DEFAULT_TP2_PCT",
    "DEFAULT_TP3_PCT",
    "TakeProfitManager",
]
