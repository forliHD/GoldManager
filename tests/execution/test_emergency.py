"""Tests for EmergencyStopManager — Block 4 Phase 4 (kill-switch)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from xauusd_bot.common.schemas.execution import (
    EmergencyStopState,
    EmergencyTrigger,
)
from xauusd_bot.connectors.schemas import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
)
from xauusd_bot.execution.emergency import EmergencyStopManager

from tests._execution_factories import make_account, make_order_request, make_position, make_settings


# ----------------------------------------------------------------- stubs


class _Recorder:
    """Records calls to flatten + cancel for assertions."""

    def __init__(self) -> None:
        self.flatten_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.positions: list[Position] = []
        self.pending: list[OrderRequest] = []

    def flatten(self, position_id: str) -> OrderResult:
        self.flatten_calls.append(position_id)
        return OrderResult(accepted=True, order_id=position_id)

    def cancel(self, order_id: str) -> OrderResult:
        self.cancel_calls.append(order_id)
        return OrderResult(accepted=True, order_id=order_id)


def _make_manager(
    recorder: _Recorder | None = None,
    state_file: Path | None = None,
    pause_duration: timedelta = timedelta(hours=1),
) -> tuple[EmergencyStopManager, _Recorder]:
    rec = recorder or _Recorder()
    mgr = EmergencyStopManager(
        settings=make_settings(),
        connector_positions=lambda: rec.positions,
        connector_pending=lambda: rec.pending,
        flatten_position=rec.flatten,
        cancel_order=rec.cancel,
        state_file=state_file,
        pause_duration=pause_duration,
    )
    return mgr, rec


# ----------------------------------------------------------------- 1. initial state


def test_initial_state_is_inactive() -> None:
    mgr, _ = _make_manager()
    assert mgr.is_active() is False
    s = mgr.state()
    assert s.active is False


# ----------------------------------------------------------------- 2. volatility spike


def test_volatility_spike_triggers_emergency() -> None:
    mgr, rec = _make_manager()
    fired = mgr.check_spikes(
        atr_1m=10.0, atr_normal=1.0,
        slippage_points=5.0, normal_spread_points=30.0,
    )
    assert fired is True
    assert mgr.is_active()
    assert mgr.state().reason == EmergencyTrigger.VOLATILITY_SPIKE.value


def test_volatility_just_below_threshold_does_not_trigger() -> None:
    mgr, _ = _make_manager()
    fired = mgr.check_spikes(
        atr_1m=4.5, atr_normal=1.0,
        slippage_points=5.0, normal_spread_points=30.0,
    )
    assert fired is False
    assert mgr.is_active() is False


# ----------------------------------------------------------------- 3. slippage spike


def test_slippage_spike_triggers_emergency() -> None:
    mgr, _ = _make_manager()
    fired = mgr.check_spikes(
        atr_1m=1.0, atr_normal=1.0,
        slippage_points=200.0, normal_spread_points=30.0,  # 6.67x
    )
    assert fired is True
    assert mgr.state().reason == EmergencyTrigger.SLIPPAGE_SPIKE.value


# ----------------------------------------------------------------- 4. broker disconnect


def test_broker_disconnect_triggers_emergency() -> None:
    mgr, _ = _make_manager()
    fired = mgr.check_health(is_connected=False)
    assert fired is True
    assert mgr.state().reason == EmergencyTrigger.BROKER_DISCONNECT.value


def test_broker_connected_does_not_trigger() -> None:
    mgr, _ = _make_manager()
    fired = mgr.check_health(is_connected=True)
    assert fired is False


# ----------------------------------------------------------------- 5. flatten + cancel on trigger


def test_trigger_flattens_positions_and_cancels_pending() -> None:
    mgr, rec = _make_manager()
    rec.positions = [make_position(position_id="p1"), make_position(position_id="p2", side=OrderSide.SELL)]
    rec.pending = [
        make_order_request(type=OrderType.LIMIT, client_order_id="pend-1"),
        make_order_request(type=OrderType.STOP, client_order_id="pend-2"),
    ]
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH)
    assert set(rec.flatten_calls) == {"p1", "p2"}
    assert set(rec.cancel_calls) == {"pend-1", "pend-2"}


def test_trigger_idempotent_for_same_reason() -> None:
    mgr, rec = _make_manager()
    mgr.trigger(EmergencyTrigger.BROKER_DISCONNECT)
    rec.flatten_calls.clear()
    rec.cancel_calls.clear()
    # Second trigger with same reason → no-op.
    fired = mgr.trigger(EmergencyTrigger.BROKER_DISCONNECT)
    assert fired is False
    assert rec.flatten_calls == []


# ----------------------------------------------------------------- 6. pause expiry


def test_pause_expires_after_duration() -> None:
    mgr, _ = _make_manager(pause_duration=timedelta(minutes=1))
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH)
    # Before the pause expires.
    assert mgr.is_active() is True
    # After the pause expires.
    later = datetime.now(tz=UTC) + timedelta(hours=1)
    assert mgr.is_active(now=later) is False


# ----------------------------------------------------------------- 7. clear


def test_clear_deactivates_pause() -> None:
    mgr, _ = _make_manager()
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH)
    assert mgr.is_active()
    mgr.clear()
    assert mgr.is_active() is False
    s = mgr.state()
    assert s.active is False


# ----------------------------------------------------------------- 8. persistence


def test_pause_persists_to_file(tmp_path: Path) -> None:
    state_file = tmp_path / "emergency.json"
    mgr, _ = _make_manager(state_file=state_file)
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH, details={"x": "1"})
    # New manager on the same file picks it up.
    mgr2, _ = _make_manager(state_file=state_file)
    assert mgr2.is_active() is True
    assert mgr2.state().reason == EmergencyTrigger.MANUAL_KILL_SWITCH.value


def test_persistence_survives_no_state_file() -> None:
    """If the state file doesn't exist, a fresh manager starts inactive."""

    mgr, _ = _make_manager(state_file=Path("/tmp/__nonexistent_emergency__.json"))
    assert mgr.is_active() is False


# ----------------------------------------------------------------- 9. external pause


def test_external_pause_from_risk_manager() -> None:
    """The RiskManager activates a pause; the emergency manager honours it."""

    mgr, _ = _make_manager()
    until = datetime.now(tz=UTC) + timedelta(hours=1)
    mgr.activate_external_pause(until=until, reason="daily_loss_limit")
    assert mgr.is_active() is True
    assert mgr.state().reason == "daily_loss_limit"
    assert mgr.state().triggered_by == "auto"


# ----------------------------------------------------------------- 10. kill-switch file


def test_kill_switch_file_triggers(tmp_path: Path) -> None:
    state_file = tmp_path / "emergency.json"
    settings = make_settings()
    settings.emergency_stop_file = str(tmp_path / "kill_switch")  # type: ignore[attr-defined]
    (tmp_path / "kill_switch").touch()

    mgr = EmergencyStopManager(
        settings=settings,
        connector_positions=lambda: [],
        connector_pending=lambda: [],
        flatten_position=lambda pid: OrderResult(accepted=True, order_id=pid),
        cancel_order=lambda oid: OrderResult(accepted=True, order_id=oid),
        state_file=state_file,
    )
    fired = mgr.check_kill_switch_file()
    assert fired is True
    assert mgr.is_active()


def test_kill_switch_file_absent_does_not_trigger(tmp_path: Path) -> None:
    state_file = tmp_path / "emergency.json"
    settings = make_settings()
    settings.emergency_stop_file = str(tmp_path / "kill_switch_does_not_exist")  # type: ignore[attr-defined]
    mgr = EmergencyStopManager(
        settings=settings,
        connector_positions=lambda: [],
        connector_pending=lambda: [],
        flatten_position=lambda pid: OrderResult(accepted=True, order_id=pid),
        cancel_order=lambda oid: OrderResult(accepted=True, order_id=oid),
        state_file=state_file,
    )
    assert mgr.check_kill_switch_file() is False


# ----------------------------------------------------------------- 11. manual trigger


def test_manual_trigger_records_operator() -> None:
    mgr, _ = _make_manager()
    mgr.manual_trigger(reason="operator_pressed_pause", pause_duration=timedelta(minutes=30))
    s = mgr.state()
    assert s.active is True
    assert s.triggered_by == "manual"
    assert s.reason == "operator_pressed_pause"


# ----------------------------------------------------------------- 12. connector errors are swallowed


def test_flatten_errors_dont_break_trigger() -> None:
    """If flatten raises, the trigger still completes + persists state."""

    def _boom(pid: str) -> OrderResult:
        raise ConnectionError("broker gone")

    mgr = EmergencyStopManager(
        settings=make_settings(),
        connector_positions=lambda: [make_position(position_id="p1")],
        connector_pending=lambda: [],
        flatten_position=_boom,
        cancel_order=lambda oid: OrderResult(accepted=True, order_id=oid),
        state_file=None,
    )
    # Should not raise.
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH)
    assert mgr.is_active()


def test_cancel_errors_dont_break_trigger() -> None:
    def _boom(oid: str) -> OrderResult:
        raise RuntimeError("cancel down")

    mgr = EmergencyStopManager(
        settings=make_settings(),
        connector_positions=lambda: [],
        connector_pending=lambda: [
            make_order_request(type=OrderType.LIMIT, client_order_id="p1")
        ],
        flatten_position=lambda pid: OrderResult(accepted=True, order_id=pid),
        cancel_order=_boom,
        state_file=None,
    )
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH)
    assert mgr.is_active()


# ----------------------------------------------------------------- 13. state serialization


def test_state_dump_is_json_serializable() -> None:
    """The :class:`EmergencyStopState` is JSON-serializable for the journal."""

    import json

    mgr, _ = _make_manager()
    mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH, details={"k": "v"})
    s = mgr.state()
    json.dumps(s.model_dump(mode="json"))


# ----------------------------------------------------------------- 14. different reason re-triggers


def test_different_reason_retriggers() -> None:
    mgr, _ = _make_manager()
    mgr.trigger(EmergencyTrigger.BROKER_DISCONNECT)
    # Different reason → re-trigger + re-flattens (but no positions to flatten).
    fired = mgr.trigger(EmergencyTrigger.MANUAL_KILL_SWITCH)
    assert fired is True
    assert mgr.state().reason == EmergencyTrigger.MANUAL_KILL_SWITCH.value
