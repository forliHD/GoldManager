"""StopManager — initial, break-even, and trailing stop logic (Block 4 Phase 3).

The :class:`StopManager` owns the *where is the SL?* question for an
open position. It has three modes (see
:class:`~xauusd_bot.common.schemas.execution.TrailingMode`):

* **FIXED** — the initial SL stays put for the life of the trade.
* **BREAK_EVEN** — once TP1 is hit, the SL is moved to entry +
  spread + commission (i.e. the trade becomes risk-free for the
  remaining size).
* **STRUCTURE_TRAIL** — after a TP1 hit, the SL trails behind each
  new M5 BOS in the trade's favour, with a minimum distance of
  ``min_trail_atr_multiplier`` × ATR(14) from the most recent swing
  point.

Initial-SL construction
-----------------------
The initial SL is **behind structure + ATR buffer**:

* For a long trade: the SL is the most recent M5 swing low minus
  ``1.0 × ATR(14)``.
* For a short trade: the SL is the most recent M5 swing high plus
  ``1.0 × ATR(14)``.

If the bundle has no structure events, the SL falls back to a
ATR-only distance from the entry price.

I-1
---
Reads prices from the bundle (computed by Block 2 feature engines)
and the connector's :class:`SymbolSpec`. No ``MetaTrader5`` import.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.schemas.execution import StopsAndTPs, TrailingMode
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.schemas import OrderSide, SymbolSpec

log = structlog.get_logger(__name__)


# Multipliers — the canonical numbers from 05_execution_risk.md.
# Lever #2 (exit tuning): the initial SL buffer behind structure was 1.0×ATR,
# which made the stop wide → a full stop = a large −R. Tightened to 0.5×ATR so
# the risk per trade shrinks (better R:R); backtested against 1.0 before live.
DEFAULT_INITIAL_SL_ATR = 0.5
DEFAULT_TRAIL_MIN_ATR = 1.0
DEFAULT_BE_BONUS_POINTS = 5.0  # tiny buffer so commission+spread is covered
# SL floor: a structure stop closer to entry than this is pushed out, so the
# lot size (risk / sl_distance) can't explode on a tiny stop. floor =
# max(DEFAULT_MIN_SL_ATR × ATR, DEFAULT_MIN_SL_POINTS).
DEFAULT_MIN_SL_ATR = 0.6
DEFAULT_MIN_SL_POINTS = 3.0


# ----------------------------------------------------------------- helpers


def _atr_safe(bundle: FeatureSnapshotBundle) -> float:
    """Return the bundle's ATR (0.0 if missing)."""

    if bundle.atr is None:
        return 0.0
    return float(bundle.atr)


def _last_swing(bundle: FeatureSnapshotBundle, kind: str) -> float | None:
    """Return the price of the most recent swing high (or low) from the bundle.

    The :class:`FeatureSnapshotBundle` exposes the structure engine's
    swings via ``bundle.structure.swings``.
    """

    if bundle.structure is None:
        return None
    for sw in reversed(bundle.structure.swings):
        if sw.kind == kind:
            return float(sw.price)
    return None


def _round(price: Decimal, spec: SymbolSpec) -> Decimal:
    """Round ``price`` to the symbol's digits."""

    q = Decimal(10) ** -spec.digits
    return price.quantize(q)


# ----------------------------------------------------------------- manager


class StopManager:
    """Compute and update the SL for an open position."""

    def __init__(
        self,
        spec: SymbolSpec,
        *,
        initial_sl_atr: float = DEFAULT_INITIAL_SL_ATR,
        trail_min_atr: float = DEFAULT_TRAIL_MIN_ATR,
        be_bonus_points: float = DEFAULT_BE_BONUS_POINTS,
        min_sl_atr: float = DEFAULT_MIN_SL_ATR,
        min_sl_points: float = DEFAULT_MIN_SL_POINTS,
    ) -> None:
        self._spec = spec
        self._initial_sl_atr = initial_sl_atr
        self._trail_min_atr = trail_min_atr
        self._be_bonus_points = be_bonus_points
        self._min_sl_atr = min_sl_atr
        self._min_sl_points = min_sl_points

    def _sl_floor(self, atr: float) -> Decimal:
        """Minimum SL distance from entry (price units): max(atr-mult, points)."""

        by_atr = self._min_sl_atr * atr
        return Decimal(str(max(by_atr, self._min_sl_points)))

    @property
    def spec(self) -> SymbolSpec:
        return self._spec

    # --------------------------------------------------------------- initial

    def compute_initial(
        self,
        side: OrderSide,
        entry_price: Decimal,
        bundle: FeatureSnapshotBundle,
        *,
        now: datetime | None = None,
    ) -> StopsAndTPs:
        """Compute the initial SL behind structure + ATR buffer."""

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        atr = _atr_safe(bundle)
        reasoning: list[str] = []
        if side == OrderSide.BUY:
            swing = _last_swing(bundle, "low")
            if swing is not None and atr > 0:
                sl = Decimal(str(swing)) - Decimal(str(self._initial_sl_atr * atr))
                reasoning.append(f"long SL behind M5 swing low {swing} minus {self._initial_sl_atr}×ATR")
            else:
                # Fallback: ATR-only distance from entry.
                sl = entry_price - Decimal(str(self._initial_sl_atr * atr or 0.5))
                reasoning.append("long SL fallback: entry minus 1×ATR (no swing low available)")
        else:
            swing = _last_swing(bundle, "high")
            if swing is not None and atr > 0:
                sl = Decimal(str(swing)) + Decimal(str(self._initial_sl_atr * atr))
                reasoning.append(f"short SL behind M5 swing high {swing} plus {self._initial_sl_atr}×ATR")
            else:
                sl = entry_price + Decimal(str(self._initial_sl_atr * atr or 0.5))
                reasoning.append("short SL fallback: entry plus 1×ATR (no swing high available)")

        if sl <= 0:
            sl = entry_price - Decimal("1.0") if side == OrderSide.BUY else entry_price + Decimal("1.0")
            reasoning.append("SL guard: clamped to entry±1 to avoid zero/negative values")

        # SL FLOOR: a structure stop closer to entry than the floor would explode
        # the lot size (risk / sl_distance). Push it out to at least the floor.
        floor = self._sl_floor(atr)
        if side == OrderSide.BUY:
            max_sl = entry_price - floor  # SL must be at or below this
            if sl > max_sl:
                reasoning.append(
                    f"SL floor: {entry_price - sl} < {floor} → pushed to entry−floor ({max_sl})"
                )
                sl = max_sl
        else:
            min_sl = entry_price + floor  # SL must be at or above this
            if sl < min_sl:
                reasoning.append(
                    f"SL floor: {sl - entry_price} < {floor} → pushed to entry+floor ({min_sl})"
                )
                sl = min_sl

        sl_rounded = _round(sl, self._spec)
        return StopsAndTPs(
            sl_price=sl_rounded,
            trail_active=False,
            trailing_mode=TrailingMode.FIXED,
            reasoning=reasoning,
            timestamp=ts,
        )

    # ---------------------------------------------------------------- break-even

    def move_to_break_even(
        self,
        side: OrderSide,
        entry_price: Decimal,
        current_spread_points: float,
        *,
        now: datetime | None = None,
    ) -> StopsAndTPs:
        """Move the SL to entry + spread + a small commission buffer.

        The trade is now risk-free for the remaining size.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        # Convert the spread/bonus from points to price units.
        spread_price = Decimal(str(current_spread_points)) * self._spec.point
        bonus_price = Decimal(str(self._be_bonus_points)) * self._spec.point
        if side == OrderSide.BUY:
            new_sl = entry_price + spread_price + bonus_price
            direction_word = "above"
        else:
            new_sl = entry_price - spread_price - bonus_price
            direction_word = "below"
        new_sl_rounded = _round(new_sl, self._spec)
        return StopsAndTPs(
            sl_price=new_sl_rounded,
            trail_active=True,
            trailing_mode=TrailingMode.BREAK_EVEN,
            reasoning=[
                f"break-even: SL moved to entry {direction_word} spread ({current_spread_points} pts) "
                f"+ bonus {self._be_bonus_points} pts"
            ],
            timestamp=ts,
        )

    # ------------------------------------------------------------------- trail

    def trail(
        self,
        side: OrderSide,
        current_sl: Decimal,
        entry_price: Decimal,
        bundle: FeatureSnapshotBundle,
        *,
        now: datetime | None = None,
    ) -> StopsAndTPs:
        """Trail the SL behind the latest swing point in the trade's favour.

        Rule
        ----
        * Long trade: new SL = max(current_sl, latest_swing_low + 1×ATR).
          The SL can only ratchet **up** (in the long's favour); it
          never moves back down.
        * Short trade: mirror — new SL = min(current_sl, latest_swing_high - 1×ATR).

        If no fresh swing is available, the SL stays put.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        atr = _atr_safe(bundle)
        reasoning: list[str] = [f"trail: min distance {self._trail_min_atr}×ATR"]

        if side == OrderSide.BUY:
            swing = _last_swing(bundle, "low")
            if swing is None or atr <= 0:
                return StopsAndTPs(
                    sl_price=current_sl,
                    trail_active=True,
                    trailing_mode=TrailingMode.STRUCTURE_TRAIL,
                    reasoning=reasoning + ["no swing low / no ATR — SL unchanged"],
                    timestamp=ts,
                )
            candidate = Decimal(str(swing)) + Decimal(str(self._trail_min_atr * atr))
            new_sl = max(current_sl, candidate)
            if new_sl > current_sl:
                reasoning.append(f"long trail: SL raised from {current_sl} to {new_sl} (swing low {swing})")
            else:
                reasoning.append(f"long trail: candidate {candidate} ≤ current SL {current_sl} — unchanged")
        else:
            swing = _last_swing(bundle, "high")
            if swing is None or atr <= 0:
                return StopsAndTPs(
                    sl_price=current_sl,
                    trail_active=True,
                    trailing_mode=TrailingMode.STRUCTURE_TRAIL,
                    reasoning=reasoning + ["no swing high / no ATR — SL unchanged"],
                    timestamp=ts,
                )
            candidate = Decimal(str(swing)) - Decimal(str(self._trail_min_atr * atr))
            new_sl = min(current_sl, candidate)
            if new_sl < current_sl:
                reasoning.append(f"short trail: SL lowered from {current_sl} to {new_sl} (swing high {swing})")
            else:
                reasoning.append(f"short trail: candidate {candidate} ≥ current SL {current_sl} — unchanged")

        return StopsAndTPs(
            sl_price=_round(new_sl, self._spec),
            trail_active=True,
            trailing_mode=TrailingMode.STRUCTURE_TRAIL,
            reasoning=reasoning,
            timestamp=ts,
        )


__all__ = [
    "DEFAULT_BE_BONUS_POINTS",
    "DEFAULT_INITIAL_SL_ATR",
    "DEFAULT_TRAIL_MIN_ATR",
    "StopManager",
    "TrailingMode",
]
