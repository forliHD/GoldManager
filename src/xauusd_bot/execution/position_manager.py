"""PositionManager — per-bar management of an open position (Block 4 Phase 3).

The entry path (``ExecutionPipeline.process``) opens a position and computes
its SL + TP1/TP2/TP3 plan. This module drives that plan **forward** on every
subsequent bar:

* **TP1 hit** → close ``tp1_pct`` of the original volume + move the SL to
  break-even (entry).
* **TP2 hit** → close ``tp2_pct`` of the original volume.
* **Trailing** (after break-even) → ratchet the SL behind structure using
  :meth:`StopManager.trail` (SL only ever moves in the trade's favour).
* **Runner** → close the remainder when price rejects the TP3 / HTF level
  (:meth:`TakeProfitManager.should_close_runner`).

Design: :meth:`PositionManager.plan` is a **pure function** — it takes the
managed-position state + the current bundle/price and returns a list of
:class:`ManagementAction` plus the updated state. It performs no I/O, so it is
exhaustively unit-testable. The execution-engine applies the actions through
the connector (``order_modify`` for SL, position close for partials) and
persists the updated state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from xauusd_bot.connectors.schemas import OrderSide, SymbolSpec
from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.execution.stops import StopManager
from xauusd_bot.execution.take_profit import TakeProfitManager

log = structlog.get_logger(__name__)


class ManagedPosition(BaseModel):
    """Persisted management state for one open position (keyed by ticket)."""

    model_config = ConfigDict(extra="forbid")

    ticket: str
    side: OrderSide
    entry_price: Decimal
    initial_volume: Decimal
    sl_price: Decimal
    tp1_price: Decimal | None = None
    tp2_price: Decimal | None = None
    tp3_price: Decimal | None = None
    tp1_pct: float = 30.0
    tp2_pct: float = 30.0
    tp1_taken: bool = False
    tp2_taken: bool = False
    breakeven_done: bool = False
    initial_risk: Decimal | None = Field(
        default=None,
        description=(
            "Entry-time risk distance |entry − initial_sl| (price units). Used to gate "
            "structure-trailing on a real ≥R profit buffer (Phase D). None = trail as soon "
            "as TP1 arms it (legacy)."
        ),
    )
    peak: Decimal | None = Field(
        default=None,
        description=(
            "Highest price seen since entry (long) / lowest (short) — the favorable extreme. "
            "Feeds the chandelier trail so the SL ratchets up continuously. None = use entry."
        ),
    )


class ManagementAction(BaseModel):
    """One management instruction the execution-engine should apply."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["partial_close", "modify_sl", "close_all"]
    volume: Decimal | None = Field(default=None, description="Lots to close (partial_close).")
    price: Decimal | None = Field(default=None, description="New SL (modify_sl).")
    reason: str = ""


def _reached(side: OrderSide, current_price: Decimal, level: Decimal) -> bool:
    """True if ``current_price`` has reached the TP ``level`` for ``side``."""
    if side == OrderSide.BUY:
        return current_price >= level
    return current_price <= level


def _tighter(side: OrderSide, new_sl: Decimal, current_sl: Decimal) -> bool:
    """True if ``new_sl`` is strictly in the trade's favour vs ``current_sl``."""
    if side == OrderSide.BUY:
        return new_sl > current_sl
    return new_sl < current_sl


def _round_volume(volume: Decimal, spec: SymbolSpec) -> Decimal:
    """Floor ``volume`` to the symbol's volume step (never round a partial up)."""
    step = spec.volume_step if spec.volume_step and spec.volume_step > 0 else Decimal("0.01")
    steps = (volume / step).to_integral_value(rounding=ROUND_DOWN)
    return (steps * step).quantize(step)


class PositionManager:
    """Pure per-bar position-management planner."""

    def __init__(
        self,
        stop_mgr: StopManager,
        tp_mgr: TakeProfitManager,
        spec: SymbolSpec,
        *,
        breakeven_at_tp1: bool = False,
        trail_activate_r: float = 0.0,
        be_trigger_r: float = 1.0,
        weekend_flat: Callable[[datetime, float], bool] | None = None,
    ) -> None:
        self._stop = stop_mgr
        self._tp = tp_mgr
        self._spec = spec
        # Weekend-flat predicate (ts, broker_offset_min) → bool. When it fires we
        # close the whole position before the weekend gap (Joshua: never hold over
        # a weekend). None = disabled. Overnight holds are unaffected.
        self._weekend_flat = weekend_flat
        # Lever #1 (exit tuning): the old logic snapped the SL to break-even the
        # instant TP1 was hit, choking the 70% runner at entry and capping the
        # average winner near +0.45R while losers ran a full −1R. Default OFF now:
        # at TP1 we only ARM structure-trailing (the SL ratchets behind swings as
        # the runner pushes toward TP3 / the far pool) instead of dead-locking it
        # at entry. Set True to restore the old behaviour.
        self._breakeven_at_tp1 = breakeven_at_tp1
        # Phase D: only start trailing once the trade is ≥ this many R in profit,
        # so an unproven trade is never tightened prematurely. 0 = legacy (trail
        # as soon as TP1 arms it).
        self._trail_activate_r = trail_activate_r
        # Break-even floor trigger (R): once favorable excursion reaches this, the
        # trail enforces a no-loss floor (entry ± cost buffer).
        self._be_trigger_r = be_trigger_r

    def _trail_armed(self, mp: ManagedPosition, current_price: Decimal) -> bool:
        """True once the trade is ≥ ``trail_activate_r`` R in profit (Phase D).

        Needs ``initial_risk`` to measure R; if it is absent or zero we don't
        arm on profit (TP1 still arms trailing via ``breakeven_done``).
        """

        if self._trail_activate_r <= 0:
            return True
        if mp.initial_risk is None or mp.initial_risk <= 0:
            return False
        if mp.side == OrderSide.BUY:
            profit = current_price - mp.entry_price
        else:
            profit = mp.entry_price - current_price
        return profit >= mp.initial_risk * Decimal(str(self._trail_activate_r))

    def _be_floor_armed(self, mp: ManagedPosition) -> bool:
        """True once the favorable excursion (peak) reached ``be_trigger_r`` R.

        Uses the peak (not the current price) so the BE floor stays locked even
        if price pulled back below the trigger after first reaching it.
        """

        if self._be_trigger_r <= 0:
            return mp.tp1_taken  # no R trigger → only TP1 arms the floor
        if mp.initial_risk is None or mp.initial_risk <= 0 or mp.peak is None:
            return mp.tp1_taken
        excursion = (mp.peak - mp.entry_price) if mp.side == OrderSide.BUY else (mp.entry_price - mp.peak)
        return mp.tp1_taken or excursion >= mp.initial_risk * Decimal(str(self._be_trigger_r))

    def plan(
        self,
        mp: ManagedPosition,
        bundle: FeatureSnapshotBundle,
        current_price: Decimal,
        current_spread_points: float = 0.0,
    ) -> tuple[list[ManagementAction], ManagedPosition]:
        """Return the management actions for this bar + the updated state.

        ``current_price`` is the latest close (or bid/ask) used to test TP hits
        and runner rejection. ``current_spread_points`` lets the break-even floor
        cover the spread (defaults to 0 for tests/backtest). The returned state
        must be persisted by the caller.
        """
        actions: list[ManagementAction] = []
        mp = mp.model_copy(deep=True)
        vol_min = self._spec.volume_min if self._spec.volume_min else Decimal("0.01")

        # --- Weekend flat: close the whole position before the weekend gap.
        # Pre-empts all other management — we just want flat. Overnight (weekday)
        # holds are NOT affected (the predicate only fires Fri-late / weekend).
        if self._weekend_flat is not None and self._weekend_flat(
            bundle.ts, getattr(bundle, "broker_offset_minutes", 0.0)
        ):
            return [ManagementAction(kind="close_all", reason="weekend_flat")], mp

        # --- TP1: partial close + move SL to break-even.
        if not mp.tp1_taken and mp.tp1_price is not None and _reached(mp.side, current_price, mp.tp1_price):
            vol = _round_volume(mp.initial_volume * Decimal(str(mp.tp1_pct / 100.0)), self._spec)
            if vol >= vol_min:
                actions.append(ManagementAction(kind="partial_close", volume=vol, reason="tp1_hit"))
            mp.tp1_taken = True
            if self._breakeven_at_tp1 and not mp.breakeven_done and _tighter(mp.side, mp.entry_price, mp.sl_price):
                actions.append(ManagementAction(kind="modify_sl", price=mp.entry_price, reason="breakeven_after_tp1"))
                mp.sl_price = mp.entry_price
            # Arm structure-trailing from TP1 either way (gates the trail block below).
            mp.breakeven_done = True

        # --- TP2: partial close.
        if not mp.tp2_taken and mp.tp2_price is not None and _reached(mp.side, current_price, mp.tp2_price):
            vol = _round_volume(mp.initial_volume * Decimal(str(mp.tp2_pct / 100.0)), self._spec)
            if vol >= vol_min:
                actions.append(ManagementAction(kind="partial_close", volume=vol, reason="tp2_hit"))
            mp.tp2_taken = True

        # Track the favorable extreme (peak) for the chandelier trail.
        if mp.peak is None:
            mp.peak = mp.entry_price
        mp.peak = max(mp.peak, current_price) if mp.side == OrderSide.BUY else min(mp.peak, current_price)

        # --- Trailing SL (ratchet only): break-even floor + structure + chandelier.
        # Arms once TP1 was taken OR the trade has earned a ≥ trail_activate_r
        # buffer, so an unproven trade is never tightened prematurely (Phase D).
        if mp.breakeven_done or self._trail_armed(mp, current_price):
            be_armed = self._be_floor_armed(mp)
            trail = self._stop.trail(
                mp.side, mp.sl_price, mp.entry_price, bundle,
                peak=mp.peak, be_armed=be_armed, spread_points=current_spread_points,
            )
            if trail.sl_price is not None and _tighter(mp.side, trail.sl_price, mp.sl_price):
                actions.append(ManagementAction(kind="modify_sl", price=trail.sl_price, reason="trail"))
                mp.sl_price = trail.sl_price

        # --- Runner: close the remainder on HTF-level rejection.
        if mp.tp3_price is not None:
            should, reason = self._tp.should_close_runner(mp.side, mp.tp3_price, current_price, bundle)
            if should:
                actions.append(ManagementAction(kind="close_all", reason=f"runner_{reason}"))

        return actions, mp


__all__ = ["ManagedPosition", "ManagementAction", "PositionManager"]
