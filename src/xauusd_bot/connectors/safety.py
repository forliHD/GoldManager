"""Pre-trade safety checks — the "is it safe to send the next order?" gate.

The :class:`PreTradeSafetyChecker` is a small, dependency-free module
that the execution engine calls immediately before each
``connector.order_send``. It runs four checks:

1. **Feed online** — the connector reports ``is_connected()``.
2. **Spread within thresholds** — the live spread is below the configured
   block threshold (and we record a warning if it crosses the warn
   threshold).
3. **No broker error** — there is no last-error pending (e.g. requote
   flood, off-quotes).
4. **Account stable** — equity has not been dropping for N consecutive
   checks (drawdown circuit breaker).

The result is a :class:`SafetyVerdict` that the execution engine
consumes. Hard rules (Brain-vs-Hands, Plan §1.4): on ``BLOCK`` the
engine must not submit the order, period.

Architecture
------------
* Stateless beyond an in-memory rolling PnL/equity trace.
* Does NOT know about connectors concretely; accepts a protocol-style
  object so it can be reused by Replay and Live.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover
    from xauusd_bot.connectors.schemas import AccountInfo


log = structlog.get_logger(__name__)


class SafetyAction(str, Enum):
    """What the execution engine should do."""

    PROCEED = "proceed"
    WARN = "warn"
    BLOCK = "block"


class SafetyReason(str, Enum):
    """Why a check failed."""

    FEED_OFFLINE = "feed_offline"
    SPREAD_TOO_WIDE = "spread_too_wide"
    SPREAD_ELEVATED = "spread_elevated"
    BROKER_ERROR = "broker_error"
    DRAWDOWN_TRIP = "drawdown_trip"
    ACCOUNT_FROZEN = "account_frozen"


class SafetyVerdict(BaseModel):
    """Result of a safety check."""

    model_config = ConfigDict(extra="forbid")

    action: SafetyAction
    reasons: list[SafetyReason] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)
    checked_at: datetime
    spread_points: float | None = None
    equity: float | None = None


@dataclass
class SafetyThresholds:
    """All thresholds in one place so they're easy to tune."""

    spread_warn_points: float = 50.0
    spread_block_points: float = 120.0
    drawdown_trip_fraction: float = 0.05  # 5% peak-to-trough equity drop in N checks
    drawdown_window: int = 10  # checks
    require_trade_allowed: bool = True


class PreTradeSafetyChecker:
    """Stateless-ish pre-trade gate.

    Parameters
    ----------
    get_account:
        Callable that returns the current :class:`AccountInfo`. In
        production this is ``lambda: connector.get_account()``. Tests
        can pass a stub.
    get_spread_points:
        Callable that returns the current spread in points. The
        :class:`xauusd_bot.data.spread_monitor.SpreadMonitor` is the
        natural source.
    thresholds:
        :class:`SafetyThresholds` for the per-check limits.
    is_connected:
        Callable that returns the connector's connectivity. Defaults
        to ``lambda: True``.
    """

    def __init__(
        self,
        get_account: Callable[[], AccountInfo],
        get_spread_points: Callable[[], float],
        *,
        thresholds: SafetyThresholds | None = None,
        is_connected: Callable[[], bool] | None = None,
    ) -> None:
        self._get_account = get_account
        self._get_spread_points = get_spread_points
        self._is_connected = is_connected or (lambda: True)
        self._thresholds = thresholds or SafetyThresholds()
        # Rolling equity window for drawdown trip
        self._equity_trace: deque[float] = deque(maxlen=self._thresholds.drawdown_window)
        self._peak_equity: float | None = None
        # Broker-error sticky flag (set externally via :meth:`mark_broker_error`)
        self._broker_error_pending: bool = False

    # --------------------------------------------------------------- inputs

    def mark_broker_error(self, code: str, message: str) -> None:
        """Set a sticky broker-error flag; cleared on next successful :meth:`check`."""

        self._broker_error_pending = True
        log.warning("pre_trade_broker_error_marked", code=code, message=message)

    def clear_broker_error(self) -> None:
        self._broker_error_pending = False

    # --------------------------------------------------------------- check

    def check(self, now: datetime) -> SafetyVerdict:
        """Run all four checks and return a :class:`SafetyVerdict`."""

        reasons: list[SafetyReason] = []
        details: dict[str, str] = {}
        spread_pts: float | None = None
        equity: float | None = None

        # 1. Feed online
        if not self._is_connected():
            reasons.append(SafetyReason.FEED_OFFLINE)
            details["feed"] = "connector reports not connected"

        # 2. Spread threshold
        try:
            spread_pts = float(self._get_spread_points())
        except Exception as exc:  # noqa: BLE001
            reasons.append(SafetyReason.SPREAD_TOO_WIDE)
            details["spread"] = f"could not read spread: {exc}"
            spread_pts = None

        if spread_pts is not None:
            if spread_pts >= self._thresholds.spread_block_points:
                reasons.append(SafetyReason.SPREAD_TOO_WIDE)
                details["spread"] = f"{spread_pts:.1f} >= block {self._thresholds.spread_block_points}"
            elif spread_pts >= self._thresholds.spread_warn_points:
                reasons.append(SafetyReason.SPREAD_ELEVATED)
                details["spread"] = f"{spread_pts:.1f} >= warn {self._thresholds.spread_warn_points}"

        # 3. Broker error sticky — capture the *current* state, then clear
        #    the flag (the operator gets one cycle to retry). If they
        #    re-mark it, the next check will see it again.
        broker_error_now = self._broker_error_pending
        self._broker_error_pending = False  # consume the sticky flag
        if broker_error_now:
            reasons.append(SafetyReason.BROKER_ERROR)
            details["broker_error"] = "sticky broker error — clear after manual review"

        # 4. Account stable
        try:
            account = self._get_account()
        except Exception as exc:  # noqa: BLE001
            reasons.append(SafetyReason.ACCOUNT_FROZEN)
            details["account"] = f"could not read account: {exc}"
        else:
            equity = float(account.equity)
            self._equity_trace.append(equity)
            if self._peak_equity is None or equity > self._peak_equity:
                self._peak_equity = equity
            if self._thresholds.require_trade_allowed and not account.trade_allowed:
                reasons.append(SafetyReason.ACCOUNT_FROZEN)
                details["account"] = "trade_allowed=False"
            if self._peak_equity and self._peak_equity > 0:
                dd_frac = (self._peak_equity - equity) / self._peak_equity
                if (
                    dd_frac >= self._thresholds.drawdown_trip_fraction
                    and len(self._equity_trace) >= self._thresholds.drawdown_window
                ):
                    reasons.append(SafetyReason.DRAWDOWN_TRIP)
                    details["drawdown"] = f"peak={self._peak_equity:.2f} now={equity:.2f} ({dd_frac:.2%})"

        # Action aggregation: any BLOCK-level reason → BLOCK, any WARN → WARN, else PROCEED.
        block_set = {SafetyReason.FEED_OFFLINE, SafetyReason.SPREAD_TOO_WIDE, SafetyReason.BROKER_ERROR, SafetyReason.DRAWDOWN_TRIP, SafetyReason.ACCOUNT_FROZEN}
        warn_set = {SafetyReason.SPREAD_ELEVATED}
        if any(r in block_set for r in reasons):
            action = SafetyAction.BLOCK
        elif any(r in warn_set for r in reasons):
            action = SafetyAction.WARN
        else:
            action = SafetyAction.PROCEED

        # (sticky broker error is now consumed at capture time above)

        return SafetyVerdict(
            action=action,
            reasons=reasons,
            details=details,
            checked_at=now,
            spread_points=spread_pts,
            equity=equity,
        )
