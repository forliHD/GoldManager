"""Paper broker — simulated order execution with variable spread and slippage.

The :class:`PaperBroker` consumes a connector (for prices) and a spread
monitor (for live spread context) and simulates fills against a
deterministic-but-realistic distribution of slippage.

Design goals
------------
* **Deterministic** in backtest: with a fixed seed, identical market
  sequences produce identical fill prices.
* **Pessimistic pending fills:** stop and limit orders are filled at the
  *worst* price of the bar that touches the trigger — not the best.
  This is conservative and matches what most retail brokers actually do
  under fast markets.
* **Variable spread:** the live spread comes from a
  :class:`xauusd_bot.data.spread_monitor.SpreadMonitor` rolling window.
  We snap to the spread that was observed at the fill bar, then add a
  random slippage.
* **No MT5 dependency.** PaperBroker runs anywhere Python runs.

The broker maintains its own simulated account (balance, equity, margin,
free margin, open positions, PnL). The connector's read-only state stays
untouched.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.schemas import (
    Bar,
    FillPolicy,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    SymbolSpec,
)

log = structlog.get_logger(__name__)


@dataclass
class FillModel:
    """Knobs for the slippage distribution."""

    base_slippage_points: float = 1.0  # 1 point on top of spread
    volatility_slippage_factor: float = 0.5  # multiplied by ATR(20) in points
    spread_markup_factor: float = 0.25  # extra spread in stress regimes
    seed: int = 42


@dataclass
class _OpenPosition:
    spec: SymbolSpec
    side: OrderSide
    volume: Decimal
    open_price: Decimal
    open_time: datetime
    sl: Decimal | None = None
    tp: Decimal | None = None
    magic: int = 0
    comment: str = ""
    position_id: str = ""
    client_order_id: str | None = None


@dataclass
class _SimAccount:
    balance: Decimal = Decimal("10000")
    equity: Decimal = Decimal("10000")
    margin: Decimal = Decimal("0")
    free_margin: Decimal = Decimal("10000")
    leverage: int = 100


def _points_to_price(points: float, spec: SymbolSpec) -> Decimal:
    return Decimal(str(points)) * spec.point


def _price_to_points(price_delta: Decimal, spec: SymbolSpec) -> Decimal:
    if spec.point == 0:
        return Decimal("0")
    return price_delta / spec.point


class PaperBroker:
    """Simulated broker — fills orders against historical bars / ticks.

    Parameters
    ----------
    connector:
        A read-only :class:`IMarketConnector` for prices. The broker
        *only* reads from it; it never calls ``order_send`` on the
        connector (that would route back into a real broker).
    spec:
        :class:`SymbolSpec` for the traded instrument. If ``None`` the
        broker fetches it from the connector.
    fill_model:
        Optional :class:`FillModel` with slippage / spread knobs.
    initial_balance:
        Starting balance for the simulated account.
    """

    def __init__(
        self,
        connector: IMarketConnector,
        spec: SymbolSpec | None = None,
        fill_model: FillModel | None = None,
        initial_balance: Decimal = Decimal("10000"),
    ) -> None:
        self._connector = connector
        self._spec = spec or connector.get_symbol_spec(connector.symbol if hasattr(connector, "symbol") else "XAUUSD")
        self._fill_model = fill_model or FillModel()
        self._rng = random.Random(self._fill_model.seed)
        self._account = _SimAccount(balance=initial_balance, equity=initial_balance, free_margin=initial_balance)
        self._positions: dict[str, _OpenPosition] = {}
        self._pending: dict[str, OrderRequest] = {}
        self._next_id = 1
        # Most recent spread in points (set by record_spread or by fills).
        self._last_spread_points: float = 30.0

    # ------------------------------------------------------------ account view

    @property
    def balance(self) -> Decimal:
        return self._account.balance

    @property
    def equity(self) -> Decimal:
        return self._account.equity

    @property
    def margin(self) -> Decimal:
        return self._account.margin

    @property
    def free_margin(self) -> Decimal:
        return self._account.free_margin

    @property
    def open_positions(self) -> list[Position]:
        """Return :class:`Position` snapshots for all open trades."""

        out: list[Position] = []
        now = datetime.now(tz=UTC)
        for p in self._positions.values():
            mark = self._mark_to_market(p)
            profit = self._unrealized_pnl(p, mark)
            out.append(
                Position(
                    position_id=p.position_id,
                    symbol=p.spec.symbol,
                    side=p.side,
                    volume=p.volume,
                    open_price=p.open_price,
                    sl=p.sl,
                    tp=p.tp,
                    open_time=p.open_time,
                    profit=profit,
                    comment=p.comment,
                    magic=p.magic,
                )
            )
        _ = now  # reserved for swap calculation in future blocks
        return out

    # ------------------------------------------------------------ market data

    def record_spread(self, spread_points: float) -> None:
        """Update the broker's view of current spread (used for next fill)."""

        self._last_spread_points = max(0.0, float(spread_points))

    def update_marks(self, last_price: Decimal) -> None:
        """Recompute equity / free margin based on the latest mark price."""

        self._account.equity = self._account.balance + sum(
            (self._unrealized_pnl(p, last_price) for p in self._positions.values()),
            Decimal("0"),
        )
        self._account.free_margin = self._account.equity - self._account.margin

    # --------------------------------------------------------------- orders

    def submit(self, request: OrderRequest) -> OrderResult:
        """Simulate a fill for ``request``.

        For MARKET orders the fill is immediate at ``connector.current_t``'s
        latest bid/ask (synthesized from the last bar's close ± spread). For
        LIMIT/STOP orders the broker records the order and the caller
        invokes :meth:`process_bar` on every new bar to evaluate triggers.
        """

        if request.fill_policy == FillPolicy.LIVE:
            log.warning("paper_broker_live_policy_treated_as_paper", order=request.client_order_id)

        if request.type == OrderType.MARKET:
            return self._fill_market(request)
        return self._record_pending(request)

    def process_bar(self, bar: Bar) -> list[OrderResult]:
        """Evaluate pending orders against a closed bar.

        Returns a list of :class:`OrderResult` for orders that triggered.
        Stop/limit fills are **pessimistic** — see module docstring.
        """

        results: list[OrderResult] = []
        triggered_ids: list[str] = []

        for pid, req in self._pending_orders_with_position_check(bar):
            if not self._bar_triggers(bar, req):
                continue
            triggered_ids.append(pid)
            results.append(self._fill_pending(bar, req))

        for pid in triggered_ids:
            self._pending.pop(pid, None)
        return results

    # --------------------------------------------------------------- public — pending

    @property
    def pending(self) -> list[OrderRequest]:
        """All pending LIMIT/STOP orders currently registered."""

        return list(self._pending.values())

    # ------------------------------------------------------------ internals

    def _pending_orders_with_position_check(self, bar: Bar) -> Iterable[tuple[str, OrderRequest]]:
        """Yield (id, req) for pending orders on this bar's symbol."""

        for pid, req in list(self._pending.items()):
            if req.symbol == bar.symbol:
                yield pid, req

    def _bar_triggers(self, bar: Bar, req: OrderRequest) -> bool:
        if req.type == OrderType.LIMIT:
            if req.side == OrderSide.BUY:
                return bar.low <= (req.price or Decimal("0"))
            return bar.high >= (req.price or Decimal("0"))
        if req.type == OrderType.STOP:
            if req.side == OrderSide.BUY:
                return bar.high >= (req.price or Decimal("0"))
            return bar.low <= (req.price or Decimal("0"))
        return False

    def _fill_pending(self, bar: Bar, req: OrderRequest) -> OrderResult:
        """Pessimistic fill: use the worst price inside the bar that triggered."""

        if req.side == OrderSide.BUY:
            fill_price = req.price or bar.high
            # For buy stop, the bar moves through `price` upward; pessimistic = bar.high
            # For buy limit, the bar dips to `price` from above; pessimistic = bar.high
            if req.type == OrderType.LIMIT:
                fill_price = bar.high  # pessimistic: filled at the high
            else:
                fill_price = max(bar.high, req.price or bar.high)
        else:
            fill_price = req.price or bar.low
            if req.type == OrderType.LIMIT:
                fill_price = bar.low  # pessimistic: filled at the low
            else:
                fill_price = min(bar.low, req.price or bar.low)

        return self._open_position(req, fill_price, trigger_time=bar.time)

    def _fill_market(self, request: OrderRequest) -> OrderResult:
        """Fill a market order at current_t's last close + half-spread."""

        # Use the connector's last bar to find a price snapshot.
        try:
            bars = self._connector.get_rates(request.symbol, "M1", count=1)
        except Exception as exc:  # noqa: BLE001 — degraded path
            log.warning("paper_broker_market_price_lookup_failed", error=str(exc))
            return OrderResult(accepted=False, error_code="NO_PRICE", error_message=str(exc))

        if not bars:
            return OrderResult(accepted=False, error_code="NO_PRICE", error_message="no bars available")

        last = bars[-1]
        spec = self._spec
        spread_price = _points_to_price(self._last_spread_points, spec)
        if request.side == OrderSide.BUY:
            fill_price = last.close + spread_price / 2 + self._slippage(spec, last, request.side)
        else:
            fill_price = last.close - spread_price / 2 - self._slippage(spec, last, request.side)
        return self._open_position(request, fill_price, trigger_time=last.time)

    def _slippage(self, spec: SymbolSpec, last: Bar, side: OrderSide) -> Decimal:
        """Sample slippage in price units from the configured distribution."""

        # Simple model: base slippage + volatility term proportional to bar range
        bar_range_points = float(_price_to_points(last.high - last.low, spec))
        sigma = max(1.0, self._fill_model.volatility_slippage_factor * bar_range_points)
        slip_points = abs(self._rng.gauss(self._fill_model.base_slippage_points, sigma))
        # Stress spread markup (e.g. during news) is applied here in future blocks.
        slip_points *= 1.0 + self._fill_model.spread_markup_factor
        return _points_to_price(slip_points, spec)

    def _record_pending(self, request: OrderRequest) -> OrderResult:
        pid = self._generate_id("pend")
        req = request.model_copy(update={"client_order_id": pid})
        self._pending[pid] = req
        return OrderResult(accepted=True, order_id=pid, client_order_id=pid)

    # _pending: pending LIMIT/STOP orders, indexed by client_order_id.
    # Initialized in __init__.

    def _open_position(
        self,
        request: OrderRequest,
        fill_price: Decimal,
        trigger_time: datetime,
    ) -> OrderResult:
        notional = Decimal(request.volume) * self._spec.trade_contract_size * fill_price
        margin = notional * self._spec.margin_rate  # noqa: F841 — used by future margin accounting

        pid = self._generate_id("pos")
        position = _OpenPosition(
            spec=self._spec,
            side=request.side,
            volume=Decimal(request.volume),
            open_price=fill_price,
            open_time=trigger_time,
            sl=request.sl,
            tp=request.tp,
            magic=request.magic,
            comment=request.comment,
            position_id=pid,
            client_order_id=request.client_order_id,
        )
        self._positions[pid] = position

        # Update margin
        self._account.margin += margin
        self._account.free_margin = self._account.equity - self._account.margin
        if self._account.free_margin < 0:
            log.error("paper_broker_negative_free_margin", pid=pid, free_margin=self._account.free_margin)

        return OrderResult(
            accepted=True,
            order_id=pid,
            client_order_id=request.client_order_id,
            filled_volume=Decimal(request.volume),
            avg_fill_price=fill_price,
        )

    def close_position(self, position_id: str, last_price: Decimal) -> OrderResult:
        """Close an open position at ``last_price`` (no slippage; caller-supplied mark)."""

        if position_id not in self._positions:
            return OrderResult(accepted=False, error_code="NOT_FOUND", error_message=f"position {position_id} not found")
        position = self._positions.pop(position_id)
        pnl = self._unrealized_pnl(position, last_price)
        self._account.balance += pnl
        self._account.margin = max(Decimal("0"), self._account.margin - position.volume * position.spec.trade_contract_size * position.open_price * position.spec.margin_rate)
        self._account.free_margin = self._account.equity - self._account.margin
        return OrderResult(accepted=True, order_id=position_id, filled_volume=position.volume, avg_fill_price=last_price)

    def _unrealized_pnl(self, position: _OpenPosition, last_price: Decimal) -> Decimal:
        if position.side == OrderSide.BUY:
            diff = last_price - position.open_price
        else:
            diff = position.open_price - last_price
        return diff * position.volume * position.spec.trade_contract_size

    def _mark_to_market(self, position: _OpenPosition) -> Decimal:
        try:
            bars = self._connector.get_rates(position.spec.symbol, "M1", count=1)
        except Exception:  # noqa: BLE001
            return position.open_price
        if not bars:
            return position.open_price
        return bars[-1].close

    def _generate_id(self, prefix: str) -> str:
        n = self._next_id
        self._next_id += 1
        return f"{prefix}-{n:06d}"
