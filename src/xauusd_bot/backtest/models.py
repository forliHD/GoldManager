"""Slippage & spread models for the BacktestEngine (Block 5b).

The BacktestEngine wraps every order fill with a slippage and spread
model so the simulated PnL is closer to a real broker's. These are
**deterministic-but-realistic** — a fixed seed gives identical
results across runs. They are *intentionally* not in
``xauusd_bot.connectors.paper_broker``: the PaperBroker handles the
in-loop bar-by-bar live execution, the BacktestEngine orchestrates
the historical replay and owns the wider time-window models.

Design contract
---------------
* **Deterministic** — same inputs + same seed → same output. The
  BacktestEngine documents the seed in the result's ``tags`` dict.
* **No MT5 dependency** — these models only know about Bar / Tick
  data. They never import ``MetaTrader5`` (verified by
  ``tests/backtest/test_invariants.py``).
* **Composable** — a SlippageModel can be a FixedSlippage (0.5 USD)
  OR a VolatilitySlippage (proportional to ATR) OR a chained sum.
  Same for SpreadModel. The BacktestEngine receives a single
  ``SlippageModel`` and a single ``SpreadModel`` instance.

PIT guarantee
-------------
Slippage and spread look at the *closing bar* of the decision bar
plus, for VolatilitySlippage, the bundle's ATR. They never read
*future* bars. The BacktestEngine passes ``current_t`` explicitly so
the model can sanity-check the contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog

from xauusd_bot.connectors.schemas import Bar, SymbolSpec

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------- helpers


def _points_to_price(points: float, spec: SymbolSpec) -> Decimal:
    """Convert a points value to a price-unit :class:`Decimal`."""

    return Decimal(str(points)) * spec.point


# ----------------------------------------------------------------- Slippage


class SlippageModel:
    """Protocol for a slippage model. Implementations are deterministic.

    A slippage model takes a closed bar + the symbol spec and
    returns a non-negative price-unit slippage that will be ADDED to
    the requested fill price (in absolute terms) on both buy and
    sell sides. The BacktestEngine is responsible for actually
    applying the model to the fill price — the model itself is
    pure.
    """

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
    ) -> Decimal:
        """Return the slippage to add to the fill price (>= 0)."""

        raise NotImplementedError

    @property
    def name(self) -> str:
        """Stable name (used in BacktestResult.tags)."""

        return type(self).__name__


@dataclass(frozen=True)
class FixedSlippage(SlippageModel):
    """Constant slippage in price units. The simplest model.

    Parameters
    ----------
    price:
        The slippage in price units (e.g. ``Decimal("0.50")`` for
        XAUUSD means $0.50 / oz — i.e. 5 points on a 0.01-point
        instrument). Must be non-negative.
    """

    price: Decimal = Decimal("0.50")

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError(f"FixedSlippage.price must be >= 0, got {self.price}")

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
    ) -> Decimal:
        return self.price


@dataclass(frozen=True)
class VolatilitySlippage(SlippageModel):
    """Slippage that scales with the bar's range (a volatility proxy).

    ``slippage = base + factor × bar_range_points``
    where ``bar_range_points = (bar.high - bar.low) / spec.point``.
    The factor is interpreted in *points per point-of-range* — a
    factor of 0.10 on a 100-point bar adds 10 points of slippage.

    This model is widely used in retail backtest frameworks because
    it captures the intuition that **wider bars = worse fills**.
    """

    base_points: float = 1.0
    factor: float = 0.10

    def __post_init__(self) -> None:
        if self.base_points < 0:
            raise ValueError(f"VolatilitySlippage.base_points must be >= 0, got {self.base_points}")
        if self.factor < 0:
            raise ValueError(f"VolatilitySlippage.factor must be >= 0, got {self.factor}")

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
    ) -> Decimal:
        if spec.point <= 0:
            return Decimal("0")
        bar_range = float(bar.high - bar.low)
        if bar_range < 0:
            bar_range = 0.0
        bar_range_points = bar_range / float(spec.point)
        slip_points = self.base_points + self.factor * bar_range_points
        return _points_to_price(max(0.0, slip_points), spec)


@dataclass(frozen=True)
class ChainedSlippage(SlippageModel):
    """Sum of two slippage models (e.g. base + volatility)."""

    primary: SlippageModel
    secondary: SlippageModel

    def __post_init__(self) -> None:
        if not isinstance(self.primary, SlippageModel) or not isinstance(self.secondary, SlippageModel):
            raise TypeError(
                f"ChainedSlippage requires two SlippageModel instances, got {type(self.primary).__name__} + {type(self.secondary).__name__}"
            )

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
    ) -> Decimal:
        return self.primary.compute(bar, spec, current_t=current_t) + self.secondary.compute(
            bar, spec, current_t=current_t
        )

    @property
    def name(self) -> str:
        return f"Chained[{self.primary.name}+{self.secondary.name}]"


# ----------------------------------------------------------------- Spread


class SpreadModel:
    """Protocol for a spread model. Implementations are deterministic.

    A spread model returns the bid-ask spread (in price units) to
    apply on top of the close price for a fill. The BacktestEngine
    uses half the spread on each side of the fill (buy = close + spread/2,
    sell = close - spread/2).
    """

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
        in_news_blackout: bool = False,
    ) -> Decimal:
        """Return the spread in price units (>= 0)."""

        raise NotImplementedError

    @property
    def name(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class FixedSpread(SpreadModel):
    """Constant bid-ask spread (e.g. 30 points = 0.30 USD on XAUUSD)."""

    price: Decimal = Decimal("0.30")

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError(f"FixedSpread.price must be >= 0, got {self.price}")

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
        in_news_blackout: bool = False,
    ) -> Decimal:
        return self.price


@dataclass(frozen=True)
class VolatilitySpread(SpreadModel):
    """Spread that scales with bar range.

    ``spread = base + factor × bar_range_points``
    """

    base_points: float = 30.0
    factor: float = 0.10

    def __post_init__(self) -> None:
        if self.base_points < 0:
            raise ValueError(f"VolatilitySpread.base_points must be >= 0, got {self.base_points}")
        if self.factor < 0:
            raise ValueError(f"VolatilitySpread.factor must be >= 0, got {self.factor}")

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
        in_news_blackout: bool = False,
    ) -> Decimal:
        if spec.point <= 0:
            return Decimal("0")
        bar_range_points = float(bar.high - bar.low) / float(spec.point)
        if bar_range_points < 0:
            bar_range_points = 0.0
        spread_points = self.base_points + self.factor * bar_range_points
        return _points_to_price(max(0.0, spread_points), spec)


@dataclass(frozen=True)
class NewsAwareSpread(SpreadModel):
    """Wraps another spread model and boosts the spread during news blackout.

    The boost is a multiplier on the wrapped model's output — by
    default 2.0× during a blackout (a conservative news-spread
    penalty for XAUUSD). The default base is
    :class:`FixedSpread(0.30)` for XAUUSD.
    """

    base: SpreadModel
    news_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if not isinstance(self.base, SpreadModel):
            raise TypeError(f"NewsAwareSpread.base must be a SpreadModel, got {type(self.base).__name__}")
        if self.news_multiplier < 1.0:
            raise ValueError(
                f"NewsAwareSpread.news_multiplier must be >= 1.0 (no penalty < 1.0), got {self.news_multiplier}"
            )

    def compute(
        self,
        bar: Bar,
        spec: SymbolSpec,
        *,
        current_t: datetime | None = None,
        in_news_blackout: bool = False,
    ) -> Decimal:
        spread = self.base.compute(bar, spec, current_t=current_t, in_news_blackout=in_news_blackout)
        if in_news_blackout:
            spread = spread * Decimal(str(self.news_multiplier))
        return spread

    @property
    def name(self) -> str:
        return f"NewsAware[{self.base.name}×{self.news_multiplier:g}]"


# ----------------------------------------------------------------- helpers


def expected_slippage_estimate(
    model: SlippageModel,
    *,
    typical_bar_range_points: float = 50.0,
    spec: SymbolSpec | None = None,
) -> float:
    """Estimate the mean slippage in price units for a typical bar.

    Useful for sanity-checks in tests ("does this VolatilitySlippage
    model add a reasonable amount?") and for the BacktestEngine
    documentation dump.
    """

    spec = spec or SymbolSpec(
        symbol="XAUUSD",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )
    # Synthesize a "typical" bar to feed the model.
    typical = Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=datetime.now(tz=__import__("datetime").UTC),
        open=Decimal("2375.00"),
        high=Decimal("2375.00") + (Decimal(str(typical_bar_range_points)) * spec.point),
        low=Decimal("2375.00"),
        close=Decimal("2375.00"),
        tick_volume=100,
    )
    return float(model.compute(typical, spec))


def std_bar(symbol: str = "XAUUSD", range_points: float = 50.0) -> Bar:
    """Construct a typical M1 bar for use in tests / samples.

    Not in production code paths — only consumed by the
    ``expected_slippage_estimate`` helper and by tests.
    """

    from datetime import UTC, datetime

    spec = SymbolSpec(
        symbol=symbol,
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
    )
    rng = Decimal(str(range_points)) * spec.point
    return Bar(
        symbol=symbol,
        timeframe="M1",
        time=datetime.now(tz=UTC),
        open=Decimal("2375.00"),
        high=Decimal("2375.00") + rng,
        low=Decimal("2375.00"),
        close=Decimal("2375.00"),
        tick_volume=100,
    )


# ----------------------------------------------------------------- re-exports


__all__ = [
    "ChainedSlippage",
    "FixedSlippage",
    "FixedSpread",
    "NewsAwareSpread",
    "SlippageModel",
    "SpreadModel",
    "VolatilitySlippage",
    "VolatilitySpread",
    "expected_slippage_estimate",
    "std_bar",
    # references for type checkers
    "Decimal",
    "math",
]
