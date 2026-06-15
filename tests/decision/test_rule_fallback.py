"""Tests for :class:`xauusd_bot.decision.rule_fallback.RuleBasedFallback`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.schemas.decision import (
    AggregatedFeatures,
    DecisionAction,
    EngineSubscore,
    EntryType,
    Score,
    ScoreBand,
)
from xauusd_bot.decision._weights import ENGINE_WEIGHTS
from xauusd_bot.decision.rule_fallback import (
    REASON_BAND_BELOW,
    REASON_BAND_OBSERVE,
    REASON_NEUTRAL_DIRECTION,
    REASON_NEWS_BLACKOUT,
    REASON_NO_CLEAR_DIRECTION,
    REASON_RISK_LIMIT_REACHED,
    REASON_SPREAD_TOO_WIDE,
    RuleBasedFallback,
)


# ---------------------------------------------------------------- inline factories


def make_settings(**overrides) -> Settings:
    base = {
        "redis_url": "redis://localhost:6379/0",
        "timescaledb_url": "postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd",
        "environment": "test",
        "risk_max_daily": 0.04,
        "risk_max_weekly": 0.08,
        "spread_max_pips": 3.0,
    }
    base.update(overrides)
    return Settings(**base)


def make_subscore(
    name: str,
    *,
    value: float = 50.0,
    weight: float = 10.0,
    direction_bias: int = 0,
    reasoning: str = "test",
) -> EngineSubscore:
    return EngineSubscore(
        name=name,
        raw=value,
        value=value,
        percentile=50.0,
        weight=weight,
        direction_bias=direction_bias,  # type: ignore[arg-type]
        reasoning=reasoning,
    )


def make_aggregated(
    *,
    subscores: dict[str, EngineSubscore] | None = None,
    has_data: bool = True,
) -> AggregatedFeatures:
    if subscores is None:
        subscores = {
            name: make_subscore(name, weight=ENGINE_WEIGHTS[name], value=50.0)
            for name in ENGINE_WEIGHTS
        }
    return AggregatedFeatures(
        ts=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        symbol="XAUUSD",
        subscores=subscores,
        conflicts=[],
        dominant_engine=None,
        has_data=has_data,
    )


def make_score(
    *,
    total: float = 70.0,
    band: ScoreBand = ScoreBand.PREPARE_65_74,
    direction: str = "long",
) -> Score:
    return Score(
        total_score=total,
        subscores={n: 50.0 for n in ENGINE_WEIGHTS},
        band=band,
        reasoning=[],
        direction=direction,  # type: ignore[arg-type]
        timestamp=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
    )


def make_account(**overrides) -> "object":  # type: ignore[name-defined]
    from xauusd_bot.connectors.schemas import AccountInfo

    base = dict(
        login=1,
        broker="x",
        balance=Decimal("10000"),
        equity=Decimal("10000"),
        margin=Decimal("0"),
        free_margin=Decimal("10000"),
        server_time=datetime(2026, 4, 15, 13, 30, tzinfo=UTC),
        daily_pnl=None,
        weekly_pnl=None,
        current_spread=None,
    )
    base.update(overrides)
    return AccountInfo(**base)


# ---------------------------------------------------------------- tests


class TestRuleOrder:
    """Rules are evaluated in a strict order; first blocker wins."""

    def test_news_blackout_blocks_first(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        # Even with a perfect score, blackout wins.
        score = make_score(total=99.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=0.0, weight=10.0, reasoning="in_blackout"),
                **{n: make_subscore(n, value=100.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.action == DecisionAction.NO_TRADE
        assert d.block_reason == REASON_NEWS_BLACKOUT

    def test_risk_limit_blocks_after_news(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=99.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=100.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        account = make_account(daily_pnl=Decimal("-500"))  # 5% loss, daily limit 4%
        d = fb.decide(score, agg, account=account)
        assert d.block_reason == REASON_RISK_LIMIT_REACHED

    def test_weekly_pnl_breach_also_blocks(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=99.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=100.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        account = make_account(weekly_pnl=Decimal("-900"))  # 9% loss, weekly limit 8%
        d = fb.decide(score, agg, account=account)
        assert d.block_reason == REASON_RISK_LIMIT_REACHED

    def test_spread_too_wide_blocks(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=99.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=100.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        # 3.0 pips = 30 points; spread_max_pips=3.0 → max_points=30.
        # 35 points > 30 → block.
        account = make_account(current_spread=Decimal("35"))
        d = fb.decide(score, agg, account=account)
        assert d.block_reason == REASON_SPREAD_TOO_WIDE

    def test_band_below_55_blocks(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=50.0, band=ScoreBand.BELOW_55, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=50.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.block_reason == REASON_BAND_BELOW

    def test_band_observe_blocks(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=60.0, band=ScoreBand.OBSERVE_55_64, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=60.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.block_reason == REASON_BAND_OBSERVE

    def test_no_clear_direction_blocks(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=70.0, band=ScoreBand.PREPARE_65_74, direction="long")
        # All engines neutral bias → |long - short| < 10 → no_clear_direction
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=70.0, weight=ENGINE_WEIGHTS[n], direction_bias=0)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.block_reason == REASON_NO_CLEAR_DIRECTION

    def test_neutral_direction_blocks(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=70.0, band=ScoreBand.PREPARE_65_74, direction="neutral")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=70.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.block_reason == REASON_NEUTRAL_DIRECTION


class TestEntryDecisions:
    def test_full_band_with_long_direction(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=90.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=90.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.action == DecisionAction.ENTER_LONG
        assert d.entry_type == EntryType.FULL
        assert d.block_reason is None

    def test_reduced_band_with_long_direction(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=80.0, band=ScoreBand.REDUCED_75_84, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=80.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.action == DecisionAction.ENTER_LONG
        assert d.entry_type == EntryType.REDUCED

    def test_prepare_band_with_short_direction(self) -> None:
        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=70.0, band=ScoreBand.PREPARE_65_74, direction="short")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=70.0, weight=ENGINE_WEIGHTS[n], direction_bias=-1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=make_account())
        assert d.action == DecisionAction.ENTER_SHORT
        assert d.entry_type == EntryType.SCOUT

    def test_no_account_degrades_gracefully(self) -> None:
        """account=None → no risk/spread checks fire (unknown → no block)."""

        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=90.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=90.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        d = fb.decide(score, agg, account=None)
        assert d.action == DecisionAction.ENTER_LONG
        assert d.entry_type == EntryType.FULL

    def test_unknown_pnl_does_not_block(self) -> None:
        """daily_pnl/weekly_pnl=None → no risk-limit check fires."""

        fb = RuleBasedFallback(settings=make_settings())
        score = make_score(total=90.0, band=ScoreBand.FULL_85_PLUS, direction="long")
        agg = make_aggregated(
            subscores={
                "news": make_subscore("news", value=80.0, weight=10.0, reasoning="clean"),
                **{n: make_subscore(n, value=90.0, weight=ENGINE_WEIGHTS[n], direction_bias=1)
                   for n in ENGINE_WEIGHTS if n != "news"},
            }
        )
        # PnL None, spread None → no blocks
        d = fb.decide(score, agg, account=make_account())
        assert d.action == DecisionAction.ENTER_LONG
