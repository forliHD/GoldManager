"""PositionSizer — deterministic lot-size calculator (Block 4 Phase 1).

The :class:`PositionSizer` converts a :class:`RiskVerdict` (which carries
the USD amount we're willing to lose) and a Stop-loss distance (in
price units) into a lot size that respects the symbol's
:attr:`~xauusd_bot.connectors.schemas.SymbolSpec.volume_min` /
``volume_max`` / ``volume_step`` constraints.

Formula
-------
For XAUUSD CFDs the risk per 1.0 lot is:

    risk_per_lot_usd = sl_distance_usd × contract_size

    lots = risk_amount_usd / risk_per_lot_usd

Where ``sl_distance_usd`` is the distance between entry and SL in
price units (USD per troy ounce for XAUUSD) and ``contract_size`` is
the number of ounces per standard lot (typically 100 oz).

Edge cases
----------
* ``lots < volume_min`` → snap up to ``volume_min`` and tag
  ``SizingRoundingMode.BELOW_MIN`` (the trade is over-sized for the
  risk budget; the executor must reject it via the risk verdict if
  the user has configured that).
* ``lots > volume_max`` → cap at ``volume_max`` and tag
  ``SizingRoundingMode.ABOVE_MAX`` (the risk budget exceeds the
  exchange's max position size).
* otherwise round **down** to the nearest ``volume_step`` and tag
  ``SizingRoundingMode.ROUNDED_DOWN``. (Never round up — rounding up
  would silently increase the trade's risk above the budget.)

Determinism
-----------
No RNG, no LLM, no time-based behaviour. Identical inputs produce
identical outputs across processes / platforms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.schemas.execution import (
    SizingResult,
    SizingRoundingMode,
)
from xauusd_bot.connectors.schemas import SymbolSpec

log = structlog.get_logger(__name__)


class PositionSizer:
    """Deterministic lot-size calculator.

    Parameters
    ----------
    default_contract_size:
        Fallback contract size when the supplied :class:`SymbolSpec`
        doesn't carry one (defensive default; the spec always does for
        a real broker, so this is essentially a test-time safety net).
    """

    def __init__(
        self,
        default_contract_size: Decimal = Decimal("100"),
        *,
        risk_tolerance: float = 0.15,
    ) -> None:
        self._default_contract_size = Decimal(default_contract_size)
        # Hard max-risk cap: realized risk may not exceed risk_amount × (1 + tol).
        self._risk_tolerance = Decimal(str(risk_tolerance))

    # ---------------------------------------------------------------- size

    def size(
        self,
        risk_amount: Decimal,
        sl_distance: Decimal,
        spec: SymbolSpec,
        *,
        now: datetime | None = None,
    ) -> SizingResult:
        """Compute the lot size for ``risk_amount`` USD at ``sl_distance``.

        Parameters
        ----------
        risk_amount:
            The amount of account currency the trade is allowed to
            lose if the SL is hit. **Must be > 0**.
        sl_distance:
            The price-unit distance between entry and SL. **Must be
            > 0** (a zero-distance SL would imply infinite lots).
        spec:
            :class:`SymbolSpec` for the traded instrument.
        now:
            Timestamp for the result. Defaults to ``datetime.now(UTC)``.
        """

        ts = (now or datetime.now(tz=UTC))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        if risk_amount <= 0:
            raise ValueError(f"risk_amount must be > 0, got {risk_amount}")
        if sl_distance <= 0:
            raise ValueError(f"sl_distance must be > 0, got {sl_distance}")

        contract_size = spec.trade_contract_size or self._default_contract_size
        if contract_size <= 0:
            raise ValueError(f"contract_size must be > 0, got {contract_size}")

        risk_per_lot = (sl_distance * contract_size).quantize(Decimal("0.01"))
        raw_lots = (risk_amount / risk_per_lot)

        # Apply the lot-step / min / max constraints.
        rounded, mode = self._apply_constraints(raw_lots, spec)

        # HARD MAX-RISK CAP (backstop independent of the SL floor). The realized
        # risk of `rounded` lots is rounded × risk_per_lot. If that exceeds the
        # budget × (1 + tolerance) — e.g. because volume_min snapped a tiny size
        # UP, or sl_distance was tighter than expected — clamp lots DOWN to the
        # cap (rounded to the step). If even volume_min breaches the cap, block
        # (0 lots) rather than silently over-risk.
        max_risk = (risk_amount * (Decimal("1") + self._risk_tolerance)).quantize(Decimal("0.01"))
        if rounded > 0 and (rounded * risk_per_lot) > max_risk:
            step = Decimal(spec.volume_step)
            capped = ((max_risk / risk_per_lot) / step).to_integral_value(
                rounding=ROUND_DOWN
            ) * step
            if capped < Decimal(spec.volume_min):
                rounded, mode = Decimal("0"), SizingRoundingMode.RISK_BLOCKED
            else:
                rounded, mode = capped, SizingRoundingMode.RISK_CAPPED

        formula = (
            f"lots = risk_amount / (sl_distance * contract_size) "
            f"= {risk_amount} / ({sl_distance} * {contract_size}) "
            f"= {risk_amount} / {risk_per_lot} = {raw_lots}"
        )

        result = SizingResult(
            volume_lots=rounded,
            risk_per_lot=risk_per_lot,
            formula_used=formula,
            rounding_mode=mode,
            sl_distance=sl_distance,
            risk_amount=risk_amount,
            timestamp=ts,
        )
        log.debug(
            "position_sized",
            raw_lots=str(raw_lots),
            final_lots=str(rounded),
            mode=mode.value,
            risk_per_lot=str(risk_per_lot),
        )
        return result

    # ------------------------------------------------------------- internals

    @staticmethod
    def _apply_constraints(raw: Decimal, spec: SymbolSpec) -> tuple[Decimal, SizingRoundingMode]:
        """Round / snap ``raw`` to the symbol's lot step + min + max.

        Rounding rule
        -------------
        * ``raw < volume_min`` → snap to ``volume_min`` (BELOW_MIN).
        * ``raw > volume_max`` → cap at ``volume_max`` (ABOVE_MAX).
        * else round down to the nearest ``volume_step`` (ROUNDED_DOWN
          unless the result is already a multiple of step → EXACT).
        """

        vmin = Decimal(spec.volume_min)
        vmax = Decimal(spec.volume_max)
        step = Decimal(spec.volume_step)
        if step <= 0:
            raise ValueError(f"volume_step must be > 0, got {step}")

        if raw < vmin:
            return vmin, SizingRoundingMode.BELOW_MIN
        if raw > vmax:
            return vmax, SizingRoundingMode.ABOVE_MAX

        # Round DOWN to the nearest step.
        n = (raw / step).to_integral_value(rounding=ROUND_DOWN)
        rounded = n * step
        if rounded == raw:
            return rounded, SizingRoundingMode.EXACT
        return rounded, SizingRoundingMode.ROUNDED_DOWN


__all__ = ["PositionSizer"]
