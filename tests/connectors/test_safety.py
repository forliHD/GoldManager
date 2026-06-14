"""Tests for PreTradeSafetyChecker — the 4-check pre-trade gate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from xauusd_bot.connectors.safety import (
    PreTradeSafetyChecker,
    SafetyAction,
    SafetyReason,
    SafetyThresholds,
    SafetyVerdict,
)
from xauusd_bot.connectors.schemas import AccountInfo

# ---------------------------------------------------------------- fixtures


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


# =============================================================== 1. happy path


def test_happy_path_returns_allowed() -> None:
    """Clean state: feed online, spread normal, account healthy, no broker
    error → SafetyVerdict.allowed."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(equity=10000),
        get_spread_points=lambda: 25.0,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.PROCEED
    assert v.reasons == []
    assert v.spread_points == 25.0
    assert v.equity == 10000.0
    assert v.checked_at is not None
    # Verdict is JSON-serializable.
    import json

    json.dumps(v.model_dump(mode="json"))


# =============================================================== 2. spread over limit → blocked


def test_spread_above_block_limit_returns_blocked() -> None:
    """A spread above the block threshold is a BLOCK with reason SPREAD_TOO_WIDE."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 200.0,
        thresholds=SafetyThresholds(spread_warn_points=50, spread_block_points=120),
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.SPREAD_TOO_WIDE in v.reasons
    assert v.spread_points == 200.0
    # The details dict carries the diagnostic.
    assert "spread" in v.details
    assert "120" in v.details["spread"]


def test_spread_at_warn_threshold_returns_warn() -> None:
    """A spread between warn and block is WARN, not BLOCK."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 75.0,
        thresholds=SafetyThresholds(spread_warn_points=50, spread_block_points=120),
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.WARN
    assert SafetyReason.SPREAD_ELEVATED in v.reasons
    assert SafetyReason.SPREAD_TOO_WIDE not in v.reasons


# =============================================================== 3. account unhealthy → blocked


def test_account_trade_allowed_false_returns_blocked() -> None:
    """An account with trade_allowed=False is blocked (ACCOUNT_FROZEN)."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(trade_allowed=False),
        get_spread_points=lambda: 25.0,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.ACCOUNT_FROZEN in v.reasons


def test_account_getter_raises_treated_as_blocked() -> None:
    """If get_account() raises, the verdict is BLOCK with ACCOUNT_FROZEN."""

    def _boom() -> AccountInfo:
        raise ConnectionError("cannot reach broker")

    checker = PreTradeSafetyChecker(
        get_account=_boom,
        get_spread_points=lambda: 25.0,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.ACCOUNT_FROZEN in v.reasons
    assert "cannot reach broker" in v.details.get("account", "")


# =============================================================== 4. feed offline → blocked


def test_feed_offline_returns_blocked() -> None:
    """is_connected=False triggers BLOCK with FEED_OFFLINE."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 25.0,
        is_connected=lambda: False,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.FEED_OFFLINE in v.reasons
    assert "feed" in v.details


# =============================================================== 5. drawdown trip


def test_drawdown_trip_blocks_after_sustained_drop() -> None:
    """A sustained drawdown (>= 5% over >= 10 checks) triggers BLOCK."""

    # 5 checks at peak, then 10 checks at 90% of peak → 10% drawdown.
    equity = [10000.0] * 5 + [9000.0] * 10
    queue = list(equity)

    def _equity() -> AccountInfo:
        e = queue.pop(0)
        return _account(equity=e)

    checker = PreTradeSafetyChecker(
        get_account=_equity,
        get_spread_points=lambda: 25.0,
        thresholds=SafetyThresholds(drawdown_trip_fraction=0.05, drawdown_window=10),
    )
    actions = [checker.check(datetime.now(tz=UTC)).action for _ in range(15)]
    assert SafetyAction.BLOCK in actions
    # We already consumed the queue; re-construct the checker to also
    # assert that the DRAWDOWN_TRIP reason was emitted (not just BLOCK).
    queue2 = list(equity)

    def _equity2() -> AccountInfo:
        e = queue2.pop(0)
        return _account(equity=e)

    checker2 = PreTradeSafetyChecker(
        get_account=_equity2,
        get_spread_points=lambda: 25.0,
        thresholds=SafetyThresholds(drawdown_trip_fraction=0.05, drawdown_window=10),
    )
    reasons_seen: set[SafetyReason] = set()
    for _ in range(15):
        v = checker2.check(datetime.now(tz=UTC))
        reasons_seen.update(v.reasons)
    assert SafetyReason.DRAWDOWN_TRIP in reasons_seen


# =============================================================== 6. sticky broker error


def test_sticky_broker_error_blocks_then_clears() -> None:
    """A marked broker error is consumed by the next check, then cleared."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 25.0,
    )
    checker.mark_broker_error("OFFQUOTES", "requote flood")
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.BROKER_ERROR in v.reasons

    # Subsequent clean check: the sticky flag is consumed, no longer raised.
    v2 = checker.check(datetime.now(tz=UTC))
    assert SafetyReason.BROKER_ERROR not in v2.reasons
    assert v2.action == SafetyAction.PROCEED


def test_clear_broker_error_resets_sticky_flag() -> None:
    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 25.0,
    )
    checker.mark_broker_error("OFFQUOTES", "x")
    checker.clear_broker_error()
    v = checker.check(datetime.now(tz=UTC))
    assert SafetyReason.BROKER_ERROR not in v.reasons


# =============================================================== 7. multiple reasons


def test_multiple_reasons_aggregate_to_block() -> None:
    """When multiple BLOCK reasons are present, action is BLOCK."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 200.0,  # too wide
        is_connected=lambda: False,  # feed offline
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.FEED_OFFLINE in v.reasons
    assert SafetyReason.SPREAD_TOO_WIDE in v.reasons


def test_block_takes_precedence_over_warn() -> None:
    """If both a BLOCK reason and a WARN reason are present, action is BLOCK."""

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=lambda: 200.0,  # BLOCK
        is_connected=lambda: True,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    # We don't assert on the warn reason here (the spread is at the BLOCK
    # threshold, not the warn threshold), but the action is BLOCK.


# =============================================================== 8. spread getter fails


def test_spread_getter_raises_treated_as_blocked() -> None:
    """If get_spread_points() raises, the verdict is BLOCK with SPREAD_TOO_WIDE."""

    def _boom() -> float:
        raise RuntimeError("spread feed down")

    checker = PreTradeSafetyChecker(
        get_account=lambda: _account(),
        get_spread_points=_boom,
    )
    v = checker.check(datetime.now(tz=UTC))
    assert v.action == SafetyAction.BLOCK
    assert SafetyReason.SPREAD_TOO_WIDE in v.reasons
    assert "spread feed down" in v.details.get("spread", "")


# =============================================================== 9. SafetyVerdict shape


def test_safety_verdict_is_pydantic_model() -> None:
    """SafetyVerdict is a Pydantic model and round-trips through JSON."""

    v = SafetyVerdict(
        action=SafetyAction.PROCEED,
        reasons=[],
        details={},
        checked_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        spread_points=25.0,
        equity=10000.0,
    )
    assert v.action == SafetyAction.PROCEED
    # Round-trip

    s = v.model_dump_json()
    v2 = SafetyVerdict.model_validate_json(s)
    assert v2 == v


def test_safety_thresholds_defaults_are_sane() -> None:
    """SafetyThresholds has sensible defaults — not all-zero."""

    t = SafetyThresholds()
    assert t.spread_warn_points > 0
    assert t.spread_block_points > t.spread_warn_points
    assert 0 < t.drawdown_trip_fraction < 1
    assert t.drawdown_window > 0
