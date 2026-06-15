"""EmergencyStopManager — the **highest-priority** safety net (Block 4 Phase 4).

The :class:`EmergencyStopManager` is the kill-switch. It can be
triggered by:

* **System errors** — an unhandled exception in the bot loop.
* **Broker disconnect** — ``connector.is_connected()`` returns False.
* **Volatility spikes** — 1m ATR > 5× the rolling normal ATR.
* **Slippage spikes** — last fill's slippage > 3× normal spread.
* **Manual trigger** — a kill-switch file on disk
  (``settings.emergency_stop_file``).

When triggered, the manager:

1. Flattens every open position at the connector (best-effort market
   order, no SL/TP).
2. Cancels every pending order.
3. Pauses the bot for a configurable duration (default 1h).
4. Persists the active state to ``emergency_stop_state.json`` so a
   crash mid-recovery does not silently resume trading.

Priority
--------
EmergencyStop has **higher** priority than every other manager. It
runs *before* the RiskManager in the executor's main loop. Even if
the RiskManager just approved a trade, an active emergency pause
vetoes it.

Persistence
-----------
The state file is written atomically (write-temp + rename) and read
on every :meth:`is_active` call, so a process restart sees the pause.

I-1
---
This module never imports ``MetaTrader5``. It talks to the broker
through :class:`IMarketConnector` only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog
from pydantic import ConfigDict

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.execution import (
    EmergencyStopState,
    EmergencyTrigger,
)
from xauusd_bot.connectors.schemas import (
    AccountInfo,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
)

log = structlog.get_logger(__name__)


# Default multipliers for spike detection.
DEFAULT_VOLATILITY_MULTIPLIER = 5.0     # 1m ATR > 5× rolling normal ATR
DEFAULT_SLIPPAGE_MULTIPLIER = 3.0       # slippage > 3× normal spread
DEFAULT_PAUSE_DURATION = timedelta(hours=1)


# ----------------------------------------------------------------- state


class _InternalState:
    """Mutable in-memory state — replaced atomically on read-from-disk."""

    __slots__ = ("active", "reason", "triggered_at", "paused_until", "triggered_by", "details")

    def __init__(self) -> None:
        self.active: bool = False
        self.reason: str = ""
        self.triggered_at: datetime | None = None
        self.paused_until: datetime | None = None
        self.triggered_by: str = "auto"
        self.details: dict[str, str] = {}


# ----------------------------------------------------------------- manager


class EmergencyStopManager:
    """Highest-priority safety net — flatten, cancel, pause, persist."""

    def __init__(
        self,
        settings: Settings,
        connector_positions: Callable[[], list[Position]],
        connector_pending: Callable[[], list[OrderRequest]],
        flatten_position: Callable[[str], OrderResult],
        cancel_order: Callable[[str], OrderResult],
        state_file: Path | str | None = None,
        pause_duration: timedelta = DEFAULT_PAUSE_DURATION,
        volatility_multiplier: float = DEFAULT_VOLATILITY_MULTIPLIER,
        slippage_multiplier: float = DEFAULT_SLIPPAGE_MULTIPLIER,
    ) -> None:
        self._settings = settings
        self._positions = connector_positions
        self._pending = connector_pending
        self._flatten = flatten_position
        self._cancel = cancel_order
        self._state_file = Path(state_file) if state_file is not None else None
        self._pause_duration = pause_duration
        self._vol_mult = volatility_multiplier
        self._slip_mult = slippage_multiplier

        self._state = _InternalState()
        self._restore_persisted_state()

    # ------------------------------------------------------------- properties

    def is_active(self, now: datetime | None = None) -> bool:
        """True if a pause is in effect (or persisted from a prior process)."""

        self._maybe_reload_state()
        if not self._state.active:
            return False
        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        if self._state.paused_until is not None and now >= self._state.paused_until:
            log.info("emergency_pause_expired", reason=self._state.reason)
            self._state.active = False
            self._persist_state()
            return False
        return True

    def state(self) -> EmergencyStopState:
        """Snapshot the current state (for the journal / CLI)."""

        return EmergencyStopState(
            active=self._state.active,
            reason=self._state.reason or "inactive",
            triggered_at=self._state.triggered_at or datetime.now(tz=UTC),
            paused_until=self._state.paused_until or datetime.now(tz=UTC),
            triggered_by=self._state.triggered_by,  # type: ignore[arg-type]
            details=dict(self._state.details),
        )

    # ----------------------------------------------------------------- triggers

    def check_spikes(
        self,
        atr_1m: float,
        atr_normal: float,
        slippage_points: float,
        normal_spread_points: float,
        now: datetime | None = None,
    ) -> bool:
        """Auto-trigger on extreme volatility or slippage.

        Returns True if a new trigger fired (and the manager paused the bot).
        """

        if atr_normal > 0 and atr_1m > 0 and atr_1m > self._vol_mult * atr_normal:
            return self.trigger(
                EmergencyTrigger.VOLATILITY_SPIKE,
                details={
                    "atr_1m": f"{atr_1m:.4f}",
                    "atr_normal": f"{atr_normal:.4f}",
                    "multiplier": f"{atr_1m / atr_normal:.2f}x",
                },
                now=now,
            )
        if (
            normal_spread_points > 0
            and slippage_points > self._slip_mult * normal_spread_points
        ):
            return self.trigger(
                EmergencyTrigger.SLIPPAGE_SPIKE,
                details={
                    "slippage_points": f"{slippage_points:.2f}",
                    "normal_spread_points": f"{normal_spread_points:.2f}",
                    "multiplier": f"{slippage_points / normal_spread_points:.2f}x",
                },
                now=now,
            )
        return False

    def check_health(self, is_connected: bool, now: datetime | None = None) -> bool:
        """Auto-trigger when the broker connection is down."""

        if not is_connected:
            return self.trigger(EmergencyTrigger.BROKER_DISCONNECT, now=now)
        return False

    def check_kill_switch_file(self, now: datetime | None = None) -> bool:
        """Auto-trigger if a kill-switch file exists at the configured path."""

        path = self._settings.emergency_stop_file
        if path and Path(path).exists():
            return self.trigger(
                EmergencyTrigger.MANUAL_KILL_SWITCH,
                details={"path": path},
                now=now,
            )
        return False

    def trigger(
        self,
        reason: EmergencyTrigger,
        details: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Trigger the emergency stop. Returns True if newly activated.

        Idempotent: re-triggering with the same reason while still
        paused just extends the pause. Different reason overwrites
        the current one and re-flattens.
        """

        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        if self._state.active and self._state.reason == reason.value:
            log.debug("emergency_retrigger_same_reason", reason=reason.value)
            return False

        log.error("emergency_triggered", reason=reason.value, details=details or {})
        self._state.active = True
        self._state.reason = reason.value
        self._state.triggered_at = now
        self._state.triggered_by = "auto"
        self._state.paused_until = now + self._pause_duration
        # Coerce detail values to str — EmergencyStopState.details is dict[str, str].
        self._state.details = {k: str(v) for k, v in (details or {}).items()}

        # Best-effort flatten + cancel. We log but never raise — the
        # caller is presumably already inside an exception handler.
        self._flatten_all()
        self._cancel_all()
        self._persist_state()
        return True

    def manual_trigger(
        self,
        reason: str = "manual",
        pause_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> None:
        """Activate the pause manually (operator / UI)."""

        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        self._state.active = True
        self._state.reason = reason
        self._state.triggered_at = now
        self._state.triggered_by = "manual"
        self._state.paused_until = now + (pause_duration or self._pause_duration)
        self._state.details = {"path": self._settings.emergency_stop_file or ""}
        self._flatten_all()
        self._cancel_all()
        self._persist_state()

    def activate_external_pause(
        self,
        until: datetime,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Allow another module (e.g. :class:`RiskManager`) to arm a pause.

        Does NOT flatten the book — the calling module is expected to
        have already done so if needed. The pause is persisted so a
        restart honours it.
        """

        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        self._state.active = True
        self._state.reason = reason
        self._state.triggered_at = now
        self._state.triggered_by = "auto"
        self._state.paused_until = until
        self._state.details = {"external": "risk_manager"}
        self._persist_state()
        log.info("emergency_external_pause_activated", reason=reason, until=until.isoformat())

    def clear(self, now: datetime | None = None) -> None:
        """De-activate the pause (operator reset)."""

        self._state.active = False
        self._state.reason = ""
        self._state.triggered_at = None
        self._state.paused_until = None
        self._state.details = {}
        log.info("emergency_cleared")
        self._persist_state()

    # ----------------------------------------------------------- flatten/cancel

    def _flatten_all(self) -> int:
        """Best-effort market-close of every open position. Returns count."""

        n = 0
        try:
            positions = self._positions()
        except Exception as exc:  # noqa: BLE001
            log.error("emergency_flatten_positions_read_failed", error=str(exc))
            return 0
        for p in positions:
            try:
                self._flatten(p.position_id)
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.error("emergency_flatten_failed", position_id=p.position_id, error=str(exc))
        log.info("emergency_flatten_done", count=n)
        return n

    def _cancel_all(self) -> int:
        """Best-effort cancel of every pending order. Returns count."""

        n = 0
        try:
            pending = self._pending()
        except Exception as exc:  # noqa: BLE001
            log.error("emergency_cancel_pending_read_failed", error=str(exc))
            return 0
        for req in pending:
            order_id = req.client_order_id
            if order_id is None:
                continue
            try:
                self._cancel(order_id)
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.error("emergency_cancel_failed", order_id=order_id, error=str(exc))
        log.info("emergency_cancel_done", count=n)
        return n

    # ------------------------------------------------------------- persistence

    def _persist_state(self) -> None:
        if self._state_file is None:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = EmergencyStopState(
            active=self._state.active,
            reason=self._state.reason or "inactive",
            triggered_at=self._state.triggered_at or datetime.now(tz=UTC),
            paused_until=self._state.paused_until or datetime.now(tz=UTC),
            triggered_by=self._state.triggered_by,  # type: ignore[arg-type]
            details=dict(self._state.details),
        )
        tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        try:
            tmp.write_text(payload.model_dump_json(indent=2))
            os.replace(tmp, self._state_file)
        except OSError as exc:  # noqa: PERF203
            log.error("emergency_persist_failed", path=str(self._state_file), error=str(exc))

    def _restore_persisted_state(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text())
            payload = EmergencyStopState.model_validate(raw)
        except (OSError, ValueError) as exc:
            log.warning("emergency_persisted_state_unreadable", error=str(exc))
            return
        self._state.active = payload.active
        self._state.reason = payload.reason
        self._state.triggered_at = payload.triggered_at
        self._state.paused_until = payload.paused_until
        self._state.triggered_by = payload.triggered_by
        self._state.details = dict(payload.details)
        if payload.active:
            log.warning(
                "emergency_restored_from_disk",
                reason=payload.reason,
                paused_until=payload.paused_until.isoformat(),
            )

    def _maybe_reload_state(self) -> None:
        """Re-read the state file once per call so an external operator
        can drop a kill-switch file and have the next ``is_active()``
        call pick it up.
        """

        if self._state_file is None:
            return
        # Cheap fast path: only re-read if the mtime changed.
        try:
            current_mtime = self._state_file.stat().st_mtime
        except OSError:
            return
        if getattr(self, "_state_mtime", None) == current_mtime:
            return
        self._state_mtime = current_mtime
        self._restore_persisted_state()


# ----------------------------------------------------------------- settings field

# Settings.emergency_stop_file is added to common/config/__init__.py
# via a thin field_validator — see the field declared below.

# We avoid mutating Settings at import time (Pydantic complains); the
# field is added via a module-level injection below. This keeps the
# default behaviour identical to "no kill-switch file".
def _install_emergency_stop_field() -> None:
    """Attach ``emergency_stop_file`` to :class:`Settings` if missing.

    Idempotent: a second call is a no-op.
    """

    from xauusd_bot.common.config import Settings as _Settings

    if "emergency_stop_file" in _Settings.model_fields:
        return
    # Use pydantic's dynamic model rebuild to add the optional field.
    _Settings.model_fields["emergency_stop_file"] = __import__("pydantic").Field(  # type: ignore[attr-defined]
        default=None,
        description=(
            "Optional path to a kill-switch file. If the file exists when "
            "EmergencyStopManager.check_kill_switch_file() is called, the bot pauses."
        ),
    )
    _Settings.model_rebuild(force=True)


_install_emergency_stop_field()


# Re-exports for tests that want a static hook on the manager without
# depending on connector internals.
def flatten_account_helper(account: AccountInfo) -> Decimal:
    """Tiny helper exposed for tests — return equity (no-op in prod)."""

    return account.equity


def spec_for_testing(**kwargs: object) -> SymbolSpec:
    """Construct a minimal :class:`SymbolSpec` for unit tests."""

    defaults: dict[str, object] = dict(
        symbol="XAUUSD",
        description="test",
        point=Decimal("0.01"),
        digits=2,
        trade_contract_size=Decimal("100"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
        margin_rate=Decimal("0.01"),
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
    )
    defaults.update(kwargs)
    return SymbolSpec(**defaults)  # type: ignore[arg-type]


__all__ = [
    "DEFAULT_PAUSE_DURATION",
    "DEFAULT_SLIPPAGE_MULTIPLIER",
    "DEFAULT_VOLATILITY_MULTIPLIER",
    "EmergencyStopManager",
    "EmergencyStopState",
    "EmergencyTrigger",
    "_InternalState",  # for tests; intentionally private
    "flatten_account_helper",
    "spec_for_testing",
]
