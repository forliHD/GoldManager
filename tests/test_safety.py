"""Tests for PreTradeSafetyChecker."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from xauusd_bot.common.logging import setup_logging
from xauusd_bot.connectors.safety import (
    PreTradeSafetyChecker,
    SafetyAction,
    SafetyReason,
    SafetyThresholds,
)
from xauusd_bot.connectors.schemas import AccountInfo


def _account(equity: float = 10000.0, trade_allowed: bool = True) -> AccountInfo:
    return AccountInfo(
        login="replay",
        broker="replay",
        balance=Decimal("10000"),
        equity=Decimal(str(equity)),
        margin=Decimal("0"),
        free_margin=Decimal(str(equity)),
        leverage=100,
        server_time=datetime.now(tz=UTC),
        trade_allowed=trade_allowed,
    )


def test_proceed_on_clean_state() -> None:
    setup_logging(level="WARNING")
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(equity=10000),
        get_spread_points=lambda: 25.0,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.PROCEED
    assert v.reasons == []


def test_block_on_feed_offline() -> None:
    setup_logging(level="WARNING")
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 25.0,
        is_connected=lambda: False,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.FEED_OFFLINE in v.reasons


def test_block_on_spread_too_wide() -> None:
    setup_logging(level="WARNING")
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 200.0,
        thresholds=SafetyThresholds(spread_warn_points=50, spread_block_points=120),
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.SPREAD_TOO_WIDE in v.reasons


def test_warn_on_elevated_spread() -> None:
    setup_logging(level="WARNING")
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 75.0,
        thresholds=SafetyThresholds(spread_warn_points=50, spread_block_points=120),
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.WARN
    assert SafetyReason.SPREAD_ELEVATED in v.reasons


def test_block_on_account_frozen() -> None:
    setup_logging(level="WARNING")
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(trade_allowed=False),
        get_spread_points=lambda: 25.0,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.ACCOUNT_FROZEN in v.reasons


def test_block_on_drawdown_trip() -> None:
    setup_logging(level="WARNING")
    # After peak of 10000, equity drops to 9000 (10%) — should trip.
    equity = [10000.0] * 5 + [9000.0] * 10

    def get_acct() -> AccountInfo:
        e = equity.pop(0)
        return _account(equity=e)

    checker = PreTradeSafetyChecker(
        get_account=get_acct,
        get_spread_points=lambda: 25.0,
        thresholds=SafetyThresholds(drawdown_trip_fraction=0.05, drawdown_window=10),
    )
    actions = [checker.check(datetime.now(tz=UTC)).action for _ in range(15)]
    # Some check in the second half should be BLOCK.
    assert SafetyAction.BLOCK in actions


def test_sticky_broker_error_clears_on_success() -> None:
    setup_logging(level="WARNING")
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 25.0,
    )
    checker.mark_broker_error("OFFQUOTES", "requote flood")
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.BROKER_ERROR in v.reasons

    # Subsequent clean check clears the sticky flag.
    v2 = checker.check(datetime.now(tz=UTC))
    assert SafetyReason.BROKER_ERROR not in v2.reasons
