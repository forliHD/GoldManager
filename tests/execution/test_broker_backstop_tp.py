"""The broker order's TP backstop is the FURTHEST target (tp3), not tp1.

Attaching tp1 for the full volume made the broker auto-close 100 % at TP1,
which pre-empted the bot-side 30/30/40 partials + runner. The submitted order
must carry the furthest target so the manage loop owns TP1/TP2 and the runner
rides to tp3.
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
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.execution.pipeline import ExecutionPipeline

_TS = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


class _FakeConnector:
    """Minimal IMarketConnector slice the entry pipeline touches."""

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
    """Captures the OrderRequest the pipeline submits."""

    def __init__(self) -> None:
        self.request = None

    def send(self, request, *, setup_id, engine_source, now):  # noqa: ANN001
        self.request = request
        return make_order_envelope(state="filled")


def _bundle() -> FeatureSnapshotBundle:
    # A long with a structure swing low below entry → a real SL; no liquidity /
    # volume_range, so TP1/TP2/TP3 fall back to the deterministic 1R/2R/3R.
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


def _decision() -> Decision:
    return Decision(
        action=DecisionAction.ENTER_LONG,
        source_score=88.0,
        source_band=ScoreBand.FULL_85_PLUS,
        source_direction="long",
        source_engine="rule",
        llm_intent=None,
        timestamp=_TS,
    )


def _score() -> Score:
    return Score(total_score=88.0, band=ScoreBand.FULL_85_PLUS, direction="long", timestamp=_TS)


def test_broker_tp_is_tp3_not_tp1():
    pipeline = ExecutionPipeline(make_settings(), _FakeConnector())
    rec = _RecordingOrderMgr()
    pipeline.order_mgr = rec  # intercept the submission

    entry = Decimal("4189.00")
    bundle = _bundle()
    outcome = pipeline.process(
        _decision(), _score(), make_qualification(), bundle, ref_price=entry, now=_TS,
    )
    assert outcome.submitted is True
    assert rec.request is not None

    # Recompute the plan with the pipeline's own managers (same config).
    stops = pipeline.stop_mgr.compute_initial(OrderSide.BUY, entry, bundle, now=_TS)
    tp_plan = pipeline.tp_mgr.compute(OrderSide.BUY, entry, stops.sl_price, bundle, now=_TS)

    # The backstop is the furthest target, and it is NOT tp1 (the old bug).
    assert rec.request.tp == tp_plan.tp3_price
    assert tp_plan.tp3_price != tp_plan.tp1_price
    assert rec.request.tp > tp_plan.tp1_price  # long → furthest target is higher
