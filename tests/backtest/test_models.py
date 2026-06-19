"""Tests for the slippage + spread models (Block 5b Phase 0).

The models are deterministic pure functions of (bar, spec). Tests
assert:

* Happy paths: the documented value comes out for the documented input.
* Negatives / non-monotonic inputs are rejected at construction time
  (no silent fallthrough to 0).
* ``ChainedSlippage`` / ``NewsAwareSpread`` correctly compose their
  inner models.
* The output is always a non-negative :class:`Decimal` (slippage /
  spread can't be negative).

Run with::

    PYTHONPATH=. .venv/bin/pytest -q tests/backtest/test_models.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.backtest.models import (
    ChainedSlippage,
    FixedSlippage,
    FixedSpread,
    NewsAwareSpread,
    SlippageModel,
    SpreadModel,
    VolatilitySlippage,
    VolatilitySpread,
    expected_slippage_estimate,
    std_bar,
)
from xauusd_bot.connectors.schemas import Bar, SymbolSpec


# ----------------------------------------------------------------- helpers


def _xauusd_spec() -> SymbolSpec:
    """Construct a minimal XAUUSD SymbolSpec for tests."""

    return SymbolSpec(
        symbol="XAUUSD",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )


def _wide_bar(range_points: float = 50.0) -> Bar:
    """A typical M1 bar with a known range (in points)."""

    spec = _xauusd_spec()
    rng = Decimal(str(range_points)) * spec.point
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime(2026, 4, 15, 13, 0, tzinfo=UTC),
        open=Decimal("2375.00"),
        high=Decimal("2375.00") + rng,
        low=Decimal("2375.00"),
        close=Decimal("2375.00"),
        tick_volume=100,
    )


# ============================================================== FixedSlippage


class TestFixedSlippage:
    def test_returns_constant_price_for_any_bar(self) -> None:
        m = FixedSlippage(Decimal("0.50"))
        spec = _xauusd_spec()
        for range_points in (10.0, 50.0, 200.0):
            assert m.compute(_wide_bar(range_points=range_points), spec) == Decimal("0.50")

    def test_zero_slippage_is_legal(self) -> None:
        m = FixedSlippage(Decimal("0"))
        assert m.compute(_wide_bar(), _xauusd_spec()) == Decimal("0")

    def test_negative_slippage_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"price must be >= 0"):
            FixedSlippage(Decimal("-0.10"))

    def test_name_is_class_name(self) -> None:
        assert FixedSlippage(Decimal("0.50")).name == "FixedSlippage"

    def test_is_slippage_model_subclass(self) -> None:
        assert isinstance(FixedSlippage(Decimal("0.50")), SlippageModel)


# ============================================================== VolatilitySlippage


class TestVolatilitySlippage:
    def test_wider_bar_yields_more_slippage(self) -> None:
        spec = _xauusd_spec()
        m = VolatilitySlippage(base_points=1.0, factor=0.10)
        narrow = m.compute(_wide_bar(range_points=10.0), spec)
        wide = m.compute(_wide_bar(range_points=200.0), spec)
        assert wide > narrow

    def test_base_points_applied_when_bar_range_is_zero(self) -> None:
        spec = _xauusd_spec()
        m = VolatilitySlippage(base_points=5.0, factor=0.0)
        bar = _wide_bar(range_points=0.0)
        out = m.compute(bar, spec)
        # 5 points * 0.01 = 0.05 USD
        assert out == Decimal("0.05")

    def test_negative_base_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"base_points must be >= 0"):
            VolatilitySlippage(base_points=-1.0, factor=0.1)

    def test_negative_factor_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"factor must be >= 0"):
            VolatilitySlippage(base_points=1.0, factor=-0.1)

    def test_zero_factor_and_base_yields_zero(self) -> None:
        spec = _xauusd_spec()
        m = VolatilitySlippage(base_points=0.0, factor=0.0)
        assert m.compute(_wide_bar(range_points=100.0), spec) == Decimal("0")

    def test_zero_point_spec_returns_zero(self) -> None:
        """If the spec's point is zero, the model can't compute and returns 0."""

        spec = SymbolSpec(
            symbol="WEIRD",
            point=Decimal("0"),
            digits=2,
            trade_contract_size=Decimal("100"),
            volume_min=Decimal("0.01"),
            volume_max=Decimal("100"),
            volume_step=Decimal("0.01"),
        )
        m = VolatilitySlippage(base_points=1.0, factor=0.1)
        assert m.compute(_wide_bar(), spec) == Decimal("0")


# ============================================================== ChainedSlippage


class TestChainedSlippage:
    def test_sums_both_components(self) -> None:
        spec = _xauusd_spec()
        bar = _wide_bar(range_points=100.0)
        a = FixedSlippage(Decimal("0.10"))
        b = FixedSlippage(Decimal("0.20"))
        chained = ChainedSlippage(a, b)
        assert chained.compute(bar, spec) == Decimal("0.30")

    def test_name_reflects_components(self) -> None:
        chained = ChainedSlippage(FixedSlippage(Decimal("0.10")), FixedSlippage(Decimal("0.20")))
        assert "Chained" in chained.name
        assert "FixedSlippage" in chained.name

    def test_rejects_non_slippage_components(self) -> None:
        with pytest.raises(TypeError, match=r"requires two SlippageModel instances"):
            ChainedSlippage("not a model", FixedSlippage(Decimal("0.20")))  # type: ignore[arg-type]

    def test_chained_with_volatility_slippage(self) -> None:
        spec = _xauusd_spec()
        bar = _wide_bar(range_points=100.0)
        chained = ChainedSlippage(
            FixedSlippage(Decimal("0.10")),
            VolatilitySlippage(base_points=1.0, factor=0.10),
        )
        out = chained.compute(bar, spec)
        # 0.10 USD + (1 + 0.10*100) points * 0.01 = 0.10 + 0.11 = 0.21 USD
        assert out == Decimal("0.21")


# ============================================================== FixedSpread


class TestFixedSpread:
    def test_returns_constant_price_for_any_bar(self) -> None:
        m = FixedSpread(Decimal("0.30"))
        spec = _xauusd_spec()
        for r in (10.0, 50.0, 200.0):
            assert m.compute(_wide_bar(range_points=r), spec) == Decimal("0.30")

    def test_zero_spread_legal(self) -> None:
        m = FixedSpread(Decimal("0"))
        assert m.compute(_wide_bar(), _xauusd_spec()) == Decimal("0")

    def test_negative_spread_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"price must be >= 0"):
            FixedSpread(Decimal("-0.10"))

    def test_in_news_blackout_returns_same_value(self) -> None:
        """FixedSpread ignores the blackout flag (no news logic)."""

        m = FixedSpread(Decimal("0.30"))
        spec = _xauusd_spec()
        assert m.compute(_wide_bar(), spec, in_news_blackout=True) == Decimal("0.30")

    def test_is_spread_model_subclass(self) -> None:
        assert isinstance(FixedSpread(Decimal("0.30")), SpreadModel)


# ============================================================== VolatilitySpread


class TestVolatilitySpread:
    def test_wider_bar_yields_wider_spread(self) -> None:
        spec = _xauusd_spec()
        m = VolatilitySpread(base_points=30.0, factor=0.10)
        narrow = m.compute(_wide_bar(range_points=10.0), spec)
        wide = m.compute(_wide_bar(range_points=200.0), spec)
        assert wide > narrow

    def test_zero_factor_yields_base(self) -> None:
        spec = _xauusd_spec()
        m = VolatilitySpread(base_points=30.0, factor=0.0)
        assert m.compute(_wide_bar(range_points=100.0), spec) == Decimal("0.30")

    def test_zero_point_spec_returns_zero(self) -> None:
        spec = SymbolSpec(
            symbol="WEIRD",
            point=Decimal("0"),
            digits=2,
            trade_contract_size=Decimal("100"),
            volume_min=Decimal("0.01"),
            volume_max=Decimal("100"),
            volume_step=Decimal("0.01"),
        )
        m = VolatilitySpread(base_points=30.0, factor=0.1)
        assert m.compute(_wide_bar(), spec) == Decimal("0")

    def test_negative_base_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"base_points must be >= 0"):
            VolatilitySpread(base_points=-1.0, factor=0.1)

    def test_negative_factor_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"factor must be >= 0"):
            VolatilitySpread(base_points=1.0, factor=-0.1)


# ============================================================== NewsAwareSpread


class TestNewsAwareSpread:
    def test_blackout_multiplies_base(self) -> None:
        spec = _xauusd_spec()
        base = FixedSpread(Decimal("0.30"))
        m = NewsAwareSpread(base=base, news_multiplier=2.0)
        assert m.compute(_wide_bar(), spec, in_news_blackout=False) == Decimal("0.30")
        assert m.compute(_wide_bar(), spec, in_news_blackout=True) == Decimal("0.60")

    def test_three_x_multiplier(self) -> None:
        spec = _xauusd_spec()
        m = NewsAwareSpread(base=FixedSpread(Decimal("0.20")), news_multiplier=3.0)
        assert m.compute(_wide_bar(), spec, in_news_blackout=True) == Decimal("0.60")

    def test_rejects_non_spread_base(self) -> None:
        with pytest.raises(TypeError, match=r"must be a SpreadModel"):
            NewsAwareSpread(base="not a model", news_multiplier=2.0)  # type: ignore[arg-type]

    def test_rejects_multiplier_below_one(self) -> None:
        with pytest.raises(ValueError, match=r"news_multiplier must be >= 1.0"):
            NewsAwareSpread(base=FixedSpread(Decimal("0.30")), news_multiplier=0.5)

    def test_name_reflects_components(self) -> None:
        m = NewsAwareSpread(base=FixedSpread(Decimal("0.30")), news_multiplier=2.0)
        assert "NewsAware" in m.name
        assert "FixedSpread" in m.name
        assert "2" in m.name

    def test_composes_with_volatility_spread(self) -> None:
        spec = _xauusd_spec()
        base = VolatilitySpread(base_points=30.0, factor=0.10)
        m = NewsAwareSpread(base=base, news_multiplier=2.0)
        # 100-point bar -> 30 + 0.10*100 = 40 points = 0.40 USD
        normal = m.compute(_wide_bar(range_points=100.0), spec, in_news_blackout=False)
        blackout = m.compute(_wide_bar(range_points=100.0), spec, in_news_blackout=True)
        assert normal == Decimal("0.40")
        assert blackout == Decimal("0.80")


# ============================================================== helpers


class TestHelpers:
    def test_expected_slippage_estimate_uses_typical_bar(self) -> None:
        """The helper gives a positive value for any reasonable model."""

        out = expected_slippage_estimate(FixedSlippage(Decimal("0.50")))
        assert out == 0.50

    def test_std_bar_builds_a_usable_bar(self) -> None:
        bar = std_bar(range_points=100.0)
        assert bar.symbol == "XAUUSD"
        assert bar.timeframe == "M1"
        # High - Low = 100 points = 1.00 USD on a 0.01-point XAUUSD.
        assert bar.high - bar.low == Decimal("1.00")

    def test_protocol_subclass_check(self) -> None:
        """Concrete classes satisfy the Protocol structural check."""

        assert isinstance(FixedSlippage(Decimal("0")), SlippageModel)
        assert isinstance(VolatilitySlippage(), SlippageModel)
        assert isinstance(ChainedSlippage(FixedSlippage(Decimal("0")), FixedSlippage(Decimal("0"))), SlippageModel)
        assert isinstance(FixedSpread(Decimal("0")), SpreadModel)
        assert isinstance(VolatilitySpread(), SpreadModel)
        assert isinstance(NewsAwareSpread(base=FixedSpread(Decimal("0"))), SpreadModel)
