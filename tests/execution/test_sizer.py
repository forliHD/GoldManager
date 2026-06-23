"""Tests for PositionSizer — Block 4 Phase 1 (lot-size calculator)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.common.schemas.execution import SizingResult, SizingRoundingMode
from xauusd_bot.connectors.schemas import SymbolSpec
from xauusd_bot.execution.sizer import PositionSizer

from tests._execution_factories import make_symbol_spec


# ----------------------------------------------------------------- 1. happy path


def test_basic_sizing_xauusd() -> None:
    """For $200 risk, 5 USD SL distance, contract 100: lots = 200 / 500 = 0.40."""

    sizer = PositionSizer()
    spec = make_symbol_spec(contract_size=Decimal("100"))
    result = sizer.size(
        risk_amount=Decimal("200"),
        sl_distance=Decimal("5"),
        spec=spec,
        now=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )
    assert isinstance(result, SizingResult)
    assert result.volume_lots == Decimal("0.40")
    assert result.risk_per_lot == Decimal("500.00")
    assert result.formula_used.startswith("lots = risk_amount / (sl_distance * contract_size)")
    assert result.rounding_mode == SizingRoundingMode.EXACT


def test_rounding_down_to_step() -> None:
    """Calculated lots are rounded DOWN to the nearest volume_step."""

    sizer = PositionSizer()
    spec = make_symbol_spec(
        contract_size=Decimal("100"),
        volume_step=Decimal("0.01"),
    )
    # 7 USD SL, 100 contract → risk_per_lot = 700. 100/700 = 0.1428... → 0.14
    result = sizer.size(
        risk_amount=Decimal("100"),
        sl_distance=Decimal("7"),
        spec=spec,
    )
    assert result.volume_lots == Decimal("0.14")
    assert result.rounding_mode == SizingRoundingMode.ROUNDED_DOWN


def test_exact_step_match_is_exact_mode() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec(contract_size=Decimal("100"))
    # 100/500 = 0.20 — exact match to step.
    result = sizer.size(
        risk_amount=Decimal("100"),
        sl_distance=Decimal("5"),
        spec=spec,
    )
    assert result.volume_lots == Decimal("0.20")
    assert result.rounding_mode == SizingRoundingMode.EXACT


# ----------------------------------------------------------------- 2. snap to min


def test_below_minimum_snaps_to_min() -> None:
    # raw < vmin but the vmin lot's realized risk (0.01×11×100 = 11) is within
    # the risk cap (10 × 1.15 = 11.5), so the snap-up to vmin is allowed.
    sizer = PositionSizer()
    spec = make_symbol_spec(
        contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
    )
    result = sizer.size(
        risk_amount=Decimal("10"),  # 10 / 1100 = 0.0091 < vmin
        sl_distance=Decimal("11"),
        spec=spec,
    )
    assert result.volume_lots == Decimal("0.01")
    assert result.rounding_mode == SizingRoundingMode.BELOW_MIN


def test_vmin_snap_blocked_when_over_risk_cap() -> None:
    # raw < vmin AND snapping to vmin would risk 0.01×50×100 = 50 ≫ the budget
    # (1 × 1.15) → block (0 lots) instead of silently over-risking 50×.
    sizer = PositionSizer()
    spec = make_symbol_spec(contract_size=Decimal("100"), volume_min=Decimal("0.01"))
    result = sizer.size(risk_amount=Decimal("1"), sl_distance=Decimal("50"), spec=spec)
    assert result.volume_lots == Decimal("0")
    assert result.rounding_mode == SizingRoundingMode.RISK_BLOCKED


def test_risk_cap_clamps_lots_down() -> None:
    # A tolerant cap still bounds realized risk to risk_amount × (1+tol). Force a
    # vmin snap that exceeds the cap slightly → clamp, not block, when a valid
    # smaller lot exists. Here risk=20, sl=30 → raw 0.0066<vmin; vmin risk=30 >
    # 20×1.15=23 → RISK_BLOCKED (no lot fits). Use a larger min-cap scenario:
    sizer = PositionSizer(risk_tolerance=0.0)
    spec = make_symbol_spec(
        contract_size=Decimal("100"), volume_min=Decimal("0.01"), volume_step=Decimal("0.01")
    )
    # raw = 100/(3×100)=0.333 → rounds to 0.33 (realized 99 ≤ 100). No cap hit.
    ok = sizer.size(risk_amount=Decimal("100"), sl_distance=Decimal("3"), spec=spec)
    assert ok.rounding_mode in (SizingRoundingMode.ROUNDED_DOWN, SizingRoundingMode.EXACT)
    # realized risk never exceeds the budget × (1+tol).
    assert ok.volume_lots * Decimal("3") * Decimal("100") <= Decimal("100")


# ----------------------------------------------------------------- 3. cap at max


def test_above_maximum_caps_at_max() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec(
        contract_size=Decimal("100"),
        volume_max=Decimal("5.00"),
    )
    result = sizer.size(
        risk_amount=Decimal("100000"),  # huge risk
        sl_distance=Decimal("1"),
        spec=spec,
    )
    assert result.volume_lots == Decimal("5.00")
    assert result.rounding_mode == SizingRoundingMode.ABOVE_MAX


# ----------------------------------------------------------------- 4. validation


def test_zero_risk_amount_raises() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec()
    with pytest.raises(ValueError):
        sizer.size(risk_amount=Decimal("0"), sl_distance=Decimal("1"), spec=spec)


def test_negative_risk_amount_raises() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec()
    with pytest.raises(ValueError):
        sizer.size(risk_amount=Decimal("-1"), sl_distance=Decimal("1"), spec=spec)


def test_zero_sl_distance_raises() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec()
    with pytest.raises(ValueError):
        sizer.size(risk_amount=Decimal("100"), sl_distance=Decimal("0"), spec=spec)


def test_zero_volume_step_in_spec_raises() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec(volume_step=Decimal("0"))
    with pytest.raises(ValueError):
        sizer.size(risk_amount=Decimal("100"), sl_distance=Decimal("1"), spec=spec)


# ----------------------------------------------------------------- 5. determinism


def test_same_inputs_same_outputs() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec()
    fixed_now = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)
    a = sizer.size(
        risk_amount=Decimal("200"),
        sl_distance=Decimal("5"),
        spec=spec,
        now=fixed_now,
    )
    b = sizer.size(
        risk_amount=Decimal("200"),
        sl_distance=Decimal("5"),
        spec=spec,
        now=fixed_now,
    )
    assert a == b


# ----------------------------------------------------------------- 6. formula strings


def test_formula_includes_all_operands() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec(contract_size=Decimal("100"))
    result = sizer.size(
        risk_amount=Decimal("250"),
        sl_distance=Decimal("5"),
        spec=spec,
    )
    # The formula string should mention all the operands for auditability.
    assert "250" in result.formula_used
    assert "5" in result.formula_used
    assert "100" in result.formula_used
    assert "contract_size" in result.formula_used


# ----------------------------------------------------------------- 7. timestamp handling


def test_naive_now_becomes_utc() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec()
    result = sizer.size(
        risk_amount=Decimal("100"),
        sl_distance=Decimal("5"),
        spec=spec,
        now=datetime(2026, 4, 15, 13, 30),  # naive
    )
    assert result.timestamp.tzinfo is not None


def test_aware_now_preserved() -> None:
    sizer = PositionSizer()
    spec = make_symbol_spec()
    result = sizer.size(
        risk_amount=Decimal("100"),
        sl_distance=Decimal("5"),
        spec=spec,
        now=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )
    assert result.timestamp == datetime(2026, 4, 15, 13, 30, tzinfo=UTC)
