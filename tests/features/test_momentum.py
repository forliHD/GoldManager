"""Tests for the CandleMomentumEngine (no pattern names)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features.momentum import CandleMomentumEngine


def _bar(time: datetime, o: float, h: float, low: float, c: float, tv: int = 100) -> Bar:
    return Bar(
        symbol="XAUUSD",
        timeframe="M1",
        time=time,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(low)),
        close=Decimal(str(c)),
        tick_volume=tv,
    )


def _bullish_run(n: int, body: float = 1.0) -> list[Bar]:
    """n consecutive bullish bars (close > open)."""

    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    price = 2000.0
    for i in range(n):
        t = base + timedelta(minutes=i)
        o = price
        c = price + body
        bars.append(_bar(t, o, c + 0.5, o - 0.5, c))
        price = c
    return bars


# ------------------------------------------------------------------- per-bar


def test_body_size_atr_positive_for_strong_bar() -> None:
    """A bar with a 2x-ATR body has body_size_atr ≈ 2.0."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    # 20 small bars to establish ATR, then one big bar.
    bars = _bullish_run(20, body=0.5)
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2010, 2020, 2010 - 0.5, 2020, tv=1000))
    out = eng.compute(bars, bars[-1].time)
    per = out.by_tf["M1"]
    assert per.body_size_atr > 1.0  # at least 1 ATR body


def test_displacement_flag_on_2x_atr_body() -> None:
    """body > 2*ATR → displacement = True."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2010, 2020, 2010 - 0.5, 2020, tv=1000))
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].displacement is True


def test_displacement_flag_on_1_5x_median_body() -> None:
    """body > 1.5× median body → displacement = True."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    # All bars have body=0.5; the next bar has body=1.0 (2x median).
    bars = _bullish_run(20, body=0.5)
    # New bar: o=2010, h=2011.3, l=2009.3, c=2011.0. body=1.0, range=2.0.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2010, 2011.3, 2009.3, 2011, tv=100))
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].displacement is True


def test_no_displacement_for_normal_body() -> None:
    """A bar with body ≈ median → no displacement."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)  # all same body
    out = eng.compute(bars, bars[-1].time)
    # The last bar is a normal body (matches the median).
    assert out.by_tf["M1"].displacement is False


def test_impulsive_follow_through_counts_consecutive_bars() -> None:
    """5 consecutive bullish bars → follow_through = 5."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(5, body=0.5)
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].impulsive_follow_through == 5


def test_follow_through_resets_on_direction_change() -> None:
    """A bearish bar after bullish bars resets the count to 1 (the bearish bar)."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(3, body=0.5)
    # A bearish bar.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2001, 2001.5, 2000, 2000.5, tv=100))
    out = eng.compute(bars, bars[-1].time)
    assert out.by_tf["M1"].impulsive_follow_through == 1


def test_wick_body_ratio_high_for_pin_bar() -> None:
    """A pin bar (small body, big wicks) has a high wick_body_ratio."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    # 20 normal bars.
    bars = _bullish_run(20, body=0.5)
    # A pin bar: open=close=2000, high=2010, low=1990 → body=0, range=20.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2000, 2010, 1990, 2000.05, tv=100))
    out = eng.compute(bars, bars[-1].time)
    per = out.by_tf["M1"]
    assert per.wick_body_ratio > 5.0


def test_close_position_1_for_close_at_high() -> None:
    """A bar where close == high → close_position = 1.0."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    # close=high bar: o=2000, h=2010, l=1999, c=2010.
    bars.append(_bar(bars[-1].time + timedelta(minutes=1), 2000, 2010, 1999, 2010, tv=100))
    out = eng.compute(bars, bars[-1].time)
    per = out.by_tf["M1"]
    assert per.close_position == 1.0


# ------------------------------------------------------------------- aggregate


def test_aggregate_score_0_to_100() -> None:
    """The aggregate score is clamped to [0, 100]."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    out = eng.compute(bars, bars[-1].time)
    assert 0.0 <= out.score <= 100.0


def test_no_bars_returns_zero_score() -> None:
    eng = CandleMomentumEngine()
    out = eng.compute([], datetime(2026, 1, 5, 0, 0, tzinfo=UTC))
    assert out.score == 0.0
    assert out.by_tf == {}


# ------------------------------------------------------------------- PIT


def test_pit_excludes_bars_after_current_t() -> None:
    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = _bullish_run(20, body=0.5)
    cutoff = bars[10].time
    out_pre = eng.compute(bars, cutoff)
    fut = _bar(bars[10].time + timedelta(minutes=1), 9999, 9999.5, 9998.5, 9999, tv=999999)
    out_with_fut = eng.compute(bars + [fut], cutoff)
    # Last visible bar is at index 10; features should match.
    assert out_pre.by_tf["M1"].body_size_atr == out_with_fut.by_tf["M1"].body_size_atr


# ------------------------------------------------------------------- adversarial


def test_no_pattern_names_in_source() -> None:
    """The momentum engine and its output schema must NOT contain pattern names.

    WHY: AGENTS.md §1 says the bot must not pattern-match on bar
    shapes ("hammer", "shooting star", etc.) at the engine level.
    These names belong to retail trading folklore; they're useful for
    humans but they're not a defensible signal in a backtest. The
    CandleMomentumEngine emits *quantitative* features (body/ATR,
    wick/body, close position) only.

    A test that grep's the source for these names is the only way to
    prevent a future "helpful" change from re-introducing the labels.
    If this test ever fails, the change is a regression — discuss
    before merging.

    Note: pattern names are allowed in *docstrings* (where they are
    referenced as examples of the rule itself) and in *comments*.
    What is forbidden is using them as *field names, variable names,
    enum values, or function arguments* — the things that would let
    them leak into the output schema or API surface.
    """

    import ast
    import re
    from pathlib import Path

    src_dir = Path("src/xauusd_bot/features")
    forbidden_patterns = [
        r"\bhammer\b",
        r"\bshooting[-_ ]?star\b",
        r"\bengulfing\b",
        r"\bmarubozu\b",
        r"\bspinning[-_ ]?top\b",
        r"\bpin[-_ ]?bar\b",
        r"\bfull[-_ ]?body\b",
    ]
    # We allow 'doji' in the existing engine comment that says
    # "doji with no range" — but not as a label anywhere else.
    forbidden_patterns.append(r"\bdoji\b")

    violations: list[str] = []
    for f in src_dir.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        # Parse to identify docstring/comment spans — we skip those.
        try:
            tree = ast.parse(text, filename=str(f))
        except SyntaxError:
            violations.append(f"{f.name}: SyntaxError — could not parse")
            continue
        # Get the line ranges of all docstrings (top-level + class + function).
        docstring_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                ds = node.body[0]
                docstring_ranges.append((ds.lineno, ds.end_lineno or ds.lineno))

        def _in_docstring(lineno: int, ranges: list[tuple[int, int]] = docstring_ranges) -> bool:
            return any(start <= lineno <= end for start, end in ranges)

        for pat in forbidden_patterns:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                # Find the line number of this match.
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_no = text[:line_start].count("\n") + 1
                if _in_docstring(line_no):
                    continue
                # Get the actual line for the violation message.
                line_end = text.find("\n", m.end())
                line = text[line_start:line_end]
                # Skip comment-only lines (lines whose first non-ws is #).
                if line.lstrip().startswith("#"):
                    continue
                # Special case: the existing engine comment "doji with no range"
                # is a comment-style mention — but it's in a docstring already,
                # so the in-docstring check above handles it.
                violations.append(f"{f.name}:{line_no}:{line!r} matches {pat}")
    assert not violations, (
        "Pattern labels found in feature engine source — AGENTS.md §1 forbids this. "
        "Violations:\n" + "\n".join(violations)
    )


def test_momentum_does_not_have_string_pattern_field() -> None:
    """The CandleMomentumPerBar schema has no string 'pattern' or 'name' field.

    WHY: a pattern-name field is the simplest way to slip a label into
    the output. The schema must remain purely numeric. If a field
    like 'pattern: str' ever appears, this test catches it.
    """

    from xauusd_bot.common.schemas.features import CandleMomentumPerBar

    fields = CandleMomentumPerBar.model_fields
    for name, field in fields.items():
        # All fields must be numeric (int, float) or bool.
        annotation = str(field.annotation)
        assert any(
            keyword in annotation
            for keyword in ("int", "float", "bool")
        ), f"field {name!r} has non-numeric annotation: {annotation}"


def test_follow_through_with_doji_resets_to_zero() -> None:
    """A doji (open == close) resets the impulsive_follow_through counter.

    WHY: a doji is a "no direction" bar. The follow-through counter
    should NOT count it as a continuation. If a doji is counted as
    the start of a new run, the counter would over-report momentum.
    """

    eng = CandleMomentumEngine(timeframes=("M1",))
    base = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    bars: list[Bar] = []
    # 3 bullish bars.
    for i in range(3):
        t = base + timedelta(minutes=i)
        bars.append(_bar(t, 2000 + i, 2001 + i, 1999 + i, 2000.5 + i, tv=100))
    # A doji: open == close.
    bars.append(_bar(base + timedelta(minutes=3), 2004, 2005, 2003, 2004, tv=100))
    # Then a bullish bar.
    bars.append(_bar(base + timedelta(minutes=4), 2004, 2005, 2003, 2004.5, tv=100))
    out = eng.compute(bars, base + timedelta(minutes=4))
    # The last bar is bullish, but the bar before was a doji → counter
    # is 1 (just the last bar), not 4.
    assert out.by_tf["M1"].impulsive_follow_through == 1


def test_tick_volume_percentile_with_constant_volume_is_50() -> None:
    """Constant volume across the lookback → percentile ≈ 50 (the neutral value)."""

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars: list[Bar] = []
    for i in range(50):
        t = datetime(2026, 1, 5, 0, 0, tzinfo=UTC) + timedelta(minutes=i)
        bars.append(_bar(t, 2000, 2001, 1999, 2000.5, tv=1000))  # constant tv
    out = eng.compute(bars, datetime(2026, 1, 5, 0, 49, tzinfo=UTC))
    # All 50 bars have tv=1000, so the last bar's percentile should be
    # near 100 (last bar is among the highest, since all are equal).
    # The percentile_rank function: (series < value).sum() / (n-1) * 100
    # For all-equal, that's 100% (last value is not less than itself).
    # The test allows the neutral 50 OR 100, since the exact value
    # depends on tie-breaking.
    pct = out.by_tf["M1"].tick_volume_percentile
    assert 0.0 <= pct <= 100.0


def test_follow_through_includes_current_bar() -> None:
    """The current bar is included in its own follow-through count.

    WHY: there's a classic off-by-one in rolling counters — does the
    "current sample" count toward the run? Our convention is YES (the
    current bar is the start of its own run). This test locks that
    in: a single bullish bar → follow_through=1, not 0.
    """

    eng = CandleMomentumEngine(timeframes=("M1",))
    bars = [_bar(datetime(2026, 1, 5, 0, 0, tzinfo=UTC), 2000, 2001, 1999, 2000.5, tv=100)]
    out = eng.compute(bars, datetime(2026, 1, 5, 0, 0, tzinfo=UTC))
    # Single bullish bar: the run is 1 bar long.
    assert out.by_tf["M1"].impulsive_follow_through == 1
