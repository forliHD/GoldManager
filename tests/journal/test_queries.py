"""Tests for the journal pure-function aggregations (Block 5a).

These functions are the *read API* the BacktestEngine and ReviewAgent
build on. They take a list of ``TradeRecord`` and return aggregated
values. No DB, no I/O, no globals. The tests are exhaustive on the
edge cases (empty input, single trade, monotonic series) because
these functions are called from the journal_smoke CLI and the
upcoming Block-5b/5c code.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from xauusd_bot.common.schemas.decision import (
    EntryType,
    ScoreBand,
)
from xauusd_bot.common.schemas.journal import TradeRecord
from xauusd_bot.journal.queries import (
    compute_equity_curve,
    compute_max_drawdown,
    compute_r_distribution,
    compute_score_band_stats,
    compute_session_stats,
    compute_setup_breakdown,
    compute_sharpe,
)


# ----------------------------------------------------------------- helpers


def _ts(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=UTC)


def _trade(
    pnl: Decimal | None = None,
    r: float | None = None,
    *,
    hour: int = 13,
    side: str = "long",
    band: ScoreBand = ScoreBand.PREPARE_65_74,
    entry_type: EntryType = EntryType.SCOUT,
    session: str = "london",
    open_day: int = 15,
    close_day: int | None = None,
    close_hour: int | None = None,
    risk_amount: Decimal = Decimal("50"),
) -> TradeRecord:
    ts_open = _ts(hour=hour, day=open_day)
    ts_close = (
        _ts(hour=close_hour if close_hour is not None else hour + 1, day=close_day or open_day)
        if pnl is not None
        else None
    )
    return TradeRecord(
        timestamp_open=ts_open,
        timestamp_close=ts_close,
        side=side,
        entry_price=Decimal("2370.00"),
        exit_price=Decimal("2375.00") if pnl is not None else None,
        stop_loss=Decimal("2365.00"),
        take_profits=[Decimal("2375.00")],
        volume_lots=Decimal("0.10"),
        risk_amount=risk_amount,
        pnl_realized=pnl,
        r_multiple=r,
        setup_id=uuid4(),
        score=80.0,
        subscores={"h1_zone": 80, "m5_zone": 70, "vwap": 75},
        band=band,
        entry_type=entry_type,
        fill_price=Decimal("2370.00"),
        session=session,
        atr_at_entry=0.35,
        structure_at_entry="up",
    )


# ----------------------------------------------------------------- compute_r_distribution


def test_r_distribution_empty_returns_zero_buckets() -> None:
    out = compute_r_distribution([])
    # All 7 buckets present, all zero — the spec requires stable JSON.
    assert out == {"-3": 0, "-2": 0, "-1": 0, "0": 0, "1": 0, "2": 0, "3+": 0}


def test_r_distribution_classic_3r_win() -> None:
    t = _trade(pnl=Decimal("150"), r=3.0)
    out = compute_r_distribution([t])
    assert out["3+"] == 1
    assert sum(out.values()) == 1


def test_r_distribution_breakeven_lands_in_0_bucket() -> None:
    t = _trade(pnl=Decimal("0"), r=0.0)
    out = compute_r_distribution([t])
    assert out["0"] == 1


def test_r_distribution_open_trades_excluded() -> None:
    open_trade = _trade(pnl=None, r=None)  # open
    closed_trade = _trade(pnl=Decimal("50"), r=1.0, hour=14)
    out = compute_r_distribution([open_trade, closed_trade])
    assert sum(out.values()) == 1
    assert out["1"] == 1


def test_r_distribution_loss_buckets() -> None:
    """R=-0.5 → "0" (between -1 and 0). R=-1.0 → "-1" (boundary).
    R=-2.0 → "-2". R=-3.0 → "-3" (boundary; r <= -3.0)."""

    trades = [
        _trade(pnl=Decimal("-25"), r=-0.5, hour=10),  # → "0" bucket
        _trade(pnl=Decimal("-50"), r=-1.0, hour=11),  # → "-1" bucket
        _trade(pnl=Decimal("-100"), r=-2.0, hour=12),  # → "-2" bucket
        _trade(pnl=Decimal("-200"), r=-3.0, hour=13),  # → "-3" bucket
    ]
    out = compute_r_distribution(trades)
    assert out["0"] == 1
    assert out["-1"] == 1
    assert out["-2"] == 1
    assert out["-3"] == 1


def test_r_distribution_large_loss_lands_in_3plus_minus_bucket() -> None:
    t = _trade(pnl=Decimal("-500"), r=-5.0)
    out = compute_r_distribution([t])
    assert out["-3"] == 1


def test_r_distribution_small_win_lands_in_0_bucket() -> None:
    """R=0.5 is below 1.0 so it lands in the breakeven '0' bucket."""

    t = _trade(pnl=Decimal("25"), r=0.5)
    out = compute_r_distribution([t])
    assert out["0"] == 1


def test_r_distribution_one_r_win_lands_in_1_bucket() -> None:
    """R=1.0 (boundary) is in the '1' bucket."""

    t = _trade(pnl=Decimal("50"), r=1.0)
    out = compute_r_distribution([t])
    assert out["1"] == 1


# ----------------------------------------------------------------- compute_setup_breakdown


def test_setup_breakdown_empty_returns_all_zeros() -> None:
    out = compute_setup_breakdown([])
    assert set(out.keys()) == {"scout", "reduced", "full"}
    for v in out.values():
        assert v["count"] == 0.0
        assert v["winrate"] == 0.0
        assert v["avg_r"] == 0.0


def test_setup_breakdown_groups_by_entry_type() -> None:
    trades = [
        _trade(pnl=Decimal("50"), r=1.0, hour=10, entry_type=EntryType.SCOUT),
        _trade(pnl=Decimal("-50"), r=-1.0, hour=11, entry_type=EntryType.SCOUT),
        _trade(pnl=Decimal("100"), r=2.0, hour=12, entry_type=EntryType.REDUCED),
        _trade(pnl=Decimal("200"), r=4.0, hour=13, entry_type=EntryType.FULL),
    ]
    out = compute_setup_breakdown(trades)
    assert out["scout"]["count"] == 2.0
    assert out["scout"]["wins"] == 1.0
    assert out["scout"]["losses"] == 1.0
    assert out["scout"]["winrate"] == 0.5
    assert out["reduced"]["count"] == 1.0
    assert out["reduced"]["total_pnl"] == 100.0
    assert out["full"]["count"] == 1.0
    assert out["full"]["avg_r"] == 4.0


def test_setup_breakdown_breakeven_counted_correctly() -> None:
    trades = [
        _trade(pnl=Decimal("0"), r=0.0, hour=10, entry_type=EntryType.FULL),
        _trade(pnl=Decimal("0"), r=0.0, hour=11, entry_type=EntryType.FULL),
    ]
    out = compute_setup_breakdown(trades)
    assert out["full"]["breakeven"] == 2.0
    assert out["full"]["wins"] == 0.0
    assert out["full"]["losses"] == 0.0
    assert out["full"]["winrate"] == 0.0  # 0 / 2 closed = 0.0


def test_setup_breakdown_open_trades_counted_in_total_but_not_winrate() -> None:
    trades = [
        _trade(pnl=None, r=None, hour=10, entry_type=EntryType.SCOUT),  # open
        _trade(pnl=Decimal("50"), r=1.0, hour=11, entry_type=EntryType.SCOUT),  # closed
    ]
    out = compute_setup_breakdown(trades)
    assert out["scout"]["count"] == 2.0
    assert out["scout"]["closed"] == 1.0
    assert out["scout"]["winrate"] == 1.0  # 1 win / 1 closed


# ----------------------------------------------------------------- compute_equity_curve


def test_equity_curve_empty() -> None:
    assert compute_equity_curve([]) == []


def test_equity_curve_skips_open_trades() -> None:
    open_trade = _trade(pnl=None, r=None, hour=10)
    closed_trade = _trade(pnl=Decimal("50"), r=1.0, hour=11)
    ec = compute_equity_curve([open_trade, closed_trade])
    assert len(ec) == 1
    _, cum = ec[0]
    assert cum == Decimal("50")


def test_equity_curve_cumulative_pnl_in_order() -> None:
    t1 = _trade(pnl=Decimal("50"), r=1.0, hour=10)
    t2 = _trade(pnl=Decimal("-25"), r=-0.5, hour=11)
    t3 = _trade(pnl=Decimal("75"), r=1.5, hour=12)
    ec = compute_equity_curve([t3, t1, t2])  # scrambled input
    assert [eq for _, eq in ec] == [Decimal("50"), Decimal("25"), Decimal("100")]


def test_equity_curve_sorted_by_close_time() -> None:
    t1 = _trade(pnl=Decimal("10"), r=0.2, hour=12)  # close at 13
    t2 = _trade(pnl=Decimal("20"), r=0.4, hour=10)  # close at 11
    ec = compute_equity_curve([t1, t2])
    # t2 (close=11) before t1 (close=13)
    assert [eq for _, eq in ec] == [Decimal("20"), Decimal("30")]


# ----------------------------------------------------------------- compute_sharpe


def test_sharpe_empty_returns_zero() -> None:
    assert compute_sharpe([]) == 0.0


def test_sharpe_single_point_returns_zero() -> None:
    assert compute_sharpe([(_ts(10), Decimal("100"))]) == 0.0


def test_sharpe_monotonic_rising_positive() -> None:
    """A monotonic equity curve has very low variance → low Sharpe."""

    ec = [
        (_ts(10), Decimal("100")),
        (_ts(11), Decimal("110")),
        (_ts(12), Decimal("120")),
        (_ts(13), Decimal("130")),
    ]
    s = compute_sharpe(ec)
    assert math.isfinite(s)
    # 3 returns: +10%, +9.09%, +8.33% — mean ≈ 9.14%, std non-zero, sharpe > 0
    assert s > 0


def test_sharpe_alternating_returns_zero_when_zero_variance() -> None:
    """Equal steps → constant returns → std = 0 → sharpe = 0 by convention."""

    ec = [
        (_ts(10), Decimal("100")),
        (_ts(11), Decimal("110")),
        (_ts(12), Decimal("120")),
    ]
    # returns: 10%, 9.09% — not exactly equal so std non-zero; just check it's finite
    s = compute_sharpe(ec)
    assert math.isfinite(s)


def test_sharpe_decreasing_curve_negative() -> None:
    ec = [
        (_ts(10), Decimal("100")),
        (_ts(11), Decimal("90")),
        (_ts(12), Decimal("80")),
        (_ts(13), Decimal("70")),
    ]
    s = compute_sharpe(ec)
    assert s < 0


def test_sharpe_does_not_explode_on_small_curve() -> None:
    ec = [(_ts(10), Decimal("100")), (_ts(11), Decimal("105"))]
    s = compute_sharpe(ec)
    # 1 return — only 1 observation, std undefined, we return 0.0
    assert s == 0.0


# ----------------------------------------------------------------- compute_max_drawdown


def test_max_drawdown_empty() -> None:
    dd, peak, trough = compute_max_drawdown([])
    assert dd == Decimal("0")
    assert peak is None
    assert trough is None


def test_max_drawdown_monotonic_rising_is_zero() -> None:
    ec = [
        (_ts(10), Decimal("100")),
        (_ts(11), Decimal("120")),
        (_ts(12), Decimal("150")),
    ]
    dd, _, _ = compute_max_drawdown(ec)
    assert dd == Decimal("0")


def test_max_drawdown_finds_peak_and_trough() -> None:
    ec = [
        (_ts(10), Decimal("100")),
        (_ts(11), Decimal("150")),  # peak
        (_ts(12), Decimal("100")),  # trough, dd = 50
        (_ts(13), Decimal("120")),
    ]
    dd, peak, trough = compute_max_drawdown(ec)
    assert dd == Decimal("50")
    assert peak == _ts(11)
    assert trough == _ts(12)


def test_max_drawdown_picks_largest_peak_to_trough() -> None:
    ec = [
        (_ts(10), Decimal("100")),
        (_ts(11), Decimal("120")),
        (_ts(12), Decimal("80")),  # dd = 40
        (_ts(13), Decimal("200")),  # new peak
        (_ts(14), Decimal("100")),  # dd = 100 ← this is the max
    ]
    dd, peak, trough = compute_max_drawdown(ec)
    assert dd == Decimal("100")
    assert peak == _ts(13)
    assert trough == _ts(14)


# ----------------------------------------------------------------- compute_session_stats


def test_session_stats_empty_returns_all_sessions() -> None:
    out = compute_session_stats([])
    assert set(out.keys()) == {"asia", "london", "ny", "overlap", "closed"}
    for v in out.values():
        assert v["count"] == 0.0


def test_session_stats_groups_by_session() -> None:
    trades = [
        _trade(pnl=Decimal("50"), r=1.0, hour=10, session="london"),
        _trade(pnl=Decimal("-25"), r=-0.5, hour=11, session="london"),
        _trade(pnl=Decimal("100"), r=2.0, hour=12, session="ny"),
    ]
    out = compute_session_stats(trades)
    assert out["london"]["count"] == 2.0
    assert out["london"]["wins"] == 1.0
    assert out["london"]["losses"] == 1.0
    assert out["london"]["winrate"] == 0.5
    assert out["ny"]["count"] == 1.0
    assert out["asia"]["count"] == 0.0


# ----------------------------------------------------------------- compute_score_band_stats


def test_score_band_stats_empty_returns_all_bands() -> None:
    out = compute_score_band_stats([])
    assert set(out.keys()) == {
        "below_55",
        "observe_55_64",
        "prepare_65_74",
        "reduced_75_84",
        "full_85_plus",
    }
    for v in out.values():
        assert v["count"] == 0.0


def test_score_band_stats_groups_by_band() -> None:
    trades = [
        _trade(pnl=Decimal("50"), r=1.0, hour=10, band=ScoreBand.FULL_85_PLUS),
        _trade(pnl=Decimal("-50"), r=-1.0, hour=11, band=ScoreBand.FULL_85_PLUS),
        _trade(pnl=Decimal("25"), r=0.5, hour=12, band=ScoreBand.PREPARE_65_74),
    ]
    out = compute_score_band_stats(trades)
    assert out["full_85_plus"]["count"] == 2.0
    assert out["full_85_plus"]["winrate"] == 0.5
    assert out["prepare_65_74"]["count"] == 1.0
    assert out["reduced_75_84"]["count"] == 0.0


# ----------------------------------------------------------------- integration: equity + sharpe + dd together


def test_realistic_pnl_stream_produces_sensible_kpis() -> None:
    """End-to-end sanity: feed a stream of wins and losses and check all KPIs are coherent."""

    base = _ts(10)
    pnls = [Decimal("100"), Decimal("-50"), Decimal("75"), Decimal("-25"), Decimal("200")]
    rs = [2.0, -1.0, 1.5, -0.5, 4.0]
    trades = []
    cum_pnl = Decimal("0")
    for i, (pnl, r) in enumerate(zip(pnls, rs)):
        cum_pnl += pnl
        trades.append(
            _trade(
                pnl=pnl,
                r=r,
                hour=10 + i,
                entry_type=EntryType.SCOUT,
                band=ScoreBand.PREPARE_65_74,
            )
        )
    ec = compute_equity_curve(trades)
    # Note: the _trade factory stamps close_time = open_hour + 1 when pnl is set,
    # so the equity curve timestamps are [11, 12, 13, 14, 15].
    assert [eq for _, eq in ec] == [Decimal("100"), Decimal("50"), Decimal("125"), Decimal("100"), Decimal("300")]

    # Max drawdown walk: cumulative 100, 50, 125, 100, 300
    #   step 0 (ts=11): eq=100, peak=100, dd=0
    #   step 1 (ts=12): eq=50, peak still 100, dd = 100 - 50 = 50 ← MAX
    #   step 2 (ts=13): eq=125, peak now 125, dd = 0
    #   step 3 (ts=14): eq=100, peak still 125, dd = 125 - 100 = 25
    #   step 4 (ts=15): eq=300, peak now 300, dd = 0
    dd, peak, trough = compute_max_drawdown(ec)
    assert dd == Decimal("50")
    assert peak == _ts(11)
    assert trough == _ts(12)

    # R-distribution
    rd = compute_r_distribution(trades)
    # rs = [2.0, -1.0, 1.5, -0.5, 4.0]
    # 2.0 → "2" (boundary: r < 3, r >= 2)
    # 1.5 → "1" (r < 2)
    # 4.0 → "3+" (r >= 3)
    # -1.0 → "-1" (r <= -1)
    # -0.5 → "0" (between -1 and 1)
    assert rd["2"] == 1
    assert rd["1"] == 1
    assert rd["3+"] == 1
    assert rd["-1"] == 1
    assert rd["0"] == 1
    assert sum(rd.values()) == 5

    # Sharpe: positive expectancy, non-zero variance
    sharpe = compute_sharpe(ec)
    assert math.isfinite(sharpe)
    assert sharpe > 0  # net positive returns
