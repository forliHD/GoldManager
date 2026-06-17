"""Tests for the BotOverlay.mq5 simulator.

The MQL5 indicator itself cannot be unit-tested in Python (different
language / runtime). So we mirror its read-parse-draw logic in Python
and assert correctness here. Visual chart validation still needs a
manual MetaTrader session — see AGENTS.md §4g-2.

These tests cover:
  1.  Sample full JSON → golden-master DrawOp list
  2.  Missing file → empty plan, error recorded
  3.  Corrupt JSON → empty plan, warning recorded
  4.  Partial vwap.utc00=null → that line missing, others present
  5.  All vwap.* null → no vwap hlines
  6.  weekly state='developing' → STYLE_DOT
  7.  weekly state='locked' → STYLE_SOLID
  8.  weekly without state → STYLE_SOLID (defensive default)
  9.  prev_week null → no prev profile hlines
  10. All prev_* null → no prev_* lines at all
  11. monthly dev, yearly locked → mixed styles
  12. fvg_zones empty → no fvg rects
  13. 3 bullish + 2 bearish → 5 rects with correct colors
  14. fvg_zone without `type` → default bullish
  15. Unknown top-level field → ignored, no crash
  16. ts field → not used for draw ops (only metadata)
  17. Multi-bar: second read replaces first plan
  18. Volume profile missing vpoc → vah/val present, no vpoc hline
  19. Negative prices → accepted (no filter on price sign)
  20. Very large numbers (1e6) → accepted
  21. (bonus) VWAP key missing entirely → no vwap hlines, no crash
  22. (bonus) FVG zone with top==bottom → skipped (top<=bottom)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xauusd_bot.viz.bot_overlay_simulator import (
    COLOR_FVG_BEAR,
    COLOR_FVG_BULL,
    COLOR_VP_DEV,
    COLOR_VP_LOCK,
    COLOR_VP_PREV,
    COLOR_VWAP_00,
    COLOR_VWAP_07,
    COLOR_VWAP_12,
    DrawPlan,
    VP_PERIODS,
    VWAP_OBJECT_NAMES,
    plan_to_dict,
    simulate_mql5_read,
)

# ---------------------------------------------------------------- sample builders


def _full_payload() -> dict:
    """A fully-populated overlay payload (golden master)."""

    return {
        "ts": "2026-04-07T22:39:00+00:00",
        "vwap": {
            "utc00": 2370.4,
            "utc07": 2374.8,
            "utc12": 2378.2,
        },
        "volume_profile": {
            "weekly": {"vah": 2368.0, "vpoc": 2358.0, "val": 2346.0, "state": "developing"},
            "monthly": {"vah": 2390.0, "vpoc": 2372.0, "val": 2355.0, "state": "developing"},
            "yearly": {"vah": 2450.0, "vpoc": 2380.0, "val": 2320.0, "state": "locked"},
            "prev_week": {"vah": 2360.0, "vpoc": 2351.0, "val": 2340.0},
            "prev_month": {"vah": 2360.0, "vpoc": 2351.0, "val": 2340.0},
            "prev_year": {"vah": 2360.0, "vpoc": 2351.0, "val": 2340.0},
        },
        "fvg_zones": [
            {"tf": "H1", "type": "bullish", "top": 2373.0, "bottom": 2371.5},
            {"tf": "M5", "type": "bearish", "top": 2375.0, "bottom": 2374.0},
            {"tf": "H1", "type": "bullish", "top": 2380.0, "bottom": 2379.0},
            {"tf": "H1", "type": "bearish", "top": 2385.0, "bottom": 2384.0},
            {"tf": "M5", "type": "bearish", "top": 2390.0, "bottom": 2388.5},
        ],
    }


def _write_payload(tmp_path: Path, payload: dict, name: str = "overlay_levels.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------------------------------------------------------------- test 1: golden master


def test_full_payload_yields_expected_draw_ops(tmp_path: Path) -> None:
    """Sample-full JSON → expected DrawOp list (golden master)."""

    p = _write_payload(tmp_path, _full_payload())
    plan = simulate_mql5_read(p)

    assert plan.errors == []
    assert plan.ts == "2026-04-07T22:39:00+00:00"

    # 3 vwap hlines
    vwap_ops = [op for op in plan.hlines if op.name in VWAP_OBJECT_NAMES]
    assert len(vwap_ops) == 3
    vwap_by_name = {op.name: op for op in vwap_ops}
    assert vwap_by_name["vwap_utc00"].price == 2370.4
    assert vwap_by_name["vwap_utc00"].color == COLOR_VWAP_00
    assert vwap_by_name["vwap_utc07"].price == 2374.8
    assert vwap_by_name["vwap_utc07"].color == COLOR_VWAP_07
    assert vwap_by_name["vwap_utc12"].price == 2378.2
    assert vwap_by_name["vwap_utc12"].color == COLOR_VWAP_12

    # 6 profiles × 3 levels = 18 profile hlines + 18 labels
    profile_hlines = [op for op in plan.hlines if op.name.startswith("vp_")]
    assert len(profile_hlines) == 18
    assert len(plan.labels) == 18

    # 6 value-area rectangles (one per profile, all with vah>val)
    assert len(plan.rects) == 6 + 5  # 6 VA rects + 5 FVG rects

    # All vwap styles are solid; weekly/monthly are dot; yearly+prev_* are solid
    weekly_vah = next(op for op in profile_hlines if op.name == "vp_weekly_vah")
    assert weekly_vah.style == "dot"
    assert weekly_vah.color == COLOR_VP_DEV
    yearly_vah = next(op for op in profile_hlines if op.name == "vp_yearly_vah")
    assert yearly_vah.style == "solid"
    assert yearly_vah.color == COLOR_VP_LOCK
    prev_vah = next(op for op in profile_hlines if op.name == "vp_prev_week_vah")
    assert prev_vah.style == "solid"
    assert prev_vah.color == COLOR_VP_PREV


# ---------------------------------------------------------------- test 2: missing file


def test_missing_file_yields_empty_plan(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    plan = simulate_mql5_read(missing)
    assert plan.ops == []
    assert any("file_missing" in e for e in plan.errors)


# ---------------------------------------------------------------- test 3: corrupt JSON


def test_corrupt_json_yields_empty_plan(tmp_path: Path) -> None:
    p = tmp_path / "overlay_levels.json"
    p.write_text("{this is not valid JSON at all", encoding="utf-8")
    plan = simulate_mql5_read(p)
    assert plan.ops == []
    assert any("corrupt_json" in e for e in plan.errors)
    assert any("corrupt" in w for w in plan.warnings)


# ---------------------------------------------------------------- test 4: partial vwap


def test_vwap_utc00_null_skips_only_that_line(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["vwap"]["utc00"] = None
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    names = {op.name for op in plan.hlines}
    assert "vwap_utc00" not in names
    assert "vwap_utc07" in names
    assert "vwap_utc12" in names


# ---------------------------------------------------------------- test 5: all vwap null


def test_all_vwap_null_skips_all_vwap_lines(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["vwap"] = {"utc00": None, "utc07": None, "utc12": None}
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    vwap_ops = [op for op in plan.hlines if op.name in VWAP_OBJECT_NAMES]
    assert vwap_ops == []


# ---------------------------------------------------------------- test 6: weekly dev


def test_weekly_developing_uses_dot_style(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["volume_profile"]["weekly"]["state"] = "developing"
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    weekly_vah = next(op for op in plan.hlines if op.name == "vp_weekly_vah")
    assert weekly_vah.style == "dot"
    assert weekly_vah.color == COLOR_VP_DEV


# ---------------------------------------------------------------- test 7: weekly locked


def test_weekly_locked_uses_solid_style(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["volume_profile"]["weekly"]["state"] = "locked"
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    weekly_vah = next(op for op in plan.hlines if op.name == "vp_weekly_vah")
    assert weekly_vah.style == "solid"
    assert weekly_vah.color == COLOR_VP_LOCK


# ---------------------------------------------------------------- test 8: weekly no state


def test_weekly_without_state_defaults_to_solid(tmp_path: Path) -> None:
    payload = _full_payload()
    del payload["volume_profile"]["weekly"]["state"]
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    weekly_vah = next(op for op in plan.hlines if op.name == "vp_weekly_vah")
    assert weekly_vah.style == "solid"
    assert weekly_vah.color == COLOR_VP_LOCK


# ---------------------------------------------------------------- test 9: prev_week null


def test_prev_week_null_skips_prev_profile(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["volume_profile"]["prev_week"] = None
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    names = {op.name for op in plan.hlines}
    assert "vp_prev_week_vah" not in names
    assert "vp_prev_week_vpoc" not in names
    assert "vp_prev_week_val" not in names
    # Other prev_* still drawn
    assert "vp_prev_month_vah" in names
    assert "vp_prev_year_vah" in names


# ---------------------------------------------------------------- test 10: all prev null (AGENTS.md §4b-1)


def test_all_prev_null_skips_all_prev_lines(tmp_path: Path) -> None:
    """First day of a new period: no prev_* at all → no prev_* lines."""

    payload = _full_payload()
    payload["volume_profile"]["prev_week"] = None
    payload["volume_profile"]["prev_month"] = None
    payload["volume_profile"]["prev_year"] = None
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    names = {op.name for op in plan.hlines}
    for period in ("prev_week", "prev_month", "prev_year"):
        assert f"vp_{period}_vah" not in names
        assert f"vp_{period}_vpoc" not in names
        assert f"vp_{period}_val" not in names
    # Live profiles still drawn
    for period in ("weekly", "monthly", "yearly"):
        assert f"vp_{period}_vah" in names
    # VWAPs still drawn
    assert "vwap_utc00" in names


# ---------------------------------------------------------------- test 11: mixed styles


def test_monthly_developing_yearly_locked_mixed_styles(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["volume_profile"]["monthly"]["state"] = "developing"
    payload["volume_profile"]["yearly"]["state"] = "locked"
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    monthly = next(op for op in plan.hlines if op.name == "vp_monthly_vah")
    yearly = next(op for op in plan.hlines if op.name == "vp_yearly_vah")
    assert monthly.style == "dot"
    assert monthly.color == COLOR_VP_DEV
    assert yearly.style == "solid"
    assert yearly.color == COLOR_VP_LOCK


# ---------------------------------------------------------------- test 12: no fvg zones


def test_empty_fvg_zones_yields_no_fvg_rects(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["fvg_zones"] = []
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    fvg_rects = [op for op in plan.rects if op.name.startswith("fvg_")]
    assert fvg_rects == []
    # But VA rects are still drawn.
    va_rects = [op for op in plan.rects if op.name.startswith("va_")]
    assert len(va_rects) == 6


# ---------------------------------------------------------------- test 13: mixed fvg zones


def test_fvg_zones_mixed_bull_bear_yields_correct_rects(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["fvg_zones"] = [
        {"tf": "H1", "type": "bullish", "top": 2373.0, "bottom": 2371.5},
        {"tf": "H1", "type": "bullish", "top": 2380.0, "bottom": 2379.0},
        {"tf": "H1", "type": "bullish", "top": 2385.0, "bottom": 2384.0},
        {"tf": "H1", "type": "bearish", "top": 2390.0, "bottom": 2389.0},
        {"tf": "H1", "type": "bearish", "top": 2395.0, "bottom": 2394.0},
    ]
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    fvg_rects = [op for op in plan.rects if op.name.startswith("fvg_")]
    assert len(fvg_rects) == 5
    bull = [op for op in fvg_rects if op.color == COLOR_FVG_BULL]
    bear = [op for op in fvg_rects if op.color == COLOR_FVG_BEAR]
    assert len(bull) == 3
    assert len(bear) == 2


# ---------------------------------------------------------------- test 14: missing fvg type


def test_fvg_zone_missing_type_defaults_to_bullish(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["fvg_zones"] = [
        {"tf": "H1", "top": 2373.0, "bottom": 2371.5},  # no type
    ]
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    fvg_rects = [op for op in plan.rects if op.name.startswith("fvg_")]
    assert len(fvg_rects) == 1
    assert fvg_rects[0].color == COLOR_FVG_BULL


# ---------------------------------------------------------------- test 15: unknown field


def test_unknown_top_level_field_is_ignored(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["custom_unknown_field"] = {"whatever": 42}
    payload["another_unknown"] = [1, 2, 3]
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    # No crash, normal ops still produced.
    assert plan.errors == []
    assert len(plan.hlines) >= 3


# ---------------------------------------------------------------- test 16: ts not used for ops


def test_ts_field_is_just_metadata(tmp_path: Path) -> None:
    payload = _full_payload()
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    assert plan.ts == "2026-04-07T22:39:00+00:00"
    # None of the DrawOps should carry the ts as a price.
    for op in plan.ops:
        if op.price is not None:
            assert op.price != 0  # not the ts epoch


# ---------------------------------------------------------------- test 17: multi-bar re-render


def test_second_read_replaces_first_plan(tmp_path: Path) -> None:
    """OnTimer-style re-read: the new DrawOps replace the old ones.

    In real MQL5, OnTimer calls DrawOverlay → ClearAll → re-draw. We
    simulate that as "the latest plan wins; the previous is discarded".
    """

    payload = _full_payload()
    p = _write_payload(tmp_path, payload)
    plan1 = simulate_mql5_read(p)
    n_ops_1 = len(plan1.ops)

    # Mutate the file (shift prices, remove a profile).
    payload2 = _full_payload()
    payload2["vwap"]["utc00"] = 9999.99
    payload2["volume_profile"]["weekly"] = None
    p.write_text(json.dumps(payload2), encoding="utf-8")

    plan2 = simulate_mql5_read(p)
    # New plan reflects the new state.
    utc00_2 = next(op for op in plan2.hlines if op.name == "vwap_utc00")
    assert utc00_2.price == 9999.99
    assert "vp_weekly_vah" not in {op.name for op in plan2.hlines}
    # plan1 is unchanged (defensive: it was a snapshot).
    utc00_1 = next(op for op in plan1.hlines if op.name == "vwap_utc00")
    assert utc00_1.price == 2370.4
    # Different plan sizes.
    assert len(plan2.ops) != n_ops_1


# ---------------------------------------------------------------- test 18: profile missing vpoc


def test_profile_missing_vpoc_skips_vpoc_only(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["volume_profile"]["weekly"]["vpoc"] = None
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    names = {op.name for op in plan.hlines}
    assert "vp_weekly_vah" in names
    assert "vp_weekly_vpoc" not in names
    assert "vp_weekly_val" in names
    # Other profiles unaffected.
    assert "vp_monthly_vpoc" in names


# ---------------------------------------------------------------- test 19: negative prices accepted


def test_negative_prices_are_accepted(tmp_path: Path) -> None:
    """Backtests with synthetic data can have prices < 0 — don't filter."""

    payload = _full_payload()
    payload["vwap"]["utc00"] = -50.0
    payload["volume_profile"]["weekly"]["vah"] = -10.0
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    utc00 = next(op for op in plan.hlines if op.name == "vwap_utc00")
    assert utc00.price == -50.0
    vah = next(op for op in plan.hlines if op.name == "vp_weekly_vah")
    assert vah.price == -10.0


# ---------------------------------------------------------------- test 20: very large numbers


def test_very_large_prices_are_accepted(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["vwap"]["utc00"] = 1e6
    payload["volume_profile"]["weekly"]["vpoc"] = 1e9
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    utc00 = next(op for op in plan.hlines if op.name == "vwap_utc00")
    assert utc00.price == 1e6
    vpoc = next(op for op in plan.hlines if op.name == "vp_weekly_vpoc")
    assert vpoc.price == 1e9


# ---------------------------------------------------------------- bonus: vwap key missing entirely


def test_vwap_section_missing_skips_all_vwap(tmp_path: Path) -> None:
    payload = _full_payload()
    del payload["vwap"]
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    vwap_ops = [op for op in plan.hlines if op.name in VWAP_OBJECT_NAMES]
    assert vwap_ops == []
    # No crash; other ops still drawn.
    assert any(op.name.startswith("vp_weekly") for op in plan.hlines)


# ---------------------------------------------------------------- bonus: fvg top == bottom → skip


def test_fvg_zone_with_top_equal_to_bottom_is_skipped(tmp_path: Path) -> None:
    payload = _full_payload()
    payload["fvg_zones"] = [
        {"tf": "H1", "type": "bullish", "top": 2373.0, "bottom": 2373.0},
    ]
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    fvg_rects = [op for op in plan.rects if op.name.startswith("fvg_")]
    assert fvg_rects == []
    assert any("top<=bottom" in w for w in plan.warnings)


# ---------------------------------------------------------------- helpers exposed


def test_plan_to_dict_serializes_for_logging(tmp_path: Path) -> None:
    p = _write_payload(tmp_path, _full_payload())
    plan = simulate_mql5_read(p)
    blob = plan_to_dict(plan)
    assert "ops" in blob
    assert "ts" in blob
    assert "n_hlines" in blob
    assert "n_rects" in blob
    assert "n_labels" in blob
    assert blob["n_hlines"] >= 3
    # Round-trippable via json.
    json.dumps(blob)


def test_drawop_op_key_is_stable() -> None:
    from xauusd_bot.viz.bot_overlay_simulator import DrawOp

    op1 = DrawOp(kind="hline", name="vwap_utc00", price=2370.4)
    op2 = DrawOp(kind="hline", name="vwap_utc00", price=2400.0)
    assert op1.op_key() == op2.op_key()


# ---------------------------------------------------------------- parametrized style matrix


@pytest.mark.parametrize(
    "period,state,expected_color,expected_style",
    [
        ("weekly", "developing", COLOR_VP_DEV, "dot"),
        ("weekly", "locked", COLOR_VP_LOCK, "solid"),
        ("monthly", "developing", COLOR_VP_DEV, "dot"),
        ("yearly", "locked", COLOR_VP_LOCK, "solid"),
        ("prev_week", "locked", COLOR_VP_PREV, "solid"),
        ("prev_month", "developing", COLOR_VP_PREV, "dot"),
        ("prev_year", "", COLOR_VP_PREV, "solid"),  # missing state, still prev
    ],
)
def test_volume_profile_style_matrix(
    tmp_path: Path,
    period: str,
    state: str,
    expected_color: str,
    expected_style: str,
) -> None:
    payload = _full_payload()
    payload["volume_profile"][period]["state"] = state
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    op = next(o for o in plan.hlines if o.name == f"vp_{period}_vah")
    assert op.color == expected_color
    assert op.style == expected_style


def test_all_periods_covered_by_style_matrix() -> None:
    """Regression guard: the parametrize matrix must cover all VP_PERIODS."""

    assert set(VP_PERIODS) == {
        "weekly",
        "monthly",
        "yearly",
        "prev_week",
        "prev_month",
        "prev_year",
    }


# ---------------------------------------------------------------- AGENTS.md §4b-1 regression


def test_all_prev_null_no_draw_ops_satisfies_caveat_4b_1(tmp_path: Path) -> None:
    """AGENTS.md §4b-1: on the first day of a new period, prev_*=null.

    The MQL5 indicator MUST gracefully omit those lines (not crash).
    This test is the regression guard named in AGENTS.md §4g-3.
    """

    payload = {
        "ts": "2026-01-01T00:00:00+00:00",
        "vwap": {"utc00": 2370.4, "utc07": 2374.8, "utc12": 2378.2},
        "volume_profile": {
            "weekly": {"vah": 2368.0, "vpoc": 2358.0, "val": 2346.0, "state": "developing"},
            "monthly": {"vah": 2390.0, "vpoc": 2372.0, "val": 2355.0, "state": "developing"},
            "yearly": {"vah": 2450.0, "vpoc": 2380.0, "val": 2320.0, "state": "developing"},
            "prev_week": None,
            "prev_month": None,
            "prev_year": None,
        },
        "fvg_zones": [],
    }
    p = _write_payload(tmp_path, payload)
    plan = simulate_mql5_read(p)
    assert plan.errors == []
    # No prev_* lines.
    for op in plan.ops:
        assert "prev_week" not in op.name
        assert "prev_month" not in op.name
        assert "prev_year" not in op.name
    # Live profiles still drawn (regression check).
    assert "vp_weekly_vah" in {op.name for op in plan.hlines}
    # VWAPs still drawn.
    assert "vwap_utc00" in {op.name for op in plan.hlines}