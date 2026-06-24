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

import re
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.schemas.execution import StopsAndTPs, TrailingMode
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.schemas import OrderSide, SymbolSpec

log = structlog.get_logger(__name__)

# Gold-plausible price band for extracting an SL level from the LLM's free-form
# invalidation strings. Filters out fib ratios (0.382), R-multiples, pip counts,
# etc. — only a real XAUUSD price (~hundreds to tens-of-thousands) qualifies.
_PRICE_RE = re.compile(r"\d{3,6}(?:[.,]\d+)?")
_PRICE_MIN = 500.0
_PRICE_MAX = 99_999.0


def parse_sl_from_invalidations(
    invalidations: list[str], side: OrderSide, entry_price: float
) -> float | None:
    """Extract an SL-level hint from the LLM's invalidation strings.

    The LLM emits "trade is dead if X" lines like ``"H1-Close unter 4179.4"``.
    We pull every gold-plausible price out of those strings, keep only the ones
    on the correct side of entry (below for a long, above for a short) and
    return the **nearest** such level — the first place the setup invalidates.
    The deterministic SL floor + max-risk cap still bound it downstream, so a
    too-tight or too-wide AI level can never produce an unsafe stop.

    Returns ``None`` when no plausible level is found (executor uses the
    structure SL).
    """

    candidates: list[float] = []
    for line in invalidations:
        for m in _PRICE_RE.findall(str(line)):
            try:
                val = float(m.replace(",", "."))
            except ValueError:
                continue
            if not (_PRICE_MIN <= val <= _PRICE_MAX):
                continue
            on_correct_side = (side == OrderSide.BUY and val < entry_price) or (
                side == OrderSide.SELL and val > entry_price
            )
            if on_correct_side:
                candidates.append(val)
    if not candidates:
        return None
    # Nearest to entry = the operative invalidation level.
    if side == OrderSide.BUY:
        return max(candidates)
    return min(candidates)


# Multipliers — the canonical numbers from 05_execution_risk.md.
# Lever #2 (exit tuning): the initial SL buffer behind structure was 1.0×ATR,
# which made the stop wide → a full stop = a large −R. Tightened to 0.5×ATR so
# the risk per trade shrinks (better R:R); backtested against 1.0 before live.
DEFAULT_INITIAL_SL_ATR = 0.5
DEFAULT_TRAIL_MIN_ATR = 1.0
# Phase D (let winners run): the trail now sits BEHIND the protective swing
# (long: swing_low − buffer×ATR), not above it, so a pullback to structure does
# not stop the runner out near break-even. Default 0.5×ATR of room.
DEFAULT_TRAIL_BUFFER_ATR = 0.5
# Chandelier ratchet: once the runner is armed, the SL also rides this many ATR
# below the highest-high-since-entry (long) so it ratchets up CONTINUOUSLY as
# the trade extends — not only when a new structural swing prints. Combined
# with the break-even floor this is what lets a winner run to its max while
# locking progressively more in. 0 = chandelier off (structure-trail only).
DEFAULT_CHANDELIER_ATR = 3.0
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
        trail_buffer_atr: float = DEFAULT_TRAIL_BUFFER_ATR,
        chandelier_atr: float = DEFAULT_CHANDELIER_ATR,
        be_bonus_points: float = DEFAULT_BE_BONUS_POINTS,
        min_sl_atr: float = DEFAULT_MIN_SL_ATR,
        min_sl_points: float = DEFAULT_MIN_SL_POINTS,
    ) -> None:
        self._spec = spec
        self._initial_sl_atr = initial_sl_atr
        self._trail_min_atr = trail_min_atr
        # Room BEHIND the swing for the trailing SL (Phase D). Kept separate from
        # trail_min_atr (legacy) so the direction flip is explicit and tunable.
        self._trail_buffer_atr = trail_buffer_atr
        self._chandelier_atr = chandelier_atr
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
        sl_hint: Decimal | None = None,
    ) -> StopsAndTPs:
        """Compute the initial SL behind structure + ATR buffer.

        ``sl_hint`` (Phase C) is the LLM's invalidation level. When set and on
        the correct side of entry, it REPLACES the structure swing as the
        anchor (the SL is placed an ATR buffer beyond it, exactly like a swing).
        The SL floor below still enforces a minimum distance, so an AI level
        that is too tight can never explode the lot size — I-4 holds.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        atr = _atr_safe(bundle)
        reasoning: list[str] = []
        # AI invalidation level (if usable) takes precedence over the structure
        # swing as the SL anchor; otherwise fall back to the most-recent swing.
        hint_ok = (
            sl_hint is not None
            and (
                (side == OrderSide.BUY and sl_hint < entry_price)
                or (side == OrderSide.SELL and sl_hint > entry_price)
            )
        )
        if side == OrderSide.BUY:
            swing = float(sl_hint) if hint_ok else _last_swing(bundle, "low")
            anchor = "AI invalidation" if hint_ok else "M5 swing low"
            if swing is not None and atr > 0:
                sl = Decimal(str(swing)) - Decimal(str(self._initial_sl_atr * atr))
                reasoning.append(f"long SL behind {anchor} {swing} minus {self._initial_sl_atr}×ATR")
            else:
                # Fallback: ATR-only distance from entry.
                sl = entry_price - Decimal(str(self._initial_sl_atr * atr or 0.5))
                reasoning.append("long SL fallback: entry minus 1×ATR (no swing low available)")
        else:
            swing = float(sl_hint) if hint_ok else _last_swing(bundle, "high")
            anchor = "AI invalidation" if hint_ok else "M5 swing high"
            if swing is not None and atr > 0:
                sl = Decimal(str(swing)) + Decimal(str(self._initial_sl_atr * atr))
                reasoning.append(f"short SL behind {anchor} {swing} plus {self._initial_sl_atr}×ATR")
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
        peak: Decimal | None = None,
        be_armed: bool = False,
    ) -> StopsAndTPs:
        """Ratchet the SL up using three protective anchors (Phase D, ratchet-only).

        The new SL is the most-protective (highest for a long / lowest for a
        short) of:

        * **Break-even floor** (``be_armed``): once the trade has proven itself
          (≥ its BE trigger), the SL never sits worse than entry ± a small cost
          buffer — so a trade that touched profit can no longer become a loss.
        * **Structure trail**: a buffer BEHIND the latest protective swing
          (``swing_low − buffer×ATR`` for a long) — room for a pullback to
          structure without stopping the runner.
        * **Chandelier**: ``peak − chandelier_atr×ATR`` (``peak`` = the highest
          high since entry for a long) — ratchets the SL up CONTINUOUSLY as the
          trade extends, not only when a new swing prints, so a runner rides to
          its maximum with progressively more locked in.

        The SL only ever ratchets in the trade's favour (``max`` for a long,
        ``min`` for a short); it never moves back. With no anchors available it
        stays put.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        atr = _atr_safe(bundle)
        is_long = side == OrderSide.BUY
        reasoning: list[str] = []
        # Collect protective candidates; the ratchet picks the tightest.
        candidates: list[Decimal] = [current_sl]

        if be_armed:
            bonus = Decimal(str(self._be_bonus_points)) * self._spec.point
            be = entry_price + bonus if is_long else entry_price - bonus
            candidates.append(be)
            reasoning.append(f"break-even floor {be}")

        swing = _last_swing(bundle, "low" if is_long else "high")
        if swing is not None and atr > 0:
            buf = Decimal(str(self._trail_buffer_atr * atr))
            struct = Decimal(str(swing)) - buf if is_long else Decimal(str(swing)) + buf
            candidates.append(struct)
            reasoning.append(f"structure {struct} ({self._trail_buffer_atr}×ATR behind swing {swing})")

        if peak is not None and atr > 0 and self._chandelier_atr > 0:
            dist = Decimal(str(self._chandelier_atr * atr))
            chand = peak - dist if is_long else peak + dist
            candidates.append(chand)
            reasoning.append(f"chandelier {chand} ({self._chandelier_atr}×ATR from peak {peak})")

        new_sl = max(candidates) if is_long else min(candidates)
        moved = new_sl > current_sl if is_long else new_sl < current_sl
        reasoning.insert(0, f"trail: SL {'raised' if moved else 'unchanged'} {current_sl}→{new_sl}")

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
    "parse_sl_from_invalidations",
]
