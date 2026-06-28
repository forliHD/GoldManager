"""Entry-zone gate — honour the LLM's proposed entry zone instead of
chasing at the signal-bar price.

Covers the pure :func:`check_entry_zone` helper (all sides / bounds / tol)
and its wiring into the live :class:`ExecutionPipeline`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tests._execution_factories import (
    make_account,
    make_order_envelope,
    make_qualification,
    make_settings,
    make_symbol_spec,
)
from xauusd_bot.common.schemas.decision import (
    Decision,
    DecisionAction,
    LLMIntent,
    Score,
    ScoreBand,
)
from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    MarketStructureOutput,
    SessionEngineOutput,
    SessionName,
    SwingPoint,
)
from xauusd_bot.execution.entry_gate import (
    ENTRY_ABOVE_ZONE,
    ENTRY_BELOW_ZONE,
    check_entry_zone,
)
from xauusd_bot.execution.pipeline import ExecutionPipeline

_TS = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


# ============================================================ pure helper


def test_long_above_zone_is_blocked():
    assert check_entry_zone(is_long=True, price=4189.0, entry_min=4180.0, entry_max=4185.0) == ENTRY_ABOVE_ZONE


def test_long_inside_zone_is_allowed():
    assert check_entry_zone(is_long=True, price=4183.0, entry_min=4180.0, entry_max=4185.0) is None


def test_long_below_zone_is_allowed_deeper_discount():
    # A long BELOW the proposed zone is a better entry, never punished.
    assert check_entry_zone(is_long=True, price=4175.0, entry_min=4180.0, entry_max=4185.0) is None


def test_long_at_upper_bound_is_allowed():
    # Exactly at entry_max is in-zone (strict >, not >=).
    assert check_entry_zone(is_long=True, price=4185.0, entry_min=4180.0, entry_max=4185.0) is None


def test_short_below_zone_is_blocked():
    assert check_entry_zone(is_long=False, price=4175.0, entry_min=4180.0, entry_max=4185.0) == ENTRY_BELOW_ZONE


def test_short_inside_zone_is_allowed():
    assert check_entry_zone(is_long=False, price=4182.0, entry_min=4180.0, entry_max=4185.0) is None


def test_short_above_zone_is_allowed_higher_premium():
    assert check_entry_zone(is_long=False, price=4190.0, entry_min=4180.0, entry_max=4185.0) is None


def test_none_bound_disables_that_side():
    # Long with no entry_max → cannot be "above the zone" → never blocked.
    assert check_entry_zone(is_long=True, price=9999.0, entry_min=4180.0, entry_max=None) is None
    # Short with no entry_min → never blocked.
    assert check_entry_zone(is_long=False, price=0.0, entry_min=None, entry_max=4185.0) is None


def test_both_none_is_noop():
    assert check_entry_zone(is_long=True, price=4189.0, entry_min=None, entry_max=None) is None


def test_tolerance_allows_a_hair_past_the_bound():
    # 4185.4 is 0.4 above entry_max → blocked at tol=0, allowed at tol=0.5.
    assert check_entry_zone(is_long=True, price=4185.4, entry_min=4180.0, entry_max=4185.0) == ENTRY_ABOVE_ZONE
    assert check_entry_zone(is_long=True, price=4185.4, entry_min=4180.0, entry_max=4185.0, tol=0.5) is None


def test_negative_tolerance_is_treated_as_zero():
    assert check_entry_zone(is_long=True, price=4185.4, entry_min=4180.0, entry_max=4185.0, tol=-5.0) == ENTRY_ABOVE_ZONE


# ============================================================ pipeline wiring


class _FakeConnector:
    def __init__(self) -> None:
        self._spec = make_symbol_spec()
        self._account = make_account(balance=Decimal("10000"))

    def get_symbol_spec(self, symbol):  # noqa: ANN001
        return self._spec

    def get_account(self):
        return self._account

    def is_connected(self) -> bool:
        return True

    def positions_get(self, symbol=None):  # noqa: ANN001
        return []

    def pending_get(self, symbol=None):  # noqa: ANN001
        return []

    def order_cancel(self, order_id):  # noqa: ANN001
        return None


class _RecordingOrderMgr:
    def __init__(self) -> None:
        self.request = None

    def send(self, request, *, setup_id, engine_source, now):  # noqa: ANN001
        self.request = request
        return make_order_envelope(state="filled")


def _bundle() -> FeatureSnapshotBundle:
    return FeatureSnapshotBundle(
        ts=_TS,
        atr=0.5,
        structure=MarketStructureOutput(
            swings=[SwingPoint(kind="low", price=4185.0, time=_TS, bar_index=5, is_external=True)],
            last_bos=None, last_choch=None, liquidity_pools=[], trend="up", fractal_n=3,
        ),
        session=SessionEngineOutput(
            current_session=SessionName.LONDON,
            session_start=_TS, session_end=_TS,
            session_progress_pct=50.0, session_risk_factor=1.0,
        ),
    )


def _decision(intent: LLMIntent | None) -> Decision:
    return Decision(
        action=DecisionAction.ENTER_LONG,
        source_score=88.0,
        source_band=ScoreBand.FULL_85_PLUS,
        source_direction="long",
        source_engine="ai",
        llm_intent=intent,
        timestamp=_TS,
    )


def _score() -> Score:
    return Score(total_score=88.0, band=ScoreBand.FULL_85_PLUS, direction="long", timestamp=_TS)


def _run(settings, intent, ref_price):  # noqa: ANN001
    pipeline = ExecutionPipeline(settings, _FakeConnector())
    pipeline.order_mgr = _RecordingOrderMgr()
    return pipeline.process(
        _decision(intent), _score(), make_qualification(), _bundle(),
        ref_price=Decimal(str(ref_price)), now=_TS,
    )


def test_pipeline_blocks_long_above_entry_zone():
    intent = LLMIntent(entry_min=4180.0, entry_max=4185.0)
    out = _run(make_settings(), intent, ref_price=4189.0)
    assert out.submitted is False
    assert out.blocked_reason == ENTRY_ABOVE_ZONE


def test_pipeline_allows_long_inside_entry_zone():
    intent = LLMIntent(entry_min=4187.0, entry_max=4191.0)
    out = _run(make_settings(), intent, ref_price=4189.0)
    assert out.submitted is True


def test_pipeline_allows_long_below_entry_zone_deeper_discount():
    # ref 4189 is below the proposed [4191, 4193] zone → a better long entry.
    intent = LLMIntent(entry_min=4191.0, entry_max=4193.0)
    out = _run(make_settings(), intent, ref_price=4189.0)
    assert out.submitted is True


def test_pipeline_gate_disabled_lets_the_chase_through():
    intent = LLMIntent(entry_min=4180.0, entry_max=4185.0)
    out = _run(make_settings(entry_zone_gate_enabled=False), intent, ref_price=4189.0)
    # Gate off → the above-zone chase is NOT blocked by the gate.
    assert out.blocked_reason != ENTRY_ABOVE_ZONE


def test_pipeline_no_intent_is_unchanged():
    out = _run(make_settings(), None, ref_price=4189.0)
    assert out.submitted is True
