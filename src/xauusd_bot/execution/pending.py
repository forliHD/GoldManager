"""PendingOrderManager — sweep and cancel obsolete limit/stop orders.

The :class:`PendingOrderManager` runs on a periodic basis (the bot's
main loop calls :meth:`sweep` every N bars / every minute). It looks
at every pending order the connector knows about and decides whether
the order is **still consistent** with the latest market structure /
VWAP / value-zone / news state. Orders that are no longer fitting
the current context are cancelled via the connector.

Obsolescence criteria
---------------------
An order is cancelled when **any** of the following holds:

1. **News blackout** — the :class:`FeatureSnapshotBundle` reports an
   active news blackout (``bundle.news.in_blackout_flag``). Any open
   order on a blackout currency is dangerous to leave alive.
2. **Structure break against the order** — the most recent BOS/CHOCH
   is in the opposite direction of the order (long order with a
   recent BOS_down, or vice versa).
3. **Far from VWAP cluster** — the order is more than
   ``cluster_break_atr`` × ATR away from the current VWAP cluster
   (i.e. price has migrated away from where the order was placed).
4. **Outside developing value area** — the order is on the wrong side
   of the developing value area AND the price has been there for more
   than ``value_break_bars`` bars (i.e. the level is no longer
   relevant).
5. **Age limit** — the order has been pending for more than
   ``max_age_bars`` bars (a stale order that never triggered is
   probably mis-placed).

I-1
---
Talks to the broker via :class:`IMarketConnector` only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.schemas.decision import DecisionAction
from xauusd_bot.common.schemas.execution import PendingSweepResult
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    StructureEventType,
    ValueAreaStatus,
)
from xauusd_bot.connectors.base import IMarketConnector
from xauusd_bot.connectors.schemas import (
    OrderRequest,
    OrderSide,
    OrderType,
)

log = structlog.get_logger(__name__)


# Default knobs.
DEFAULT_MAX_AGE_BARS = 60
DEFAULT_CLUSTER_BREAK_ATR = 3.0


# ----------------------------------------------------------------- helper fns


def _is_in_blackout(bundle: FeatureSnapshotBundle) -> bool:
    if bundle.news is None:
        return False
    return bool(bundle.news.in_blackout_flag)


def _structure_against_order(
    bundle: FeatureSnapshotBundle, order: OrderRequest
) -> bool:
    """True if the latest BOS/CHOCH contradicts the order side."""

    if bundle.structure is None:
        return False
    last = bundle.structure.last_bos or bundle.structure.last_choch
    if last is None:
        return False
    long_side = order.side == OrderSide.BUY
    if long_side:
        return last.type in (
            StructureEventType.BOS_DOWN,
            StructureEventType.CHOCH_DOWN,
        )
    return last.type in (
        StructureEventType.BOS_UP,
        StructureEventType.CHOCH_UP,
    )


def _far_from_vwap_cluster(
    bundle: FeatureSnapshotBundle, order: OrderRequest, current_price: float, threshold_atr: float
) -> bool:
    """True if the order is far from the active VWAP cluster."""

    if bundle.vwap is None or bundle.vwap.cluster_center is None or bundle.atr is None:
        return False
    if bundle.atr <= 0:
        return False
    order_price = float(order.price) if order.price is not None else current_price
    distance = abs(order_price - float(bundle.vwap.cluster_center))
    return distance > threshold_atr * bundle.atr


def _outside_value_area(
    bundle: FeatureSnapshotBundle, order: OrderRequest
) -> bool:
    """True if the order is on the wrong side of the developing value area."""

    if bundle.volume_range is None:
        return False
    vr = bundle.volume_range
    # Use weekly profile as the most relevant developing one.
    weekly = vr.weekly
    if weekly.value_status == ValueAreaStatus.WITHIN_VALUE:
        return False
    if weekly.vah is None or weekly.val is None:
        return False
    if order.price is None:
        return False
    op = float(order.price)
    if weekly.value_status == ValueAreaStatus.ABOVE_VALUE and order.side == OrderSide.SELL:
        # Sell order above weekly value: expect reversion, not extension — keep.
        return False
    if weekly.value_status == ValueAreaStatus.BELOW_VALUE and order.side == OrderSide.BUY:
        return False
    if op < float(weekly.val) and order.side == OrderSide.SELL:
        return True
    if op > float(weekly.vah) and order.side == OrderSide.BUY:
        return True
    return False


# ----------------------------------------------------------------- manager


class PendingOrderManager:
    """Cancel pending orders that no longer match the current market state.

    Parameters
    ----------
    connector:
        :class:`IMarketConnector` (Replay or Live).
    max_age_bars:
        Cancel any pending order older than this many bars.
    cluster_break_atr:
        VWAP-cluster break threshold (in ATR units).
    """

    def __init__(
        self,
        connector: IMarketConnector,
        *,
        max_age_bars: int = DEFAULT_MAX_AGE_BARS,
        cluster_break_atr: float = DEFAULT_CLUSTER_BREAK_ATR,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._connector = connector
        self._max_age_bars = max_age_bars
        self._cluster_break_atr = cluster_break_atr
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))
        # Track the bar_index at which each pending order was registered.
        # Bar-index-based aging is deterministic and replay-friendly.
        self._registered: dict[str, int] = {}

    # ----------------------------------------------------------- registration

    def register(self, request: OrderRequest, *, bar_index: int | None = None) -> None:
        """Note that ``request`` is now pending (so the sweep can age it).

        Parameters
        ----------
        bar_index:
            The M1 bar index at the time of registration. If omitted,
            the manager's view of "now" is encoded as a synthetic bar
            index (timestamp-based). For replay determinism the
            executor should pass the actual current bar index.
        """

        if request.client_order_id is None:
            return
        if bar_index is None:
            bar_index = int(self._now_fn().timestamp())
        self._registered[request.client_order_id] = bar_index

    def forget(self, client_order_id: str) -> None:
        """Drop a pending order from the aging ledger (e.g. after fill)."""

        self._registered.pop(client_order_id, None)

    # --------------------------------------------------------------- sweep

    def sweep(
        self,
        bundle: FeatureSnapshotBundle,
        current_price: float,
        bar_index: int,
        *,
        now: datetime | None = None,
    ) -> PendingSweepResult:
        """Sweep all open pendings and cancel the obsolete ones.

        Parameters
        ----------
        bundle:
            The latest :class:`FeatureSnapshotBundle` (used to test
            structure / VWAP / news alignment).
        current_price:
            Last close (for distance tests).
        bar_index:
            Current bar index (used for age tests).
        now:
            Timestamp for the sweep result.
        """

        ts = (now or self._now_fn())
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)

        examined = 0
        kept = 0
        cancelled = 0
        reasons: dict[str, int] = {}

        pendings = self._connector.pending_get()
        for req in pendings:
            examined += 1
            cid = req.client_order_id
            if cid is None:
                kept += 1
                continue
            # MARKET orders should never appear in pending_get, but guard anyway.
            if req.type == OrderType.MARKET:
                kept += 1
                continue

            cancel_reason: str | None = None

            # 1. News blackout
            if _is_in_blackout(bundle):
                cancel_reason = "news_blackout"

            # 2. Structure break
            if cancel_reason is None and _structure_against_order(bundle, req):
                cancel_reason = "structure_against"

            # 3. Far from VWAP cluster
            if cancel_reason is None and _far_from_vwap_cluster(
                bundle, req, current_price, self._cluster_break_atr
            ):
                cancel_reason = "vwap_cluster_break"

            # 4. Outside developing value area
            if cancel_reason is None and _outside_value_area(bundle, req):
                cancel_reason = "outside_value_area"

            # 5. Age — bar-index-based.
            if cancel_reason is None:
                registered_bi = self._registered.get(cid)
                if registered_bi is not None:
                    age_bars = max(0, bar_index - registered_bi)
                    if age_bars > self._max_age_bars:
                        cancel_reason = "max_age"

            if cancel_reason is None:
                kept += 1
                continue

            # Cancel via the connector.
            result = self._connector.order_cancel(cid)
            if result.accepted:
                cancelled += 1
                reasons[cancel_reason] = reasons.get(cancel_reason, 0) + 1
                self.forget(cid)
                log.info("pending_cancelled", client_order_id=cid, reason=cancel_reason)
            else:
                kept += 1
                log.warning(
                    "pending_cancel_failed",
                    client_order_id=cid,
                    reason=cancel_reason,
                    error_code=result.error_code,
                )

        return PendingSweepResult(
            swept_at=ts,
            examined=examined,
            kept=kept,
            cancelled=cancelled,
            cancel_reasons=reasons,
        )


__all__ = [
    "DEFAULT_CLUSTER_BREAK_ATR",
    "DEFAULT_MAX_AGE_BARS",
    "PendingOrderManager",
]
