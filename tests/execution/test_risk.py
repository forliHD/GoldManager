"""Tests for RiskManager — Block 4 Phase 0 (approval gate)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    DecisionAction,
    EntryType,
    ScoreBand,
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
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.execution.risk import (
    RISK_PCT_BY_BAND,
    RiskManager,
    risk_band_for_entry_type,
    risk_pct_for_band,
)

from tests._execution_factories import (
    make_account,
    make_position,
    make_qualification,
    make_settings,
)


# ----------------------------------------------------------------- helpers


def _empty_positions() -> list:
    return []


def _now() -> datetime:
    # A mid-week day so weekly rollover isn't an issue.
    return datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


# ============================================================== 1. basics


def test_risk_pct_table_is_canonical() -> None:
    """The band → fraction table is locked: 0.5 % / 1 % / 2 %."""

    assert RISK_PCT_BY_BAND[RiskBand.SCOUT] == 0.005
    assert RISK_PCT_BY_BAND[RiskBand.REDUCED] == 0.010
    assert RISK_PCT_BY_BAND[RiskBand.FULL] == 0.020


def test_risk_band_for_entry_type_is_deterministic() -> None:
    assert risk_band_for_entry_type(EntryType.SCOUT) == RiskBand.SCOUT
    assert risk_band_for_entry_type(EntryType.REDUCED) == RiskBand.REDUCED
    assert risk_band_for_entry_type(EntryType.FULL) == RiskBand.FULL
    assert risk_pct_for_band(RiskBand.FULL) == 0.02


# ============================================================== 2. happy path


def test_approve_qualified_full_trade() -> None:
    """A qualified FULL trade on a clean account is approved at 2 %."""

    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    qual = make_qualification(entry_type=EntryType.FULL, score=88.0, band=ScoreBand.FULL_85_PLUS)
    v = rm.approve(qual, now=_now())
    assert v.approved
    assert v.risk_band == RiskBand.FULL
    assert v.risk_per_trade_pct == 0.02
    assert v.risk_amount == Decimal("200.00")
    assert v.blocked_reason is None
    assert v.open_positions == 0


class _FakeEmergency:
    """Minimal EmergencyStopManager stand-in — only is_active() is consulted."""

    def __init__(self, active: bool) -> None:
        self._active = active

    def is_active(self, now=None) -> bool:  # noqa: ANN001
        return self._active


def test_emergency_stop_vetoes_qualified_entry() -> None:
    """REGRESSION: the operator kill-switch must block NEW entries, not just flatten.

    The dashboard STOP triggers the attached EmergencyStopManager; RiskManager.
    approve() must consult it (it used to check only its own internal pause, so
    a kill-switched bot still opened trades).
    """
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
        emergency=_FakeEmergency(active=True),
    )
    qual = make_qualification(entry_type=EntryType.FULL, score=88.0, band=ScoreBand.FULL_85_PLUS)
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == "risk_pause_active"


def test_no_emergency_allows_qualified_entry() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
        emergency=_FakeEmergency(active=False),
    )
    qual = make_qualification(entry_type=EntryType.FULL, score=88.0, band=ScoreBand.FULL_85_PLUS)
    assert rm.approve(qual, now=_now()).approved


def test_approve_qualified_scout_trade_uses_half_pct() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    qual = make_qualification(
        entry_type=EntryType.SCOUT, score=70.0, band=ScoreBand.PREPARE_65_74
    )
    v = rm.approve(qual, now=_now())
    assert v.approved
    assert v.risk_per_trade_pct == 0.005
    assert v.risk_amount == Decimal("50.00")


def test_approve_qualified_reduced_trade_uses_one_pct() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    qual = make_qualification(
        entry_type=EntryType.REDUCED, score=80.0, band=ScoreBand.REDUCED_75_84
    )
    v = rm.approve(qual, now=_now())
    assert v.approved
    assert v.risk_per_trade_pct == 0.010
    assert v.risk_amount == Decimal("100.00")


# ============================================================== 3. not qualified → block


def test_unqualified_qualification_blocks() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    qual = make_qualification(qualified=False, action=DecisionAction.NO_TRADE, entry_type=None)
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_NOT_QUALIFIED
    assert v.risk_amount == Decimal("0")


def test_news_blackout_in_qualification_blocks() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    qual = make_qualification(
        entry_type=EntryType.FULL, block_reasons=[REASON_NEWS_BLACKOUT, "extra"]
    )
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_NEWS_BLACKOUT


# ============================================================== 4. daily loss limit


def test_daily_loss_limit_blocks_and_pauses() -> None:
    """A realized 4 % daily loss blocks the next trade + arms a daily pause."""

    rm = RiskManager(
        settings=make_settings(risk_max_daily=0.04),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    rm.record_pnl(pnl=Decimal("-500"), now=_now())  # 5 % of equity
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_DAILY_LOSS_LIMIT
    # The pause is now active.
    assert rm.pause_active(_now() + timedelta(hours=1))
    assert not rm.pause_active(_now() + timedelta(days=2))


def test_daily_loss_below_threshold_passes() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_daily=0.04),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    rm.record_pnl(pnl=Decimal("-300"), now=_now())  # 3 % loss < 4 % limit
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert v.approved


# ============================================================== 5. weekly loss limit


def test_weekly_loss_limit_blocks_and_pauses_until_monday() -> None:
    """Weekly 8.5 % loss with daily 3 % loss — weekly triggers (daily is below 4 % threshold)."""

    rm = RiskManager(
        settings=make_settings(risk_max_daily=0.04, risk_max_weekly=0.08),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    # Roll the day key forward, then set weekly PnL to 8.5 % with
    # daily PnL at 3 % (below the 4 % daily limit). The manager uses
    # one running total, so we use the internal state to set them
    # independently for this test.
    rm._state.day_key = "2026-04-15"  # noqa: SLF001
    rm._state.daily_pnl = Decimal("-300")  # noqa: SLF001
    rm._state.weekly_pnl = Decimal("-850")  # noqa: SLF001
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_WEEKLY_LOSS_LIMIT
    # Pause is armed — until next Monday.
    assert rm.pause_active(_now() + timedelta(days=2))


# ============================================================== 6. open exposure


def test_max_open_exposure_blocks() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_open_positions=3),
        get_account=lambda: make_account(),
        get_positions=lambda: [
            make_position(position_id=f"p-{i}", side=OrderSide.BUY) for i in range(3)
        ],
    )
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_MAX_OPEN_EXPOSURE
    assert v.open_positions == 3


def test_below_max_exposure_passes() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_open_positions=3),
        get_account=lambda: make_account(),
        get_positions=lambda: [
            make_position(position_id="p-1", side=OrderSide.BUY)
        ],
    )
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert v.approved
    assert v.open_positions == 1


# ============================================================== 7. trades per session


def test_max_trades_per_session_blocks() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_trades_per_session=5),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    for _ in range(5):
        rm.record_trade(now=_now())
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_MAX_TRADES_PER_SESSION


# ============================================================== 8. opposite position


def test_opposite_position_blocks() -> None:
    """A long trade with an open short position is vetoed (no hedge)."""

    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=lambda: [make_position(side=OrderSide.SELL)],
    )
    qual = make_qualification(action=DecisionAction.ENTER_LONG, direction="long")
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_OPPOSITE_POSITION


def test_same_direction_position_allowed() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_open_positions=3),
        get_account=lambda: make_account(),
        get_positions=lambda: [make_position(side=OrderSide.BUY)],
    )
    qual = make_qualification(action=DecisionAction.ENTER_LONG, direction="long")
    v = rm.approve(qual, now=_now())
    assert v.approved


# ============================================================== 9. invalid input


def test_approve_with_non_qualification_raises() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    with pytest.raises(TypeError):
        rm.approve("not a qualification", now=_now())  # type: ignore[arg-type]


def test_naive_now_raises() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    qual = make_qualification()
    with pytest.raises(ValueError):
        rm.approve(qual, now=datetime(2026, 4, 15))  # naive


# ============================================================== 10. day rollover


def test_daily_pnl_resets_on_new_day() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_daily=0.04),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    # Day 1: 5 % loss.
    rm.record_pnl(pnl=Decimal("-500"), now=datetime(2026, 4, 15, 23, 0, tzinfo=UTC))
    # Day 2 (rollover): counters should reset.
    day2 = datetime(2026, 4, 16, 1, 0, tzinfo=UTC)
    rm.record_pnl(pnl=Decimal("0"), now=day2)
    qual = make_qualification()
    v = rm.approve(qual, now=day2)
    assert v.approved
    assert v.daily_pnl_running == Decimal("0")


def test_weekly_pnl_resets_on_new_week() -> None:
    rm = RiskManager(
        settings=make_settings(risk_max_weekly=0.08),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    rm.record_pnl(pnl=Decimal("-900"), now=datetime(2026, 4, 17, 22, 0, tzinfo=UTC))  # Friday
    # Next Monday 00:00 UTC → weekly roll.
    next_monday = datetime(2026, 4, 20, 1, 0, tzinfo=UTC)
    qual = make_qualification()
    v = rm.approve(qual, now=next_monday)
    assert v.approved
    assert v.weekly_pnl_running == Decimal("0")


# ============================================================== 11. pause integration


def test_pause_via_external_activation_blocks_subsequent_approvals() -> None:
    """When the manager's pause is active, approve() returns a blocked verdict."""

    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    rm._activate_pause(  # noqa: SLF001 — direct test of the internal pause
        until=_now() + timedelta(hours=1),
        reason="manual_test",
        now=_now(),
    )
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == "risk_pause_active"


# ============================================================== 12. emergency integration


def test_daily_limit_activates_emergency_pause() -> None:
    """The RiskManager forwards the daily-loss pause to the EmergencyStopManager."""

    from xauusd_bot.execution.emergency import EmergencyStopManager

    settings = make_settings(risk_max_daily=0.04)
    emergency = EmergencyStopManager(
        settings=settings,
        connector_positions=lambda: [],
        connector_pending=lambda: [],
        flatten_position=lambda pid: type("R", (), {"accepted": True, "order_id": pid})(),
        cancel_order=lambda oid: type("R", (), {"accepted": True, "order_id": oid})(),
        state_file=None,
    )
    rm = RiskManager(
        settings=settings,
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
        emergency=emergency,
    )
    rm.record_pnl(pnl=Decimal("-500"), now=_now())
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_DAILY_LOSS_LIMIT
    # The emergency manager has been notified and is active.
    assert emergency.is_active(now=_now())


# ============================================================== 13. invalid entry type


def test_unknown_entry_type_blocks() -> None:
    """A qualification with a None entry_type is vetoed (defensive)."""

    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    qual = make_qualification(entry_type=None, qualified=True, action=DecisionAction.ENTER_LONG)
    # bypass TradeQualification's own None-check by constructing manually.
    qual = TradeQualification.__class__.model_validate(
        rm.state  # not used; this is just to keep type-checkers happy
    ) if False else qual
    # Build a TradeQualification with a None entry_type directly.
    from xauusd_bot.common.schemas.decision import TradeQualification

    qual = TradeQualification(
        qualified=True,
        final_action=DecisionAction.ENTER_LONG,
        final_entry_type=None,
        block_reasons=[],
        final_direction="long",
        source_score=88.0,
        source_band=ScoreBand.FULL_85_PLUS,
        timestamp=_now(),
    )
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_INVALID_QUALIFICATION


# ============================================================== 14. risk_pct boundary


def test_daily_loss_at_exact_threshold_blocks() -> None:
    """Exactly at the threshold the rule triggers (>= comparison)."""

    rm = RiskManager(
        settings=make_settings(risk_max_daily=0.04),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    rm.record_pnl(pnl=Decimal("-400"), now=_now())  # exactly 4 %
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert not v.approved
    assert v.blocked_reason == REASON_DAILY_LOSS_LIMIT


# ============================================================== 15. multiple blocks - daily wins first


def test_block_priority_daily_before_weekly() -> None:
    """Daily loss limit takes priority over weekly (in the check order)."""

    rm = RiskManager(
        settings=make_settings(risk_max_daily=0.04, risk_max_weekly=0.08),
        get_account=lambda: make_account(equity=Decimal("10000")),
        get_positions=_empty_positions,
    )
    # 5 % daily (over 4 % limit), 5 % weekly (under 8 % limit).
    rm.record_pnl(pnl=Decimal("-500"), now=_now())
    qual = make_qualification()
    v = rm.approve(qual, now=_now())
    assert v.blocked_reason == REASON_DAILY_LOSS_LIMIT


# ============================================================== 16. id / record_pnl determinism


def test_record_pnl_accumulates() -> None:
    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    rm.record_pnl(pnl=Decimal("-100"), now=_now())
    rm.record_pnl(pnl=Decimal("50"), now=_now())
    rm.record_pnl(pnl=Decimal("-25"), now=_now())
    assert rm.state.daily_pnl == Decimal("-75")
    assert rm.state.weekly_pnl == Decimal("-75")


def test_state_is_dataclass_like() -> None:
    """The state attribute is queryable (used by the journal / UI)."""

    rm = RiskManager(
        settings=make_settings(),
        get_account=lambda: make_account(),
        get_positions=_empty_positions,
    )
    state = rm.state
    assert state.daily_pnl == Decimal("0")
    assert state.weekly_pnl == Decimal("0")
    assert state.trades_today == 0
