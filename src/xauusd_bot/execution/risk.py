"""RiskManager — the **safety-authoritative** approval gate for new trades.

Block 4 Phase 0. The RiskManager sits between the decision layer's
:class:`TradeQualification` and the executor's :class:`OrderManager`.
Its job is to enforce all *pre-trade* safety rules and translate the
decision's :class:`EntryType` into a concrete risk amount in USD.

What the RiskManager enforces
-----------------------------
1. **Score-band → risk fraction**

   * Scout  (65-74) → 0.5 % of equity
   * Reduced (75-84) → 1.0 % of equity
   * Full   (≥85)   → 2.0 % of equity

2. **Hard limits (each independently testable):**

   * Daily  loss ≥ ``Settings.risk_max_daily``  → block + pause for the day
   * Weekly loss ≥ ``Settings.risk_max_weekly`` → block + pause for the week
   * Open positions ≥ ``Settings.risk_max_open_positions`` → block
   * Trades today    ≥ ``Settings.risk_max_trades_per_session`` → block
   * Long+Short both open (no hedge setup in Block 3) → block

3. **Authority:** RiskManager **always** wins over the decision layer.
   If the decision says ``enter_long`` and the RiskManager says
   ``blocked`` (any reason), the order is NOT submitted.

4. **Pause integration:** when a daily / weekly limit is hit, the
   manager activates an :class:`EmergencyStopManager` pause for the
   rest of the day / week. The pause is exposed via :meth:`pause_active`
   so the executor's main loop can short-circuit.

State
-----
The manager maintains the running day / week PnL in memory. The
``record_pnl()`` method is called by the executor after each fill
(BLOCK-4-only API). TimescaleDB persistence of the PnL trail comes
in Block 5 (Journal). For now the in-memory state is enough for the
replay + lifecycle smoke.

I-1 (connector isolation)
-------------------------
The RiskManager reads :class:`AccountInfo` and the open-position list
from the connector via the protocol. It does **not** import
``MetaTrader5``. Verifier greps ``src/xauusd_bot/execution/`` and
should find zero matches.

I-4 (brain vs hands)
--------------------
This is the layer that *does* compute risk (in USD). The decision
layer (Block 3) has nothing of the kind. The split is documented in
``AGENTS.md`` §3 I-4.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    EntryType,
    ScoreBand,
    TradeQualification,
)
from xauusd_bot.common.schemas.execution import (
    REASON_DAILY_LOSS_LIMIT,
    REASON_INVALID_QUALIFICATION,
    REASON_MAX_OPEN_EXPOSURE,
    REASON_MAX_TRADES_PER_SESSION,
    REASON_NEWS_BLACKOUT,
    REASON_NOT_QUALIFIED,
    REASON_OPPOSITE_POSITION,
    REASON_RISK_BAND_UNKNOWN,
    REASON_WEEKLY_LOSS_LIMIT,
    RiskBand,
    RiskVerdict,
)
from xauusd_bot.connectors.schemas import AccountInfo, Position

if TYPE_CHECKING:  # pragma: no cover
    from xauusd_bot.execution.emergency import EmergencyStopManager

log = structlog.get_logger(__name__)


# ----------------------------------------------------------------- constants


# Risk-per-trade fraction per EntryType / RiskBand.
# These are the canonical numbers from 05_execution_risk.md §Deliverables.
RISK_PCT_BY_BAND: dict[RiskBand, float] = {
    RiskBand.SCOUT: 0.005,    # 0.5 %
    RiskBand.REDUCED: 0.010,  # 1.0 %
    RiskBand.FULL: 0.020,     # 2.0 %
}


def risk_band_for_entry_type(entry_type: EntryType) -> RiskBand:
    """Map :class:`EntryType` to :class:`RiskBand` deterministically."""

    return {
        EntryType.SCOUT: RiskBand.SCOUT,
        EntryType.REDUCED: RiskBand.REDUCED,
        EntryType.FULL: RiskBand.FULL,
    }[entry_type]


def risk_pct_for_band(band: RiskBand) -> float:
    """Return the canonical risk-fraction for a :class:`RiskBand`."""

    return RISK_PCT_BY_BAND[band]


# ----------------------------------------------------------------- state


class _RiskState(BaseModel):
    """Mutable in-memory state — exported for the journal to dump in Block 5."""

    model_config = ConfigDict(extra="forbid")

    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")
    trades_today: int = 0
    day_key: str = ""          # "YYYY-MM-DD" (UTC) — the day the running PnL belongs to
    week_key: str = ""         # "YYYY-Www" (ISO) — the week the running PnL belongs to
    paused_until: datetime | None = None  # active pause (e.g. daily loss limit)


# ----------------------------------------------------------------- manager


class RiskManager:
    """Approve / veto new trades + maintain running daily/weekly PnL.

    Parameters
    ----------
    settings:
        :class:`Settings` (risk_max_daily, risk_max_weekly, etc.).
    get_account:
        Callable returning the current :class:`AccountInfo`. In
        production this is ``lambda: connector.get_account()``; in
        tests it can be a stub.
    get_positions:
        Callable returning the current open :class:`Position` list.
    emergency:
        Optional :class:`EmergencyStopManager` to coordinate with.
        If attached, the risk manager activates an emergency pause
        when a daily / weekly limit is hit.
    """

    def __init__(
        self,
        settings: Settings,
        get_account: Callable[[], AccountInfo],
        get_positions: Callable[[], list[Position]],
        emergency: "EmergencyStopManager | None" = None,
    ) -> None:
        self._settings = settings
        self._get_account = get_account
        self._get_positions = get_positions
        self._emergency = emergency
        self._state = _RiskState()
        self._session_window_key: str = ""

    # -------------------------------------------------------------- state mgmt

    @property
    def state(self) -> _RiskState:
        """Read-only view of the running PnL state (for the journal)."""

        return self._state

    def _day_key(self, now: datetime) -> str:
        return now.astimezone(UTC).strftime("%Y-%m-%d")

    def _week_key(self, now: datetime) -> str:
        iso = now.astimezone(UTC).isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"

    def _maybe_roll_day(self, now: datetime) -> None:
        """Reset daily PnL when the UTC date rolls."""

        key = self._day_key(now)
        if self._state.day_key and self._state.day_key != key:
            log.info("risk_day_roll", previous=self._state.day_key, current=key)
            self._state.daily_pnl = Decimal("0")
            self._state.trades_today = 0
        self._state.day_key = key

    def _maybe_roll_week(self, now: datetime) -> None:
        key = self._week_key(now)
        if self._state.week_key and self._state.week_key != key:
            log.info("risk_week_roll", previous=self._state.week_key, current=key)
            self._state.weekly_pnl = Decimal("0")
        self._state.week_key = key

    # ------------------------------------------------------------------ record

    def record_pnl(self, pnl: Decimal, now: datetime) -> None:
        """Add a realized PnL delta to the day and week totals.

        Called by the executor after every fill (full or partial).
        Negative ``pnl`` is a loss.
        """

        self._maybe_roll_day(now)
        self._maybe_roll_week(now)
        self._state.daily_pnl += pnl
        self._state.weekly_pnl += pnl
        log.debug(
            "risk_pnl_recorded",
            delta=str(pnl),
            daily=str(self._state.daily_pnl),
            weekly=str(self._state.weekly_pnl),
        )

    def record_trade(self, now: datetime) -> None:
        """Increment the trades-today counter (one per new setup)."""

        self._maybe_roll_day(now)
        self._state.trades_today += 1

    # ----------------------------------------------------------- pause state

    def pause_active(self, now: datetime) -> bool:
        """True if any active pause (emergency, daily-limit, weekly-limit) is in effect."""

        # The attached EmergencyStopManager covers the operator kill-switch
        # (dashboard STOP → manual_trigger). It is a SEPARATE state from this
        # manager's internal daily/weekly pause, so we MUST consult it here —
        # otherwise the kill-switch flattens the book but new entries still pass.
        if self._emergency is not None and self._emergency.is_active(now):
            return True
        if self._state.paused_until is None:
            return False
        if now.astimezone(UTC) >= self._state.paused_until:
            self._state.paused_until = None
            return False
        return True

    def _activate_pause(self, until: datetime, reason: str, now: datetime) -> None:
        """Set an internal pause and forward to the EmergencyStopManager if attached."""

        self._state.paused_until = until
        log.warning("risk_pause_activated", reason=reason, paused_until=until.isoformat())
        if self._emergency is not None:
            self._emergency.activate_external_pause(until=until, reason=reason, now=now)

    # ------------------------------------------------------------------ approve

    def approve(
        self,
        qualification: TradeQualification,
        now: datetime | None = None,
    ) -> RiskVerdict:
        """Decide whether to allow a new trade.

        The decision is made on the :class:`TradeQualification` and the
        current account / open-positions state. If approved, the
        verdict contains the risk fraction + USD amount the executor
        will use. If vetoed, ``approved=False`` and a stable
        ``blocked_reason`` string.
        """

        if not isinstance(qualification, TradeQualification):
            raise TypeError(
                f"RiskManager.approve expects TradeQualification, got {type(qualification).__name__}"
            )
        if not qualification.qualified:
            return self._blocked(qualification.timestamp, REASON_NOT_QUALIFIED)

        now = now or qualification.timestamp
        if now.tzinfo is None:
            raise ValueError("RiskManager.approve requires a timezone-aware `now`.")
        now = now.astimezone(UTC)

        # Roll the daily/weekly counters if the date changed.
        self._maybe_roll_day(now)
        self._maybe_roll_week(now)

        account = self._get_account()
        positions = self._get_positions()

        # 0. News-blackout veto: the decision layer already blocks,
        #    but we re-check here defensively in case the qualification
        #    was synthesized outside the normal decision pipeline.
        if qualification.final_action.value == "no_trade":
            return self._blocked(now, REASON_NOT_QUALIFIED)
        if REASON_NEWS_BLACKOUT in qualification.block_reasons:
            return self._blocked(now, REASON_NEWS_BLACKOUT)

        # 1. Resolve the risk band from the qualification.
        if qualification.final_entry_type is None:
            return self._blocked(now, REASON_INVALID_QUALIFICATION)
        try:
            band = risk_band_for_entry_type(qualification.final_entry_type)
        except KeyError:
            return self._blocked(now, REASON_RISK_BAND_UNKNOWN)
        risk_pct = risk_pct_for_band(band)

        # 2. Pause check (covers all prior limit hits).
        if self.pause_active(now):
            return RiskVerdict(
                approved=False,
                risk_band=None,
                risk_per_trade_pct=0.0,
                risk_amount=Decimal("0"),
                blocked_reason="risk_pause_active",
                daily_pnl_running=self._state.daily_pnl,
                weekly_pnl_running=self._state.weekly_pnl,
                equity=account.equity,
                open_positions=len(positions),
                trades_today=self._state.trades_today,
                timestamp=now,
            )

        # 3. Daily loss limit.
        daily_loss_pct = self._loss_pct(self._state.daily_pnl, account.equity)
        if daily_loss_pct >= self._settings.risk_max_daily:
            until = self._end_of_day(now)
            self._activate_pause(until, REASON_DAILY_LOSS_LIMIT, now)
            return self._blocked(now, REASON_DAILY_LOSS_LIMIT)

        # 4. Weekly loss limit.
        weekly_loss_pct = self._loss_pct(self._state.weekly_pnl, account.equity)
        if weekly_loss_pct >= self._settings.risk_max_weekly:
            until = self._end_of_week(now)
            self._activate_pause(until, REASON_WEEKLY_LOSS_LIMIT, now)
            return self._blocked(now, REASON_WEEKLY_LOSS_LIMIT)

        # 5. Max open exposure.
        if len(positions) >= self._settings.risk_max_open_positions:
            return self._blocked(now, REASON_MAX_OPEN_EXPOSURE)

        # 6. Max trades per session.
        if self._state.trades_today >= self._settings.risk_max_trades_per_session:
            return self._blocked(now, REASON_MAX_TRADES_PER_SESSION)

        # 7. Opposite positions (no hedge in Block 3).
        if self._has_opposite_position(positions, qualification):
            return self._blocked(now, REASON_OPPOSITE_POSITION)

        # 8. Compute the risk amount in USD.
        equity = account.equity
        risk_amount = (equity * Decimal(str(risk_pct))).quantize(Decimal("0.01"))

        return RiskVerdict(
            approved=True,
            risk_band=band,
            risk_per_trade_pct=risk_pct,
            risk_amount=risk_amount,
            blocked_reason=None,
            daily_pnl_running=self._state.daily_pnl,
            weekly_pnl_running=self._state.weekly_pnl,
            equity=equity,
            open_positions=len(positions),
            trades_today=self._state.trades_today,
            timestamp=now,
        )

    # --------------------------------------------------------------- helpers

    def _blocked(self, now: datetime, reason: str) -> RiskVerdict:
        """Build a uniform veto :class:`RiskVerdict`."""

        try:
            account = self._get_account()
            positions = self._get_positions()
        except Exception:  # noqa: BLE001
            account = None
            positions = []
        return RiskVerdict(
            approved=False,
            risk_band=None,
            risk_per_trade_pct=0.0,
            risk_amount=Decimal("0"),
            blocked_reason=reason,
            daily_pnl_running=self._state.daily_pnl,
            weekly_pnl_running=self._state.weekly_pnl,
            equity=account.equity if account else Decimal("0"),
            open_positions=len(positions),
            trades_today=self._state.trades_today,
            timestamp=now,
        )

    @staticmethod
    def _loss_pct(pnl: Decimal, equity: Decimal) -> float:
        """Return the loss as a positive fraction of equity (0 if pnl ≥ 0)."""

        if equity <= 0:
            return 0.0
        if pnl >= 0:
            return 0.0
        return float(-pnl / equity)

    @staticmethod
    def _has_opposite_position(positions: list[Position], q: TradeQualification) -> bool:
        """True if any open position is opposite to the qualification's direction.

        Block 3 has no hedge setup, so any long+short combination is
        a hard veto.
        """

        if not positions:
            return False
        from xauusd_bot.connectors.schemas import OrderSide

        want_long = q.final_action.value == "enter_long"
        for p in positions:
            if want_long and p.side == OrderSide.SELL:
                return True
            if not want_long and p.side == OrderSide.BUY:
                return True
        return False

    @staticmethod
    def _end_of_day(now: datetime) -> datetime:
        """End of the current UTC day (start of next day, exclusive)."""

        tomorrow = (now.astimezone(UTC) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return tomorrow

    @staticmethod
    def _end_of_week(now: datetime) -> datetime:
        """End of the current ISO week (Monday 00:00 UTC, exclusive)."""

        utc_now = now.astimezone(UTC)
        # isocalendar: Mon=1, Sun=7
        days_to_monday = utc_now.isocalendar().weekday - 1
        monday = (utc_now - timedelta(days=days_to_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return monday + timedelta(days=7)


# ----------------------------------------------------------------- re-exports

__all__ = [
    "REASON_DAILY_LOSS_LIMIT",
    "REASON_MAX_OPEN_EXPOSURE",
    "REASON_MAX_TRADES_PER_SESSION",
    "REASON_NEWS_BLACKOUT",
    "REASON_OPPOSITE_POSITION",
    "REASON_RISK_BAND_UNKNOWN",
    "REASON_WEEKLY_LOSS_LIMIT",
    "RiskBand",
    "RiskManager",
    "risk_band_for_entry_type",
    "risk_pct_for_band",
]
